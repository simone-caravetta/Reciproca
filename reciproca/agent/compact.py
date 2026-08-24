"""The compacted view of the turn history the model receives at each step.

A turn with a lot of polling (cycle_status every 30s, status, logs_tail)
grows the message history with raw tool JSON, and the whole history is
re-sent to the model on every call: the model spends most of its context
window on tool outputs it has already consumed and acted on.

This module is the pre_model_hook of create_agent. It builds what the
model sees - user messages and the agent's own narrations always whole,
raw tool outputs reduced to a one-line essence once they have been used -
while the graph state (and with it the render and the turn memory) keeps
the full history. The compaction is selective, never lossy about facts:
an hashtags_add result keeps the full list of added tags, a cycle_status
keeps the same one-line status the wait tool uses, errors stay whole.
"""

import json

from langchain_core.messages import AIMessage, ToolMessage

# The last completed tool rounds stay whole: that is the immediate context
# the next decision is made on. Everything older is compacted.
_KEEP_ROUNDS = 2

# The keys that carry the facts of a tool result, in the order they are
# reported. Lists of additions/removals are kept whole (they are short);
# long listings are reduced to a count plus the first entries.
_ESSENCE_KEYS = (
    "added", "removed", "followed", "unfollowed", "skipped", "errors",
    "succeeded", "attempted", "done", "count", "total", "state", "error",
    "message", "browser", "logged_in", "queue", "hashtags", "running",
    "lines", "tasks",
)

_MAX_TEXT = 180       # non-JSON (or unparsable) results are truncated here
_MAX_ITEMS = 8        # how many entries of a long list to keep


def _format_value(value):
    if isinstance(value, list):
        items = [str(v) for v in value[:_MAX_ITEMS]]
        more = f", +{len(value) - _MAX_ITEMS} more" if len(value) > _MAX_ITEMS else ""
        return ", ".join(items) + more
    if isinstance(value, dict):
        # progress dicts: {"done": 2, "total": 5, "current": "street"}
        done, total = value.get("done"), value.get("total")
        if isinstance(done, int) and isinstance(total, int):
            line = f"{done}/{total}"
            if value.get("current"):
                line += f" ({value['current']})"
            return line
        return json.dumps(value, ensure_ascii=False)[:_MAX_TEXT]
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)[:_MAX_TEXT]


def _essence(text, tool_name=None):
    """One line carrying the facts of a tool result, no bloat.

    cycle_status snapshots use the same one-line status the wait tool
    hands the model; other JSON payloads report their informative keys;
    plain text is truncated. Errors always win over everything else.
    """
    stripped = (text or "").strip()
    if not stripped or stripped == "{}":
        return "no result"
    if not stripped.startswith(("{", "[")):
        return stripped[:_MAX_TEXT]
    try:
        payload = json.loads(stripped)
    except ValueError:
        return stripped[:_MAX_TEXT]
    if isinstance(payload, list):
        items = [str(v) for v in payload[:_MAX_ITEMS]]
        more = f", +{len(payload) - _MAX_ITEMS} more" if len(payload) > _MAX_ITEMS else ""
        return f"{len(payload)} items: {', '.join(items)}{more}"
    if not isinstance(payload, dict):
        return str(payload)[:_MAX_TEXT]
    if payload.get("ok") is False:
        return f"failed: {str(payload.get('error') or 'no reason given')[:140]}"
    if payload.get("error"):
        return f"error: {str(payload['error'])[:140]}"
    # cycle_status snapshots carry a task_id: reuse the wait tool's line.
    if tool_name in ("cycle_status", "unfollow_status") or "task_id" in payload:
        line = _status_line(payload)
        if line:
            return line
    parts = []
    for key in _ESSENCE_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "hashtags" and isinstance(value, list):
            # a listing, not an addition: count + first entries is enough
            items = [str(v) for v in value[:_MAX_ITEMS]]
            more = f", +{len(value) - _MAX_ITEMS} more" if len(value) > _MAX_ITEMS else ""
            parts.append(f"hashtags={len(value)} ({', '.join(items)}{more})")
        elif key == "queue" and isinstance(value, list):
            items = [str(v) for v in value[:_MAX_ITEMS]]
            more = f", +{len(value) - _MAX_ITEMS} more" if len(value) > _MAX_ITEMS else ""
            parts.append(f"queue={len(value)} ({', '.join(items)}{more})")
        elif key == "lines" and isinstance(value, list):
            last = str(value[-1])[-80:] if value else ""
            parts.append(f"lines={len(value)}"
                         f"{', last: ' + last if last else ''}")
        elif key == "tasks" and isinstance(value, list):
            running = sum(1 for t in value if isinstance(t, dict)
                          and t.get("state") == "running")
            parts.append(f"tasks={len(value)} ({running} running)")
        else:
            parts.append(f"{key}={_format_value(value)}")
    if parts:
        return ", ".join(parts)[:_MAX_TEXT + 60]
    return json.dumps(payload, ensure_ascii=False)[:_MAX_TEXT]


def _status_line(snapshot):
    """The same one-line status the wait tool reports (no import cycle:
    the runner's status_line lives in agent.agent, this is the compact
    copy with the same behaviour)."""
    tag = f"Task {snapshot.get('task_id') or '?'} ({snapshot.get('kind') or 'task'})"
    if snapshot.get("error"):
        return f"{tag} failed: {snapshot['error']}"
    if snapshot.get("state") == "done":
        result = snapshot.get("result")
        if isinstance(result, dict):
            parts = [f"{k}={result[k]}" for k in
                     ("followed", "skipped", "errors", "succeeded", "attempted",
                      "added", "removed", "unfollowed", "done") if k in result]
            summary = ", ".join(parts)[:100] if parts else ""
        else:
            summary = str(result)[:100] if result else ""
        return f"{tag} completed - {summary}" if summary else f"{tag} completed"
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        done, total = progress.get("done"), progress.get("total")
        if isinstance(done, int) and isinstance(total, int):
            line = f"{tag}: {done}/{total}"
            if progress.get("current"):
                line += f" ({progress['current']})"
            return line
    for entry in reversed(snapshot.get("last_logs") or []):
        message = entry.get("message") if isinstance(entry, dict) else None
        if message and message.strip():
            return f"{tag} in progress - last: {message.strip()}"
    elapsed = snapshot.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)):
        return f"{tag} in progress for {int(elapsed)}s"
    return f"{tag} in progress"


def _tool_text(message):
    """The text payload of a ToolMessage, whatever its content shape
    (str, list of dicts, list of content blocks)."""
    content = message.content or ""
    if isinstance(content, str):
        return content
    for block in content:
        text = block.get("text") if isinstance(block, dict) \
            else getattr(block, "text", None)
        if text:
            return text
    return ""


def _tool_rounds(messages):
    """The completed tool rounds: (start_index, aimessage, tools_by_id).

    A round is an AIMessage with tool_calls plus the ToolMessages that
    follow it, matched by tool_call_id. A round without its ToolMessages
    is not completed - the model has not seen the results yet, so it is
    never compacted.
    """
    rounds = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if not (isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None)):
            i += 1
            continue
        ids = {call.get("id") for call in msg.tool_calls}
        tools = {}
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            if messages[j].tool_call_id in ids:
                tools[messages[j].tool_call_id] = messages[j]
            j += 1
        rounds.append((i, msg, tools, j))
        i = j
    return rounds


def compact_turn_history(state):
    """The pre_model_hook for create_agent.

    Returns the compacted messages the model receives: user messages and
    narrations whole, the last _KEEP_ROUNDS tool rounds whole, and every
    older completed round reduced to a one-line essence. Returns {} when
    there is nothing to compact, so the graph uses the full state as-is.
    """
    if isinstance(state, dict):
        messages = state.get("messages")
    else:
        messages = getattr(state, "messages", None)
    if not messages:
        return {}

    rounds = _tool_rounds(messages)
    completed = [r for r in rounds if r[2]]
    if len(completed) <= _KEEP_ROUNDS:
        return {}

    compact_rounds = completed[:- _KEEP_ROUNDS]
    # ids of ToolMessages to drop: all tools of the compacted rounds
    drop_tool_ids = {tid for _, _, tools, _ in compact_rounds for tid in tools}

    out = []
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage) and msg.tool_call_id in drop_tool_ids:
            continue
        # is this the start of a compacted round? its narration (if any)
        # stays whole, the tool outputs become essence lines
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for start, aim, tools, _ in compact_rounds:
                if start == i:
                    lines = []
                    if aim.content:
                        lines.append(aim.content)
                    for call in aim.tool_calls:
                        tool_msg = tools.get(call.get("id"))
                        if tool_msg is None:
                            continue
                        name = call.get("name") or "tool"
                        lines.append(f"\U0001f527 {name} → "
                                     f"{_essence(_tool_text(tool_msg), name)}")
                    if lines:
                        out.append(AIMessage(content="\n".join(lines)))
                    break
            else:
                out.append(msg)
            continue
        out.append(msg)
    return {"llm_input_messages": out}
