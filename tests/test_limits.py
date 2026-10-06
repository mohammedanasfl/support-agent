"""Tests for the Part 5 hard limits: iterations, total tokens, and calls per tool.

All limits are enforced by Python code in agent.py, so these tests use a fake
model that keeps asking for tools, and check that the code stops it.
"""

import json
import runpy
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletion

from support_desk import agent, tools
from support_desk.guardrails import (
    ESCALATION_REJECTION_MESSAGE,
    REJECTION_MESSAGE,
    make_tool_limit_message,
)
from tests.test_agent import FakeClient, make_text_reply, make_tool_request_reply
from tests.test_guardrails import (
    FakeInput,
    count_escalations,
    count_replies,
    no_input_allowed,
    ticket_status,
)

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


# ---------- Helpers ----------

def make_tool_calls_reply(calls, tokens=10):
    """A fake model reply that asks for one or more tools.

    calls is a list of (tool name, arguments dictionary, call id).
    """
    tool_calls = []
    for tool_name, arguments, call_id in calls:
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
        })

    return ChatCompletion.model_validate({
        "id": "fake-reply", "created": 0, "model": "fake-model", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": tokens},
    })


def make_spy_tool(calls, result):
    """Return a fake tool that records the arguments of every call in calls
    and always returns result, so a test can see exactly which calls ran."""
    def spy_tool(**arguments):
        calls.append(arguments)
        return result
    return spy_tool


def run_limited(client, max_iterations=10, max_total_tokens=100_000, max_tool_calls=1000,
                input_function=no_input_allowed):
    # Compaction is switched off (threshold 1000), so only the limits act.
    return agent.run_agent(
        client=client,
        goal="Triage ticket 20.",
        model="fake-model",
        system_prompt="test prompt",
        max_iterations=max_iterations,
        max_total_tokens=max_total_tokens,
        max_tool_calls=max_tool_calls,
        context_message_threshold=1000,
        context_keep_exchanges=2,
        input_function=input_function,
    )


def tool_results(result):
    """Return {tool_call_id: content} for every tool message in the history."""
    results = {}
    for message in result["messages"]:
        if message["role"] == "tool":
            results[message["tool_call_id"]] = message["content"]
    return results


# ---------- A. Iteration limit ----------

def test_iteration_limit_never_makes_an_extra_call(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "not it")
    client = FakeClient([make_tool_request_reply(ticket_id=1, tokens=10)])  # asks forever

    result = run_limited(client, max_iterations=2)

    assert len(client.completions.messages_sent) == 2  # no third call
    assert result["stop_reason"] == "max_iterations"
    assert result["iterations"] == 2
    assert result["final_text"] == (
        "Stopped: the iteration limit of 2 model calls was reached before the "
        "model gave a final answer."
    )


def test_final_answer_on_the_last_allowed_iteration_is_kept(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])

    result = run_limited(client, max_iterations=2)

    assert result["stop_reason"] == "final_answer"
    assert result["final_text"] == "Ticket: 20 ..."


# ---------- B. Token limit ----------

def test_token_limit_stops_before_the_next_call(monkeypatch):
    calls = []
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", make_spy_tool(calls, "not it"))
    client = FakeClient([make_tool_request_reply(ticket_id=1, tokens=400)])  # asks forever

    result = run_limited(client, max_total_tokens=1000)

    # Totals: 400, 800, 1200. The third reply crosses the limit, so the run
    # stops there: no fourth call, and the third reply's tool is not run.
    assert len(client.completions.messages_sent) == 3
    assert len(calls) == 2
    assert result["stop_reason"] == "token_budget"
    # The real total is kept, not cut down to the limit.
    assert result["total_tokens"] == 1200
    assert result["final_text"] == (
        "Stopped: the token limit of 1000 was reached (1200 tokens used) before "
        "the model gave a final answer."
    )


def test_token_limit_reached_exactly_also_stops(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "not it")
    client = FakeClient([make_tool_request_reply(ticket_id=1, tokens=500)])

    result = run_limited(client, max_total_tokens=1000)

    # 500, then 1000: reaching the limit is enough, no third call.
    assert len(client.completions.messages_sent) == 2
    assert result["stop_reason"] == "token_budget"
    assert result["total_tokens"] == 1000


# ---------- C. Per-tool limit ----------

def test_tool_over_its_limit_is_not_run_and_other_tools_still_work(monkeypatch):
    get_ticket_calls = []
    history_calls = []
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", make_spy_tool(get_ticket_calls, "FAKE TICKET"))
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_customer_history", make_spy_tool(history_calls, "FAKE HISTORY"))
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_1")]),
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_2")]),
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_3")]),
        make_tool_calls_reply([("get_customer_history", {"customer_id": 5}, "call_4")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])

    result = run_limited(client, max_tool_calls=2)

    # get_ticket ran twice; the third request was NOT run.
    assert len(get_ticket_calls) == 2
    results = tool_results(result)
    assert results["call_1"] == "FAKE TICKET"
    assert results["call_2"] == "FAKE TICKET"
    # It still got a readable tool result, linked to its request.
    assert results["call_3"] == make_tool_limit_message("get_ticket", 2)
    assert "get_ticket was NOT run" in results["call_3"]

    # Another tool still ran, and the run went on to a normal final answer.
    assert history_calls == [{"customer_id": 5}]
    assert results["call_4"] == "FAKE HISTORY"
    assert result["stop_reason"] == "final_answer"


def test_limit_also_counts_requests_inside_one_reply(monkeypatch):
    calls = []
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", make_spy_tool(calls, "FAKE"))
    client = FakeClient([
        make_tool_calls_reply([
            ("get_ticket", {"ticket_id": 1}, "call_1"),
            ("get_ticket", {"ticket_id": 2}, "call_2"),
            ("get_ticket", {"ticket_id": 3}, "call_3"),
        ]),
        make_text_reply("Done.", tokens=10),
    ])

    result = run_limited(client, max_tool_calls=2)

    assert calls == [{"ticket_id": 1}, {"ticket_id": 2}]
    assert tool_results(result)["call_3"] == make_tool_limit_message("get_ticket", 2)


# ---------- D. Separate counters ----------

def test_each_tool_has_its_own_counter(monkeypatch):
    get_ticket_calls = []
    search_calls = []
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", make_spy_tool(get_ticket_calls, "FAKE"))
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "search_tickets", make_spy_tool(search_calls, "FAKE"))
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_1")]),
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_2")]),
        # get_ticket has now used its 2 calls; search_tickets has used none.
        make_tool_calls_reply([
            ("get_ticket", {"ticket_id": 20}, "call_3"),
            ("search_tickets", {"query": "password"}, "call_4"),
        ]),
        make_text_reply("Done.", tokens=10),
    ])

    result = run_limited(client, max_tool_calls=2)

    results = tool_results(result)
    assert len(get_ticket_calls) == 2
    assert results["call_3"] == make_tool_limit_message("get_ticket", 2)
    # get_ticket being at its limit did not block search_tickets.
    assert search_calls == [{"query": "password"}]
    assert results["call_4"] == "FAKE"


# ---------- E. State-changing tools ----------

def test_send_reply_limit_is_checked_before_the_approval_gate():
    client = FakeClient([
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Draft 1"}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Draft 2"}, "call_2")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Draft 3"}, "call_3")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])
    # Only two answers: if the human were asked a third time, FakeInput would fail.
    fake_input = FakeInput(["n", "n"])

    result = run_limited(client, max_tool_calls=2, input_function=fake_input)

    # The two allowed requests still went through the human gate...
    assert len(fake_input.questions) == 2
    results = tool_results(result)
    assert results["call_1"] == REJECTION_MESSAGE
    assert results["call_2"] == REJECTION_MESSAGE
    # ...and the third was blocked before the human was even asked.
    assert results["call_3"] == make_tool_limit_message("send_reply", 2)
    assert count_replies() == 0
    assert ticket_status(20) == "open"


def test_approved_send_reply_still_runs_within_the_limit():
    client = FakeClient([
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Reply 1"}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Reply 2"}, "call_2")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_limited(client, max_tool_calls=1, input_function=fake_input)

    results = tool_results(result)
    assert json.loads(results["call_1"])["status"] == "replied"
    # The second request is over the limit: not shown to the human, not sent.
    assert len(fake_input.questions) == 1
    assert results["call_2"] == make_tool_limit_message("send_reply", 1)
    assert count_replies() == 1


def test_escalate_limit_is_checked_before_the_approval_gate():
    client = FakeClient([
        make_tool_calls_reply([("escalate", {"ticket_id": 25, "reason": "Needs a human."}, "call_1")]),
        make_tool_calls_reply([("escalate", {"ticket_id": 25, "reason": "Needs a human."}, "call_2")]),
        make_text_reply("Ticket: 25 ...", tokens=10),
    ])
    fake_input = FakeInput(["n"])

    result = run_limited(client, max_tool_calls=1, input_function=fake_input)

    results = tool_results(result)
    assert results["call_1"] == ESCALATION_REJECTION_MESSAGE
    # Blocked without asking the human, and it did NOT end the run.
    assert len(fake_input.questions) == 1
    assert results["call_2"] == make_tool_limit_message("escalate", 1)
    assert result["stop_reason"] == "final_answer"
    assert count_escalations() == 0
    assert ticket_status(25) == "open"


def test_approved_escalate_within_the_limit_still_ends_the_run():
    client = FakeClient([
        make_tool_calls_reply([("escalate", {"ticket_id": 25, "reason": "Needs a human."}, "call_1")]),
        make_text_reply("must never be reached", tokens=10),
    ])

    result = run_limited(client, max_tool_calls=1, input_function=FakeInput(["y"]))

    assert result["stop_reason"] == "escalated"
    assert len(client.completions.messages_sent) == 1
    assert ticket_status(25) == "escalated"
