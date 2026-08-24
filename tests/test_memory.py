"""The conversation memory: pending-question detection, the digest block,
and the confirmation injection.

Pure parts only - the memory is built from message objects and plain text,
no network, no server. The memory is what makes a bare "si" resolve on any
model: the pending question is detected mechanically and injected
explicitly into the next turn's context.

Requires the agent stack (requirements-agent.txt); the module import is
skipped when it is not installed.
"""
import unittest

import _stubs  # noqa: F401

from langchain_core.messages import AIMessage, ToolMessage

try:
    from reciproca.agent.memory import SessionMemory, is_confirmation
except ImportError:
    SessionMemory = None
    is_confirmation = None


@unittest.skipIf(SessionMemory is None, "agent stack not installed")
class ConfirmationTest(unittest.TestCase):
    def test_italian_confirmations_are_recognised(self):
        for text in ("si", "sì", "SI!", "ok", "okay", "certo", "va bene",
                     "confermo", "perfetto", "sisi"):
            self.assertTrue(is_confirmation(text), text)

    def test_english_confirmations_are_recognised(self):
        for text in ("yes", "yeah", "yep", "yup", "sure", "of course",
                     "alright", "fine", "confirmed", "go ahead",
                     "sounds good", "YES", "Ok!"):
            self.assertTrue(is_confirmation(text), text)

    def test_real_messages_are_not_confirmations(self):
        for text in ("ciao", "segui 5 dalla coda", "si ma aspetta",
                     "fermati", "come va la sessione?", "hello",
                     "follow 5 from the queue", "yes please",
                     "sure thing", "fine, let's go"):
            self.assertFalse(is_confirmation(text), text)


@unittest.skipIf(SessionMemory is None, "agent stack not installed")
class PendingQuestionTest(unittest.TestCase):
    def _record(self, memory, messages, request="segui 5 dalla coda"):
        memory.record_turn(request, messages)
        return memory

    def test_a_question_at_the_end_is_pending(self):
        memory = SessionMemory()
        self._record(memory, [AIMessage(content="Parto con i primi 5 - confermi?")])
        self.assertIsNotNone(memory.pending)
        self.assertIn("confermi?", memory.pending["question"])

    def test_a_narration_after_the_question_clears_it(self):
        memory = SessionMemory()
        self._record(memory, [
            AIMessage(content="Parto con i primi 5 - confermi?"),
            AIMessage(content="Ok, parto."),
        ])
        self.assertIsNone(memory.pending)

    def test_a_question_before_a_final_tool_call_stays_pending(self):
        # The agent asked, then made a last tool call (e.g. cycle_status)
        # and the turn ended: the question is still open.
        memory = SessionMemory()
        self._record(memory, [
            AIMessage(content="Parto con i primi 5 - confermi?",
                      tool_calls=[{"name": "status", "args": {}, "id": "c1",
                                   "type": "tool_call"}]),
            ToolMessage(content='{"ok": true}', tool_call_id="c1"),
        ])
        self.assertIsNotNone(memory.pending)

    def test_a_content_list_does_not_count_as_a_question(self):
        memory = SessionMemory()
        self._record(memory, [AIMessage(content=["Confermi?"])])
        self.assertIsNone(memory.pending)

    def test_no_narration_means_no_pending(self):
        memory = SessionMemory()
        self._record(memory, [])
        self.assertIsNone(memory.pending)


@unittest.skipIf(SessionMemory is None, "agent stack not installed")
class DigestTest(unittest.TestCase):
    def test_the_first_turn_has_no_conversation(self):
        memory = SessionMemory()
        self.assertIn("no previous conversation", memory.digest())

    def test_the_digest_carries_request_outcome_and_question(self):
        memory = SessionMemory()
        memory.record_turn(
            "segui 5 dalla coda",
            [AIMessage(content="Parto con i primi 5 - confermi?")])
        digest = memory.digest()
        self.assertIn("segui 5 dalla coda", digest)
        self.assertIn("Parto con i primi 5 - confermi?", digest)
        self.assertIn("Question awaiting an answer", digest)

    def test_wait_interruptions_are_not_lost(self):
        memory = SessionMemory()
        memory.record_turn(
            "segui 5 dalla coda",
            [AIMessage(content="Ok, parto.", tool_calls=[
                {"name": "wait", "args": {"seconds": 60}, "id": "w1",
                 "type": "tool_call"}]),
             ToolMessage(content="Wait ended early - the user typed: fermati",
                         tool_call_id="w1")])
        self.assertIn("fermati", memory.digest())

    def test_the_history_is_trimmed_to_the_last_turns(self):
        memory = SessionMemory(history_size=2)
        for i in range(4):
            memory.record_turn(f"richiesta {i}", [AIMessage(content=f"fatto {i}")])
        digest = memory.digest()
        self.assertNotIn("richiesta 0", digest)
        self.assertIn("richiesta 3", digest)

    def test_summary_requests_are_detected_in_both_languages(self):
        from reciproca.agent.memory import _asks_for_summary
        for text in ("riassumi la chat", "riassunto della conversazione",
                     "riepiloga cosa abbiamo fatto", "puoi fare una sintesi?",
                     "summarize the conversation", "recap please"):
            self.assertTrue(_asks_for_summary(text), text)
        for text in ("come va la sessione?", "segui 5 dalla coda", "ciao",
                     "what is the status?"):
            self.assertFalse(_asks_for_summary(text), text)

    def test_a_summary_request_widens_the_memory_window(self):
        # A recap must see the whole kept conversation, not just the
        # default window - otherwise "riassumi la chat" answers with the
        # last few exchanges and the user hears the memory "cut off".
        memory = SessionMemory(history_size=2)
        for i in range(6):
            memory.record_turn(f"richiesta {i}", [AIMessage(content=f"fatto {i}")])
        flat = " ".join(m[1] for m in memory.context_messages("come va?"))
        self.assertNotIn("richiesta 0", flat)
        self.assertIn("richiesta 5", flat)
        for request in ("riassumi la chat", "summarize the conversation"):
            flat = " ".join(m[1] for m in memory.context_messages(request))
            self.assertIn("richiesta 0", flat)
            self.assertIn("richiesta 5", flat)


@unittest.skipIf(SessionMemory is None, "agent stack not installed")
class ContextBuildingTest(unittest.TestCase):
    def test_the_turn_opens_with_memory_then_the_request(self):
        memory = SessionMemory()
        messages = memory.context_messages("ciao")
        self.assertEqual(messages[-1], ("user", "ciao"))
        self.assertEqual(messages[0][0], "system")
        self.assertIn("Conversation memory", messages[0][1])

    def test_a_confirmation_gets_the_pending_question_injected(self):
        memory = SessionMemory()
        memory.record_turn(
            "segui 5 dalla coda",
            [AIMessage(content="Parto con i primi 5 - confermi?")])
        messages = memory.context_messages("si")
        injections = [m for m in messages if m[0] == "system" and m is not messages[0]]
        self.assertEqual(len(injections), 1)
        self.assertIn("Parto con i primi 5 - confermi?", injections[0][1])
        self.assertEqual(messages[-1], ("user", "si"))

    def test_no_injection_without_a_pending_question(self):
        memory = SessionMemory()
        memory.record_turn("ciao", [AIMessage(content="Ciao!")])
        messages = memory.context_messages("si")
        self.assertEqual(len(messages), 2)  # memory block + request only

    def test_no_injection_for_a_real_message(self):
        memory = SessionMemory()
        memory.record_turn(
            "segui 5 dalla coda",
            [AIMessage(content="Parto con i primi 5 - confermi?")])
        messages = memory.context_messages("come va la sessione?")
        self.assertEqual(len(messages), 2)

    def test_a_confirm_answer_resolves_end_to_end(self):
        # Turn 1: the agent asks. Turn 2: "si". The context of turn 2 must
        # let any model resolve the confirmation without reasoning.
        memory = SessionMemory()
        memory.record_turn(
            "segui 5 dalla coda",
            [AIMessage(content="La coda ha 288 candidati. Parto con i primi 5 - confermi?")])
        context = memory.context_messages("si")
        flat = " ".join(m[1] for m in context)
        self.assertIn("288 candidati", flat)
        self.assertIn("never ask them to restate it", flat)
        self.assertEqual(context[-1], ("user", "si"))

    def test_an_english_confirmation_resolves_an_english_question(self):
        # The whole machinery must work the same in an English
        # conversation: "yes" gets the open question injected, verbatim.
        memory = SessionMemory()
        memory.record_turn(
            "follow 5 from the queue",
            [AIMessage(content="The queue has 288 candidates. "
                               "Start with the top 5 - confirm?")])
        context = memory.context_messages("yes")
        flat = " ".join(m[1] for m in context)
        self.assertIn("confirm?", flat)
        self.assertIn("never ask them to restate it", flat)
        self.assertEqual(context[-1], ("user", "yes"))

    def test_an_italian_confirmation_after_an_english_question(self):
        # Mixed-language check: the Italian nod must also resolve a
        # question asked in English.
        memory = SessionMemory()
        memory.record_turn(
            "follow 5 from the queue",
            [AIMessage(content="Start with the top 5 - confirm?")])
        context = memory.context_messages("va bene")
        flat = " ".join(m[1] for m in context)
        self.assertIn("confirm?", flat)
        self.assertEqual(context[-1], ("user", "va bene"))


if __name__ == "__main__":
    unittest.main()
