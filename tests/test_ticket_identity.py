"""Tests for the ticket-identity and action rules (Part 5 behaviour fixes).

What the MODEL decides cannot be tested without a live API call, so these
tests check two things instead:
  - the system prompt really contains the rules (the model can only follow
    rules that are in it), and
  - the code around the model keeps its promises: it never picks a ticket by
    itself, search results never trigger an action, and send_reply and
    escalate still only run after a human approves them.
"""

import json
import runpy
from pathlib import Path

import pytest

from prompts.system_prompt import SYSTEM_PROMPT
from support_desk import agent, tools
from support_desk.guardrails import ESCALATION_REJECTION_MESSAGE, REJECTION_MESSAGE
from tests.test_agent import FakeClient, make_text_reply
from tests.test_guardrails import (
    FakeInput,
    count_escalations,
    count_replies,
    no_input_allowed,
    ticket_status,
)
from tests.test_limits import make_tool_calls_reply, tool_results

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


# The prompt is wrapped over many lines. Joining all whitespace into single
# spaces lets the tests look for a sentence without caring where it wraps.
PROMPT_TEXT = " ".join(SYSTEM_PROMPT.split())


def run_with_prompt(client, goal, input_function):
    """Run the loop with the REAL system prompt and normal limits."""
    return agent.run_agent(
        client=client,
        goal=goal,
        model="fake-model",
        system_prompt=SYSTEM_PROMPT,
        max_iterations=10,
        max_total_tokens=100_000,
        max_tool_calls=3,
        context_message_threshold=1000,
        context_keep_exchanges=2,
        input_function=input_function,
    )


# ---------- The rules are in the system prompt ----------

def test_prompt_forbids_guessing_a_ticket_id():
    assert "never guess which ticket the task is about. If it is missing, ask." in PROMPT_TEXT
    assert "Use a ticket id only when the user's task states it." in PROMPT_TEXT
    assert "Never invent, guess, or pick one yourself" in PROMPT_TEXT
    assert "not an example id, not the newest ticket" in PROMPT_TEXT


def test_prompt_says_to_ask_when_the_ticket_id_is_missing():
    assert (
        "gives no ticket id, do not call any tool, not even search_tickets. "
        "Ask the user for the ticket id."
    ) in PROMPT_TEXT
    assert 'write "Ticket: not provided"' in PROMPT_TEXT


def test_prompt_says_search_results_and_names_do_not_identify_a_ticket():
    assert "They never decide which ticket the task is about." in PROMPT_TEXT
    assert "A customer's name does not identify a customer or a ticket." in PROMPT_TEXT


def test_prompt_lists_every_way_to_ask_for_a_reply():
    assert "reply, respond, send a response, or use send_reply" in PROMPT_TEXT
    assert "Writing the reply or the tool name in your answer does not send anything." in PROMPT_TEXT


def test_prompt_has_escalate_guidance():
    # escalate has its own entry under TOOL GUIDANCE, before ESCALATION GUIDANCE.
    tool_guidance = SYSTEM_PROMPT.index("TOOL GUIDANCE")
    escalate_entry = SYSTEM_PROMPT.index("\nescalate\n")
    escalation_guidance = SYSTEM_PROMPT.index("ESCALATION GUIDANCE")
    assert tool_guidance < escalate_entry < escalation_guidance
    assert "Use it when the task asks you to escalate" in PROMPT_TEXT


def test_prompt_separates_recommending_attempting_and_completing():
    assert "Calling the tool only proposes the action." in PROMPT_TEXT
    assert "Calling escalate is an attempt." in PROMPT_TEXT
    assert "Only a successful escalate result means the ticket was escalated." in PROMPT_TEXT
    assert (
        "Never say a reply was sent or a ticket was escalated unless the tool "
        "result confirms it."
    ) in PROMPT_TEXT


# ---------- 1. An explicit reply request goes to the approval gate ----------

def test_explicit_reply_request_reaches_the_approval_gate():
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Hi Aisha, ..."}, "call_2")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_with_prompt(
        client, "Triage ticket 20 and respond to the customer using send_reply.", fake_input
    )

    # The real prompt was the first message the model received.
    assert client.completions.lists_sent[0][0]["content"] == SYSTEM_PROMPT
    # The model's send_reply request was shown to the human before it ran.
    assert len(fake_input.questions) == 1
    assert json.loads(tool_results(result)["call_2"])["status"] == "replied"
    assert count_replies() == 1


# ---------- 2. An explicit escalation request goes to the approval gate ----------

def test_explicit_escalation_request_reaches_the_approval_gate():
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 25}, "call_1")]),
        make_tool_calls_reply([("escalate", {"ticket_id": 25, "reason": "Asks to reset another person's password."}, "call_2")]),
        make_text_reply("must never be reached", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_with_prompt(client, "Escalate ticket 25 to the human support queue.", fake_input)

    assert len(fake_input.questions) == 1
    assert result["stop_reason"] == "escalated"
    # The text that ends the run is the TOOL's confirmation, not the model's claim.
    assert result["final_text"] == "Ticket 25 escalated to the human support queue."
    assert ticket_status(25) == "escalated"


# ---------- 3 and 4. No ticket id: nothing ticket-specific happens ----------

def test_goal_without_ticket_id_runs_no_ticket_tool():
    # The model follows the prompt and asks for the id. The code adds nothing
    # of its own: no tool runs, no human is asked, and no ticket changes.
    client = FakeClient([
        make_text_reply(
            "Ticket: not provided\n\nCategory: unclear\n\nSummary: The user wants a refund "
            "but gave no ticket id.\n\nNext step: Ask the user for the ticket id.",
            tokens=10,
        ),
    ])

    result = run_with_prompt(client, "Please refund the charge immediately.", no_input_allowed)

    assert result["stop_reason"] == "final_answer"
    assert tool_results(result) == {}
    assert "Ticket: not provided" in result["final_text"]
    assert "ticket id" in result["final_text"]
    assert ticket_status(12) == "open"
    assert count_replies() == 0
    assert count_escalations() == 0


# ---------- 5. Search results do not authorise an action ----------

def test_search_result_does_not_authorize_a_reply():
    # A goal with only a customer name. Even if the model wrongly takes a
    # ticket from the search results, the reply is only a proposal: the human
    # sees which ticket it is for, and rejecting it changes nothing.
    client = FakeClient([
        make_tool_calls_reply([("search_tickets", {"query": "charged twice"}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 28, "message": "Hi Sofia, ..."}, "call_2")]),
        make_text_reply("Ticket: not provided ...", tokens=10),
    ])
    fake_input = FakeInput(["n"])

    result = run_with_prompt(
        client, "A customer named Sofia was charged twice. Reply to her using send_reply.", fake_input
    )

    # The search ran, but by itself it triggered nothing: the human was asked
    # only once, for the model's own send_reply request.
    assert "total_matches" in tool_results(result)["call_1"]
    assert len(fake_input.questions) == 1
    assert tool_results(result)["call_2"] == REJECTION_MESSAGE
    assert count_replies() == 0
    assert ticket_status(28) == "open"


def test_search_result_does_not_authorize_an_escalation():
    client = FakeClient([
        make_tool_calls_reply([("search_tickets", {"query": "charged twice"}, "call_1")]),
        make_tool_calls_reply([("escalate", {"ticket_id": 28, "reason": "Refund needs a person."}, "call_2")]),
        make_text_reply("Ticket: not provided ...", tokens=10),
    ])
    fake_input = FakeInput(["n"])

    result = run_with_prompt(client, "Sofia was charged twice. Escalate it.", fake_input)

    assert len(fake_input.questions) == 1
    assert tool_results(result)["call_2"] == ESCALATION_REJECTION_MESSAGE
    assert result["stop_reason"] == "final_answer"  # not "escalated"
    assert count_escalations() == 0
    assert ticket_status(28) == "open"


# ---------- 6 and 7. Success is only what the tool confirms ----------

def test_approved_reply_to_a_closed_ticket_is_not_a_success():
    # Ticket 1 is closed. Even when the human approves, send_reply refuses,
    # and the model is told so; nothing is stored.
    client = FakeClient([
        make_tool_calls_reply([("send_reply", {"ticket_id": 1, "message": "Thanks for the idea!"}, "call_1")]),
        make_text_reply("Ticket: 1 ...", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_with_prompt(client, "Respond to ticket 1 using send_reply.", fake_input)

    assert len(fake_input.questions) == 1  # approval was still required
    assert tool_results(result)["call_1"].startswith("Error:")
    assert result["stop_reason"] == "final_answer"
    assert count_replies() == 0
    assert ticket_status(1) == "closed"


def test_approved_escalation_that_fails_does_not_end_the_run():
    # Ticket 1 is closed, so escalate refuses even after approval. The run is
    # NOT reported as escalated; the model gets the error and continues.
    client = FakeClient([
        make_tool_calls_reply([("escalate", {"ticket_id": 1, "reason": "Needs a human."}, "call_1")]),
        make_text_reply("Ticket: 1 ...", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_with_prompt(client, "Escalate ticket 1.", fake_input)

    assert len(fake_input.questions) == 1
    assert tool_results(result)["call_1"].startswith("Error:")
    assert result["stop_reason"] == "final_answer"
    assert count_escalations() == 0
