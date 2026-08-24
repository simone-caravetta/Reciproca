"""The pre-model compaction: what the model sees at each step.

Pure tests on _essence and compact_turn_history, plus an integration test
that runs a real create_agent graph with a scripted chat model and checks
exactly what the model receives: user messages and narrations whole, the
last tool rounds whole, older tool outputs reduced to their essence.
"""
import asyncio
import unittest

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from reciproca.agent import compact


def _aim(content="", tool_calls=None):
    if tool_calls:
        return AIMessage(content=content, tool_calls=tool_calls)
    return AIMessage(content=content)


def _tool(name, text, call_id="c1"):
    return ToolMessage(content=[{"type": "text", "text": text}],
                       tool_call_id=call_id, name=name)


def _call(name, call_id, args=None):
    return {"name": name, "id": call_id, "args": args or {},
            "type": "tool_call"}


def _essence_lines(messages):
    """The content of the compact AIMessage(es) inside a compacted list."""
    out = []
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None) is None:
            out.append(msg.content)
    return out


class EssenceTest(unittest.TestCase):
    def test_error_wins_over_everything(self):
        payload = json_dumps({"ok": True, "added": ["x"],
                              "error": "rate_limited"})
        line = compact._essence(payload, "hashtags_add")
        self.assertIn("error: rate_limited", line)
        self.assertNotIn("added", line)

    def test_ok_false_reports_the_failure(self):
        line = compact._essence(
            json_dumps({"ok": False, "error": "hashtags must be a list"}), "queue_add")
        self.assertIn("failed: hashtags must be a list", line)

    def test_cycle_status_uses_the_status_line(self):
        snapshot = {"task_id": "t1", "kind": "follow", "state": "running",
                    "progress": {"done": 2, "total": 5, "current": "street"},
                    "last_logs": [{"message": "followed @user"}]}
        line = compact._essence(json_dumps(snapshot), "cycle_status")
        self.assertEqual(line, "Task t1 (follow): 2/5 (street)")

    def test_cycle_status_done_reports_the_result(self):
        snapshot = {"task_id": "t1", "kind": "follow", "state": "done",
                    "result": {"followed": 40, "skipped": 12, "errors": 1}}
        line = compact._essence(json_dumps(snapshot), "cycle_status")
        self.assertEqual(
            line, "Task t1 (follow) completed - followed=40, skipped=12, errors=1")

    def test_hashtags_add_keeps_the_full_list_of_added_tags(self):
        line = compact._essence(
            json_dumps({"ok": True, "added": ["street", "35mm", "analog"]}),
            "hashtags_add")
        self.assertIn("added=street, 35mm, analog", line)

    def test_queue_add_keeps_the_full_list_of_usernames(self):
        line = compact._essence(
            json_dumps({"ok": True, "added": ["alice", "bob"]}), "queue_add")
        self.assertIn("added=alice, bob", line)

    def test_listing_is_reduced_to_count_and_first_entries(self):
        line = compact._essence(
            json_dumps({"ok": True, "hashtags": [f"tag{i}" for i in range(20)]}),
            "hashtags_list")
        self.assertIn("hashtags=20", line)
        self.assertIn("tag0", line)
        self.assertNotIn("tag19", line)

    def test_logs_tail_keeps_count_and_last_line(self):
        line = compact._essence(
            json_dumps({"ok": True, "lines": ["a", "b", "followed @user"]}),
            "logs_tail")
        self.assertEqual(line, "lines=3, last: followed @user")

    def test_plain_text_is_truncated_not_parsed(self):
        line = compact._essence("Waited 30s. Task t1 (follow): 2/5 (street)")
        self.assertEqual(line, "Waited 30s. Task t1 (follow): 2/5 (street)")

    def test_empty_result(self):
        self.assertEqual(compact._essence("{}"), "no result")
        self.assertEqual(compact._essence(""), "no result")


def json_dumps(payload):
    import json
    return json.dumps(payload)


class CompactHistoryTest(unittest.TestCase):
    def _state(self, messages):
        return {"messages": messages}

    def test_nothing_to_compact_returns_empty(self):
        messages = [SystemMessage("sys"), HumanMessage("ciao"),
                    _aim("tutto ok")]
        self.assertEqual(compact.compact_turn_history(self._state(messages)), {})

    def test_keeps_user_messages_and_narrations_whole(self):
        messages = [
            SystemMessage("sys"),
            HumanMessage("aggiungi questi tag"),
            _aim("", [_call("hashtags_add", "c1", {"hashtags": ["street"]})]),
            _tool("hashtags_add", json_dumps({"ok": True, "added": ["street"]}), "c1"),
            _aim("Ho aggiunto street."),
            _aim("", [_call("cycle_status", "c2")]),
            _tool("cycle_status", json_dumps(
                {"task_id": "t1", "kind": "follow", "state": "running",
                 "progress": {"done": 1, "total": 5}}), "c2"),
            _aim("Il task è a 1/5."),
            _aim("", [_call("queue_add", "c3", {"usernames": ["alice"]})]),
            _tool("queue_add", json_dumps({"ok": True, "added": ["alice"]}), "c3"),
            _aim("Ho aggiunto alice alla coda."),
            _aim("", [_call("cycle_status", "c4")]),
            _tool("cycle_status", json_dumps(
                {"task_id": "t1", "kind": "follow", "state": "running",
                 "progress": {"done": 3, "total": 5}}), "c4"),
        ]
        out = compact.compact_turn_history(self._state(messages))["llm_input_messages"]

        # the user message is there, whole
        self.assertTrue(any(isinstance(m, HumanMessage) and m.content ==
                            "aggiungi questi tag" for m in out))
        # the narrations are there, whole
        texts = [m.content for m in out
                 if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)]
        self.assertIn("Ho aggiunto street.", texts)
        self.assertIn("Il task è a 1/5.", texts)
        self.assertIn("Ho aggiunto alice alla coda.", texts)
        # the first round (hashtags_add) is compacted to its essence
        # and its full JSON is gone
        self.assertIn("added=street", texts[0])
        self.assertNotIn('"ok"', texts[0])
        self.assertFalse(any(isinstance(m, ToolMessage) and m.tool_call_id == "c1"
                             for m in out))
        # the last two rounds stay whole: their ToolMessages survive
        for cid in ("c3", "c4"):
            self.assertTrue(any(isinstance(m, ToolMessage) and m.tool_call_id == cid
                                for m in out))

    def test_works_with_state_as_an_object(self):
        messages = [SystemMessage("sys"), HumanMessage("ciao")]
        out = compact.compact_turn_history(
            type("State", (), {"messages": messages})())
        self.assertEqual(out, {})


class _ScriptedModel(BaseChatModel):
    """Replays a script of answers and records every input it receives,
    so a test can see exactly what the pre-model hook feeds the model.

    BaseChatModel is a pydantic model: fields must be declared on the
    class (pydantic deep-copies the defaults, so each instance gets its
    own list)."""

    script: list
    seen: list = []

    def __init__(self, script):
        super().__init__(script=script)
        self.script = list(script)
        self._step = 0

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        answer = self.script[min(self._step, len(self.script) - 1)]
        self._step += 1
        return ChatResult(generations=[ChatGeneration(message=answer)])


@tool
def fake_cycle_status():
    """Poll the fake task."""
    import json
    return json.dumps({"task_id": "t1", "kind": "follow", "state": "running",
                       "progress": {"done": 2, "total": 5, "current": "street"},
                       "last_logs": [{"message": "BIG_MARKER_cycle1"}]})


@tool
def fake_hashtags_add(hashtags: list):
    """Add fake hashtags."""
    import json
    return json.dumps({"ok": True, "added": hashtags})


class PreModelHookIntegrationTest(unittest.TestCase):
    """The hook really changes what the model receives, while the graph
    state keeps the full history."""

    def _run(self, script):
        from langgraph.prebuilt import create_react_agent
        model = _ScriptedModel(script)
        agent = create_react_agent(
            model, [fake_cycle_status, fake_hashtags_add],
            prompt="You are a test agent.",
            pre_model_hook=compact.compact_turn_history)
        states = []
        async def go():
            async for step in agent.astream(
                    {"messages": [HumanMessage("vai")]}, stream_mode="values"):
                states.append(step)
        asyncio.run(go())
        return model, states

    def test_model_receives_the_compacted_view(self):
        script = [
            _aim("", [_call("fake_cycle_status", "c1")]),          # 1: decide
            # narrations ride along with the next decision, the way the
            # real model narrates mid-turn
            _aim("Il task è a 2/5.", [_call("fake_hashtags_add", "c2",
                                            {"hashtags": ["street", "35mm"]})]),
            _aim("Ho aggiunto street e 35mm.", [_call("fake_cycle_status", "c3")]),
            _aim("Il task è a 2/5 ancora.", [_call("fake_cycle_status", "c4")]),
            _aim("Fine."),                                         # final
        ]
        model, states = self._run(script)
        # 5 model calls happened
        self.assertEqual(len(model.seen), 5)
        # while the round ran, the full JSON was in the input (call 2)
        second_input = " ".join(
            getattr(m, "content", "") or "" for m in model.seen[1])
        self.assertIn("BIG_MARKER_cycle1", second_input)
        # at the final call the first two rounds are compacted: their
        # full JSON is gone, replaced by the essence lines - while the
        # narrations they carried are still there, whole. The marker
        # survives only in the two rounds kept whole (c3, c4)
        final = model.seen[-1]
        final_text = " ".join(getattr(m, "content", "") or "" for m in final)
        self.assertEqual(final_text.count("BIG_MARKER_cycle1"), 2)
        self.assertIn("Task t1 (follow): 2/5 (street)", final_text)
        self.assertIn("🔧 fake_cycle_status", final_text)
        self.assertIn("🔧 fake_hashtags_add", final_text)
        self.assertIn("added=street, 35mm", final_text)
        # the user message and all narrations are there, whole
        self.assertTrue(any(isinstance(m, HumanMessage) and m.content == "vai"
                            for m in final))
        self.assertIn("Il task è a 2/5.", final_text)
        self.assertIn("Ho aggiunto street e 35mm.", final_text)
        self.assertIn("Il task è a 2/5 ancora.", final_text)
        # the last two rounds stay whole: their tool calls and JSON survive
        self.assertTrue(any(
            isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
            and m.tool_calls[0]["id"] == "c3" for m in final))
        self.assertTrue(any(isinstance(m, ToolMessage)
                            and m.tool_call_id == "c3" for m in final))
        self.assertTrue(any(isinstance(m, ToolMessage)
                            and m.tool_call_id == "c4" for m in final))
        # the graph state kept the full history all along
        last_state_messages = states[-1]["messages"]
        state_text = " ".join(
            getattr(m, "content", "") or "" for m in last_state_messages)
        self.assertIn("BIG_MARKER_cycle1", state_text)

    def test_no_tool_rounds_does_not_change_the_input(self):
        script = [_aim("Rispondo subito.")]
        model, _ = self._run(script)
        self.assertEqual(len(model.seen), 1)
        self.assertEqual(model.seen[0][0].content, "You are a test agent.")
        self.assertEqual(model.seen[0][1].content, "vai")


if __name__ == "__main__":
    unittest.main()
