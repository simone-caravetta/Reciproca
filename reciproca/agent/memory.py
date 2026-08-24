"""Conversation memory for the agent frontends (REPL and Telegram).

Each turn is a fresh request: the model must never re-run completed ones.
But a fresh turn is also blind - and then a plain "si" answering a
confirmation has no referent, and "come va la sessione?" knows nothing.
This module is the bridge: after each turn it records a compact digest
(what was requested, what was done, the pending question if any) and hands
it back to the next turn as a background block. The digest is built
mechanically from the turn's own messages - no model call, no AI summary -
so it is exactly as reliable on a 9B local model as on a large API one.

The one place the summary must be stronger than "what happened" is the
confirmation checkpoint: when the user answers with a bare confirmation and
a question is open, the question is injected explicitly into the context,
so the model never has to infer what the "si" refers to.
"""

import re

from langchain_core.messages import AIMessage, ToolMessage

# Turns kept in the digest. The block stays small: it is background context,
# not a transcript - the fresh request is the user's new message. A request
# for a summary ("riassumi la chat", "summarize the conversation") widens
# the window to SUMMARY_TURNS, so the model can actually recap the session
# instead of the last few exchanges.
HISTORY_SIZE = 8
SUMMARY_TURNS = 24
_REQUEST_CHARS = 200
_NARRATION_CHARS = 300

# Requests for a recap of the whole conversation, in the two languages.
# Both Italian roots: "riassum-" (riassumere, riassumi) and "riassun-"
# (riassunto, riassunzione).
_SUMMARY_RE = re.compile(r"(riassum|riassun|riepilog|recap|summariz|sintesi)",
                         re.IGNORECASE)


def _asks_for_summary(text):
    """A request to summarise the conversation (either language)."""
    return bool(_SUMMARY_RE.search(text))

# Bare confirmations, in the two languages the bot speaks, normalised
# before matching (lowercased, trailing punctuation stripped). Anything
# longer is a real message, not a nod.
CONFIRMATIONS = frozenset({
    # Italian
    "si", "sì", "sisi", "certo", "certamente", "confermo", "va bene",
    "d'accordo", "perfetto", "ok", "okay", "okok",
    # English
    "yes", "yeah", "yep", "yup", "sure", "definitely", "absolutely",
    "of course", "fine", "alright", "ok", "okay", "confirmed",
    "go ahead", "sounds good",
})

# The wait tool's early-return format (see agent.py).
_WAIT_INTERRUPT_RE = re.compile(r"^Wait ended early - the user typed: (.*)$")


def _text(message):
    """The message's content when it is a plain string, else ''."""
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else ""


def _trim(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def is_confirmation(text):
    """A bare confirmation in either language - "si", "yes", "ok", "sure" -
    and nothing else."""
    if not text or len(text) > 24:
        return False
    normalised = re.sub(r"[\s!.,;:?]+$", "", text.strip().lower())
    return normalised in CONFIRMATIONS


class SessionMemory:
    """The per-conversation record: pending question + the last few turns."""

    def __init__(self, history_size=HISTORY_SIZE):
        self.history_size = history_size
        self.turns = []      # {"request", "outcome", "interrupts"}
        self.pending = None  # {"question", "request"} when a question is open

    # -- recording ----------------------------------------------------------

    def record_turn(self, request, messages):
        """Register what this turn did, mechanically, from its own messages.

        `messages` is the turn's streamed message list. The outcome is the
        agent's last narration; wait interruptions ("Wait ended early - the
        user typed: X") are collected so a message a small model ignored is
        not lost - it reappears in the next turn's digest.
        """
        request = _trim(request, _REQUEST_CHARS)
        narrations = [m for m in messages if isinstance(m, AIMessage) and _text(m)]
        outcome = _trim(_text(narrations[-1]), _NARRATION_CHARS) if narrations else ""
        interrupts = [
            match.group(1).strip()
            for m in messages
            if isinstance(m, ToolMessage)
            for match in (_WAIT_INTERRUPT_RE.match(_text(m)),)
            if match
        ]
        self.turns.append({
            "request": request,
            "outcome": outcome,
            "interrupts": interrupts,
        })
        # Keep enough turns for a summary request as well as for the
        # default window; digest() decides how much to actually show.
        self.turns = self.turns[-max(self.history_size, SUMMARY_TURNS):]

        # A question is pending iff the agent's most recent words end with
        # one. If it said anything after the question, the question was
        # answered or superseded - so the narration before a final tool call
        # still counts (the last AIMessage with content is the question).
        last_words = narrations[-1] if narrations else None
        if last_words and _text(last_words).rstrip().endswith("?"):
            self.pending = {"question": _text(last_words), "request": request}
        else:
            self.pending = None

    # -- reading ------------------------------------------------------------

    def digest(self, max_turns=None):
        """The background block handed to the next turn.

        `max_turns` widens the window (used for summary requests); the
        default is the memory's history_size. The framing is in English
        (the system prompt's language, so the block reads the same in an
        English or an Italian conversation); the quoted requests, questions
        and log lines stay verbatim in whatever language the user wrote
        them.
        """
        if not self.turns and not self.pending:
            return "Conversation memory: no previous conversation."
        window = (self.turns[-max_turns:] if max_turns
                  else self.turns[-self.history_size:])
        lines = [
            "Conversation memory (background, not a request - never re-run "
            "what is marked as done):"
        ]
        if self.pending:
            lines.append(f"- Question awaiting an answer: «{self.pending['question']}»")
        for turn in window:
            bits = []
            if turn["request"]:
                bits.append(f"request: \"{turn['request']}\"")
            if turn["outcome"]:
                bits.append(f"outcome: \"{turn['outcome']}\"")
            for interrupt in turn["interrupts"]:
                bits.append(
                    f"while waiting, the user wrote: \"{interrupt}\" "
                    "(if you have not acted on it yet, do it now)")
            if bits:
                lines.append("- " + " · ".join(bits))
        return "\n".join(lines)

    def context_messages(self, text):
        """The turn's opening messages: memory block, maybe the confirmation
        injection, then the user's request."""
        window = SUMMARY_TURNS if _asks_for_summary(text) else None
        messages = [("system", self.digest(window))]
        if self.pending and is_confirmation(text):
            messages.append((
                "system",
                f"The user is answering the question: "
                f"«{self.pending['question']}» - respond to that "
                f"confirmation; never ask them to restate it.",
            ))
        messages.append(("user", text))
        return messages
