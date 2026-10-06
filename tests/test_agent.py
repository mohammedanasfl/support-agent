"""Tests for the agent loop.

These use a fake client that returns replies we write ourselves, so the
tests never call the real API and never use up any rate limit.
"""

import json
import runpy
from pathlib import Path

from openai.types.chat import ChatCompletion

from support_desk import agent, tools
from support_desk.agent import run_agent, run_tool
from support_desk.tools import TOOL_DECLARATIONS

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


# ---------- Fake model replies ----------
# Built with the openai SDK's real ChatCompletion type, so they have exactly
# the same shape as the replies the real API returns.

def make_tool_request_reply(ticket_id, tokens):
    """A fake reply in which the model asks for get_ticket(ticket_id)."""
    return ChatCompletion.model_validate({
        "id": "fake-reply", "created": 0, "model": "fake-model", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{ticket_id}",
                    "type": "function",
                    "function": {"name": "get_ticket", "arguments": json.dumps({"ticket_id": ticket_id})},
                }],
            },
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": tokens},
    })


def make_text_reply(text, tokens):
    """A fake reply with a final text answer and no tool requests."""
    return ChatCompletion.model_validate({
        "id": "fake-reply", "created": 0, "model": "fake-model", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": text},
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": tokens},
    })


class FakeCompletions:
    """Stands in for client.chat.completions. Returns our replies in order.

    When it runs out of replies it keeps repeating the last one, which lets a
    test make the model "ask for a tool forever".
    """

    def __init__(self, replies):
        self.replies = replies
        self.messages_sent = []  # how many messages each call received
        self.lists_sent = []  # the exact list object each call received
        self.tools_sent = []  # the tools each call received

    def create(self, model, messages, tools):
        self.messages_sent.append(len(messages))
        self.lists_sent.append(messages)
        self.tools_sent.append(tools)

        call_number = len(self.messages_sent) - 1
        if call_number < len(self.replies):
            return self.replies[call_number]
        return self.replies[-1]


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    """Stands in for OpenAI(), so the code can call client.chat.completions.create."""

    def __init__(self, replies):
        self.completions = FakeCompletions(replies)
        self.chat = FakeChat(self.completions)


def run_with(client, max_iterations=10, max_total_tokens=100_000, max_tool_calls=1000,
             context_message_threshold=1000, context_keep_exchanges=2):
    # The default threshold and tool limit are so high that compaction and the
    # per-tool limit never happen, so these tests check the loop on its own.
    # tests/test_context.py tests compaction, tests/test_limits.py the limits.
    return run_agent(
        client=client,
        goal="Look up ticket 20.",
        model="fake-model",
        system_prompt="test prompt",
        max_iterations=max_iterations,
        max_total_tokens=max_total_tokens,
        max_tool_calls=max_tool_calls,
        context_message_threshold=context_message_threshold,
        context_keep_exchanges=context_keep_exchanges,
    )


# ---------- Tests ----------

def test_simple_ticket_completes_end_to_end(tmp_path, monkeypatch):
    # Real get_ticket against a freshly seeded database; only the model is fake.
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=100),
        make_text_reply("Ticket 20 is a password reset.", tokens=150),
    ])

    result = run_with(client)

    assert result["stop_reason"] == "final_answer"
    assert result["final_text"] == "Ticket 20 is a password reset."
    assert result["iterations"] == 2
    assert result["total_tokens"] == 250

    # The real tool ran: its result (ticket 20 from the database) is in the history.
    tool_message = result["messages"][3]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"])["subject"] == "Forgot my password"


def test_tool_is_executed_and_result_added_to_history(monkeypatch):
    tool_was_called_with = []

    def fake_get_ticket(ticket_id):
        tool_was_called_with.append(ticket_id)
        return "FAKE TICKET 20"

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", fake_get_ticket)
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=100),
        make_text_reply("Done.", tokens=100),
    ])

    result = run_with(client)

    assert tool_was_called_with == [20]
    roles = []
    for message in result["messages"]:
        roles.append(message["role"])
    assert roles == ["system", "user", "assistant", "tool", "assistant"]

    tool_message = result["messages"][3]
    assert tool_message["content"] == "FAKE TICKET 20"
    # The result is linked to the request it answers.
    assert tool_message["tool_call_id"] == result["messages"][2]["tool_calls"][0]["id"]


def test_complete_history_is_sent_on_every_call(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=100),
        make_tool_request_reply(ticket_id=21, tokens=100),
        make_text_reply("Done.", tokens=100),
    ])

    result = run_with(client)

    # Each call sees 2 more messages than the one before: the history grows.
    assert client.completions.messages_sent == [2, 4, 6]
    # Every call received the same, single message list.
    for message_list in client.completions.lists_sent:
        assert message_list is result["messages"]


def test_stops_on_final_answer():
    client = FakeClient([make_text_reply("All done.", tokens=100)])

    result = run_with(client)

    assert result["stop_reason"] == "final_answer"
    assert result["final_text"] == "All done."
    assert result["iterations"] == 1


def test_stops_at_iteration_cap(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "not it")
    client = FakeClient([make_tool_request_reply(ticket_id=1, tokens=100)])  # asks forever

    result = run_with(client, max_iterations=3)

    assert result["stop_reason"] == "max_iterations"
    assert "iteration limit of 3" in result["final_text"]
    assert len(client.completions.messages_sent) == 3  # never more calls than the cap


def test_stops_at_token_budget(monkeypatch):
    tool_was_called_with = []

    def fake_get_ticket(ticket_id):
        tool_was_called_with.append(ticket_id)
        return "not it"

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", fake_get_ticket)
    client = FakeClient([make_tool_request_reply(ticket_id=1, tokens=600)])

    result = run_with(client, max_total_tokens=1000)

    # Call 1: total 600 (under budget). Call 2: total 1200 (over budget) -> stop.
    assert result["stop_reason"] == "token_budget"
    assert result["total_tokens"] == 1200
    assert len(client.completions.messages_sent) == 2
    assert len(tool_was_called_with) == 1  # tools from the last call are not run


def test_stops_on_empty_response():
    client = FakeClient([make_text_reply("", tokens=10)])

    result = run_with(client)

    assert result["stop_reason"] == "empty_response"


def test_tool_declarations_are_sent_on_every_call(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=100),
        make_text_reply("Done.", tokens=100),
    ])

    run_with(client)

    for tools_sent in client.completions.tools_sent:
        assert tools_sent is TOOL_DECLARATIONS


def test_gemini_thought_signature_is_kept_in_the_history(monkeypatch):
    # Gemini 3 returns a thought signature with each tool call and rejects the
    # next call unless it comes back in the history unchanged.
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    reply_with_signature = ChatCompletion.model_validate({
        "id": "fake-reply", "created": 0, "model": "fake-model", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_ticket", "arguments": json.dumps({"ticket_id": 20})},
                    "extra_content": {"google": {"thought_signature": "SIGNATURE"}},
                }],
            },
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 10},
    })
    client = FakeClient([reply_with_signature, make_text_reply("Done.", tokens=10)])

    result = run_with(client)

    tool_call = result["messages"][2]["tool_calls"][0]
    assert tool_call["extra_content"] == {"google": {"thought_signature": "SIGNATURE"}}


def test_tool_calls_without_a_signature_get_no_extra_field(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("Done.", tokens=10),
    ])

    result = run_with(client)

    assert "extra_content" not in result["messages"][2]["tool_calls"][0]


def test_unknown_tool_returns_error_string():
    assert run_tool("delete_everything", "{}") == "Error: unknown tool 'delete_everything'."


def test_invalid_json_arguments_return_error_string():
    assert run_tool("get_ticket", "{ticket_id: 20").startswith("Error: the arguments for get_ticket are not valid JSON")


def test_wrong_argument_name_returns_error_string():
    assert run_tool("get_ticket", '{"id": 3}').startswith("Error: wrong arguments for get_ticket")
