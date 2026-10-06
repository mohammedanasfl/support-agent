"""Tests for Part 5: the human approval gates for send_reply and escalate.

No test waits for a person to type. Each test passes a fake input function
that returns the answers we choose, in order.
"""

import json
import runpy
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletion

from support_desk import agent, tools
from support_desk.agent import run_escalate_with_approval, run_send_reply_with_approval
from support_desk.db import connect
from support_desk.guardrails import (
    APPROVAL_QUESTION,
    ESCALATION_QUESTION,
    ESCALATION_REJECTION_MESSAGE,
    REJECTION_MESSAGE,
    is_approved,
)
from tests.test_agent import FakeClient, make_text_reply, make_tool_request_reply

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


# ---------- Helpers ----------

class FakeInput:
    """Stands in for input(). Returns the given answers in order and records
    every question it was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        return self.answers[len(self.questions) - 1]


def no_input_allowed(question):
    """An input function for tests where the human must NOT be asked."""
    raise AssertionError(f"The human was asked unexpectedly: {question}")


def send_reply_arguments(ticket_id, message):
    return json.dumps({"ticket_id": ticket_id, "message": message})


def make_send_reply_request(ticket_id, message, call_id):
    """A fake model reply in which the model asks for send_reply."""
    return ChatCompletion.model_validate({
        "id": "fake-reply", "created": 0, "model": "fake-model", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "send_reply",
                        "arguments": send_reply_arguments(ticket_id, message),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 10},
    })


def count_replies():
    conn = connect(tools.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
    finally:
        conn.close()


def ticket_status(ticket_id):
    conn = connect(tools.DB_PATH)
    try:
        return conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()[0]
    finally:
        conn.close()


# ---------- is_approved ----------

def test_only_y_and_yes_approve():
    for answer in ["y", "yes"]:
        assert is_approved(answer) is True


def test_approval_ignores_case_and_spaces():
    for answer in ["Y", "YES", "Yes", "  y  ", "yes\n"]:
        assert is_approved(answer) is True


def test_everything_else_is_a_rejection():
    for answer in ["", "n", "no", "N", "yep", "ok", "sure", "yes please", "y y", "1"]:
        assert is_approved(answer) is False


# ---------- run_send_reply_with_approval ----------

def test_approved_reply_is_sent_and_stored(capsys):
    fake_input = FakeInput(["y"])

    result = run_send_reply_with_approval(
        send_reply_arguments(20, "Here is how to reset your password."), fake_input
    )

    # The real send_reply ran: it returned its normal result...
    assert json.loads(result)["status"] == "replied"
    # ...stored exactly one reply...
    assert count_replies() == 1
    # ...and changed the ticket status, as send_reply always does.
    assert ticket_status(20) == "replied"

    # The human was shown the ticket id and message, and asked the question.
    shown = capsys.readouterr().out
    assert "--- Proposed reply ---" in shown
    assert "Ticket: 20" in shown
    assert "Here is how to reset your password." in shown
    assert fake_input.questions == [APPROVAL_QUESTION]


def test_rejected_reply_is_not_sent():
    result = run_send_reply_with_approval(
        send_reply_arguments(20, "A reply the human does not like."), FakeInput(["n"])
    )

    assert result == REJECTION_MESSAGE
    assert count_replies() == 0
    assert ticket_status(20) == "open"


def test_send_reply_function_is_not_called_when_rejected(monkeypatch):
    calls = []

    def spy_send_reply(ticket_id, message):
        calls.append((ticket_id, message))
        return "SENT"

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "send_reply", spy_send_reply)

    run_send_reply_with_approval(send_reply_arguments(20, "Draft"), FakeInput(["no"]))
    assert calls == []

    run_send_reply_with_approval(send_reply_arguments(20, "Draft"), FakeInput(["yes"]))
    assert calls == [(20, "Draft")]


def test_unreadable_arguments_are_not_shown_or_sent():
    result = run_send_reply_with_approval("{not json", no_input_allowed)
    assert result.startswith("Error: the arguments for send_reply are not valid JSON")

    result = run_send_reply_with_approval("[20]", no_input_allowed)
    assert result.startswith("Error: the arguments for send_reply must be a JSON object")

    assert count_replies() == 0


# ---------- The gate inside the agent loop ----------

def run_loop(client, fake_input, max_tool_calls=1000):
    return agent.run_agent(
        client=client,
        goal="Triage ticket 20 and reply to the customer.",
        model="fake-model",
        system_prompt="test prompt",
        max_iterations=10,
        max_total_tokens=100_000,
        max_tool_calls=max_tool_calls,
        context_message_threshold=1000,
        context_keep_exchanges=2,
        input_function=fake_input,
    )


def test_reject_then_approve_in_the_loop():
    # The model proposes a reply, the human rejects it, the model proposes a
    # revised reply, the human approves it, and the model finishes.
    client = FakeClient([
        make_send_reply_request(20, "First draft.", call_id="call_1"),
        make_send_reply_request(20, "Revised draft.", call_id="call_2"),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])
    fake_input = FakeInput(["n", "y"])

    result = run_loop(client, fake_input)

    # Rejection did not end the run: the model was called again and finished.
    assert result["stop_reason"] == "final_answer"
    assert len(fake_input.questions) == 2

    tool_messages = []
    for message in result["messages"]:
        if message["role"] == "tool":
            tool_messages.append(message)

    # The rejection reached the model as a normal tool result for call_1...
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[0]["content"] == REJECTION_MESSAGE
    # ...and the approved reply's real send_reply result answered call_2.
    assert tool_messages[1]["tool_call_id"] == "call_2"
    assert json.loads(tool_messages[1]["content"])["status"] == "replied"

    # Only the approved reply was stored.
    conn = connect(tools.DB_PATH)
    stored = conn.execute("SELECT message FROM replies").fetchall()
    conn.close()
    assert stored == [("Revised draft.",)]
    assert ticket_status(20) == "replied"


def test_other_tools_do_not_ask_the_human():
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("Done.", tokens=10),
    ])

    result = run_loop(client, no_input_allowed)

    assert result["stop_reason"] == "final_answer"


# ---------- The escalate gate ----------

def escalate_arguments(ticket_id, reason):
    return json.dumps({"ticket_id": ticket_id, "reason": reason})


def make_escalate_request(ticket_id, reason, call_id):
    """A fake model reply in which the model asks for escalate."""
    return ChatCompletion.model_validate({
        "id": "fake-reply", "created": 0, "model": "fake-model", "object": "chat.completion",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "escalate",
                        "arguments": escalate_arguments(ticket_id, reason),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 10},
    })


def count_escalations():
    conn = connect(tools.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
    finally:
        conn.close()


def test_approved_escalation_is_run(capsys):
    fake_input = FakeInput(["y"])

    result, escalated = run_escalate_with_approval(
        escalate_arguments(25, "Asks for a reset of a colleague's password."), fake_input
    )

    assert result == "Ticket 25 escalated to the human support queue."
    assert escalated is True
    assert count_escalations() == 1
    assert ticket_status(25) == "escalated"

    shown = capsys.readouterr().out
    assert "--- Proposed escalation ---" in shown
    assert "Ticket: 25" in shown
    assert "Asks for a reset of a colleague's password." in shown
    assert fake_input.questions == [ESCALATION_QUESTION]


def test_rejected_escalation_changes_nothing(monkeypatch):
    calls = []

    def spy_escalate(ticket_id, reason):
        calls.append((ticket_id, reason))
        return "ESCALATED"

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "escalate", spy_escalate)

    result, escalated = run_escalate_with_approval(
        escalate_arguments(25, "Needs a human."), FakeInput(["n"])
    )

    assert result == ESCALATION_REJECTION_MESSAGE
    assert escalated is False
    assert calls == []  # the escalate function was never called
    assert count_escalations() == 0
    assert ticket_status(25) == "open"


def test_approved_escalation_that_fails_does_not_count():
    # The human says yes, but the tool itself refuses (no such ticket).
    result, escalated = run_escalate_with_approval(
        escalate_arguments(999, "Needs a human."), FakeInput(["y"])
    )

    assert result.startswith("Error: no ticket with id 999")
    assert escalated is False


def test_unreadable_escalate_arguments_are_not_shown_or_run():
    result, escalated = run_escalate_with_approval("{not json", no_input_allowed)
    assert result.startswith("Error: the arguments for escalate are not valid JSON")
    assert escalated is False
    assert count_escalations() == 0


def test_approved_escalation_ends_the_run_immediately():
    client = FakeClient([
        make_escalate_request(25, "Asks for a reset of a colleague's password.", call_id="call_1"),
        # The model must never get this far: if it were called again, it would
        # ask for get_ticket, and the history would show it.
        make_tool_request_reply(ticket_id=25, tokens=10),
    ])

    result = run_loop(client, FakeInput(["y"]))

    assert result["stop_reason"] == "escalated"
    assert result["final_text"] == "Ticket 25 escalated to the human support queue."
    assert result["iterations"] == 1
    assert client.completions.messages_sent == [2]  # the model was called only once

    # The escalation result is the last message in the history.
    last_message = result["messages"][-1]
    assert last_message["role"] == "tool"
    assert last_message["tool_call_id"] == "call_1"
    assert last_message["content"] == "Ticket 25 escalated to the human support queue."

    assert ticket_status(25) == "escalated"
    assert count_escalations() == 1


def test_rejected_escalation_lets_the_agent_continue():
    client = FakeClient([
        make_escalate_request(25, "Needs a human.", call_id="call_1"),
        make_text_reply("Ticket: 25 ...", tokens=10),
    ])

    result = run_loop(client, FakeInput(["n"]))

    # The run did not stop at the rejection: the model was called again.
    assert result["stop_reason"] == "final_answer"
    assert client.completions.messages_sent == [2, 4]

    tool_message = result["messages"][3]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    assert tool_message["content"] == ESCALATION_REJECTION_MESSAGE

    assert ticket_status(25) == "open"
    assert count_escalations() == 0


def test_rejected_then_approved_escalation():
    client = FakeClient([
        make_escalate_request(25, "Needs a human.", call_id="call_1"),
        make_escalate_request(25, "Asks us to reset another person's password.", call_id="call_2"),
    ])

    result = run_loop(client, FakeInput(["n", "y"]))

    assert result["stop_reason"] == "escalated"
    assert result["iterations"] == 2
    assert client.completions.messages_sent == [2, 4]

    conn = connect(tools.DB_PATH)
    stored = conn.execute("SELECT reason FROM escalations").fetchall()
    conn.close()
    assert stored == [("Asks us to reset another person's password.",)]
