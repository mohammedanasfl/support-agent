"""Tests for the scope rule: the agent handles support desk work only.

Two different cases must stay apart:
  - an unrelated request ("what is api ?") is out of scope: no tools, a short
    redirect instead of an answer;
  - a customer's problem without a ticket id is IN scope: no tools either,
    but the answer asks for the ticket id.

Whether the model follows the rule can only be seen in a live run. These
tests check that the rule is in the prompt, and that the code around the
model behaves correctly in each case: no tool function runs and no human is
asked when the model makes no tool request, and a normal ticket task can
still use the tools and the approval gates.
"""

import json
import runpy
from pathlib import Path

import pytest

from prompts.system_prompt import SYSTEM_PROMPT
from support_desk import agent, tools
from support_desk.tools import TOOL_DECLARATIONS
from tests.test_agent import FakeClient, make_text_reply
from tests.test_guardrails import FakeInput, count_escalations, count_replies, no_input_allowed, ticket_status
from tests.test_limits import make_spy_tool, make_tool_calls_reply, tool_results

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))

PROMPT_TEXT = " ".join(SYSTEM_PROMPT.split())

OUT_OF_SCOPE_GOALS = [
    "what is api ?",
    "Explain Java inheritance.",
    "What's the weather today?",
]

SUPPORT_GOALS_WITHOUT_TICKET_ID = [
    "I don't recognize the charge on my account from yesterday. Please investigate it.",
    "Sofia is having trouble with her CSV export. Please investigate it.",
]


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


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


def replace_every_tool_with_a_spy(monkeypatch):
    """Swap all six tool functions for spies, and return the list that
    records every call. An empty list at the end means no tool ran."""
    calls = []
    for tool_name in list(agent.TOOL_FUNCTIONS.keys()):
        monkeypatch.setitem(agent.TOOL_FUNCTIONS, tool_name, make_spy_tool(calls, "SPY"))
    return calls


# ---------- The rule is in the system prompt ----------

def test_scope_rule_is_part_of_the_role_section():
    # Scope is decided first, so it sits in ROLE, before RULES.
    assert SYSTEM_PROMPT.index("ROLE") < SYSTEM_PROMPT.index("Scope:") < SYSTEM_PROMPT.index("RULES")
    assert "you only handle support desk work" in PROMPT_TEXT
    assert "Before anything else, decide whether the task is support desk work" in PROMPT_TEXT


def test_out_of_scope_tasks_get_no_answer_and_no_tools():
    assert "do not answer it and do not call any tool." in PROMPT_TEXT
    assert "outside the scope of the support desk triage agent" in PROMPT_TEXT
    assert "ask for a support issue or ticket id" in PROMPT_TEXT
    # The redirect replaces the ticket format, which only fits support work.
    assert "If the task is not support desk work (see Scope), do not use the format below" in PROMPT_TEXT


def test_missing_ticket_id_is_not_treated_as_out_of_scope():
    assert "A customer's problem without a ticket id is still support desk work" in PROMPT_TEXT
    # ...and for such a task, no tool at all may be called, not even a search.
    assert "do not call any tool, not even search_tickets." in PROMPT_TEXT


# ---------- Out of scope: no tool runs ----------

def test_out_of_scope_goals_run_no_tool(monkeypatch):
    tool_calls = replace_every_tool_with_a_spy(monkeypatch)

    for goal in OUT_OF_SCOPE_GOALS:
        client = FakeClient([
            make_text_reply(
                "That request is outside the scope of the support desk triage agent. "
                "Please provide a customer support issue or ticket id.",
                tokens=10,
            ),
        ])

        result = run_with_prompt(client, goal, no_input_allowed)

        assert result["stop_reason"] == "final_answer"
        assert len(client.completions.messages_sent) == 1  # one model call, nothing more
        assert tool_results(result) == {}

    assert tool_calls == []  # no tool function ran for any of the goals


# ---------- Support request without a ticket id: no tool runs ----------

def test_support_goals_without_ticket_id_run_no_tool(monkeypatch):
    tool_calls = replace_every_tool_with_a_spy(monkeypatch)

    for goal in SUPPORT_GOALS_WITHOUT_TICKET_ID:
        client = FakeClient([
            make_text_reply(
                "Ticket: not provided\n\nCategory: unclear\n\nSummary: ...\n\n"
                "Next step: Ask the user for the ticket id.",
                tokens=10,
            ),
        ])

        result = run_with_prompt(client, goal, no_input_allowed)

        assert result["stop_reason"] == "final_answer"
        assert tool_results(result) == {}

    # No lookup of any kind: no get_ticket, no search, no customer history.
    assert tool_calls == []
    assert count_replies() == 0
    assert count_escalations() == 0


# ---------- A valid ticket task still works ----------

def test_ticket_task_can_still_use_the_tools_and_the_reply_gate():
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 18}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 18, "message": "Hi Kenji, ..."}, "call_2")]),
        make_text_reply("Ticket: 18 ...", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_with_prompt(client, "Triage ticket 18 and respond to the customer using send_reply.", fake_input)

    # The scope rule is a prompt rule only: every call still offers all tools.
    for tools_sent in client.completions.tools_sent:
        assert tools_sent is TOOL_DECLARATIONS
    # The real get_ticket ran and returned ticket 18.
    assert json.loads(tool_results(result)["call_1"])["subject"] == "Any plans for dark mode?"
    # send_reply still needed the human, and only then ran.
    assert len(fake_input.questions) == 1
    assert json.loads(tool_results(result)["call_2"])["status"] == "replied"
    assert ticket_status(18) == "replied"


def test_ticket_task_can_still_escalate_through_the_gate():
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 12}, "call_1")]),
        make_tool_calls_reply([("escalate", {"ticket_id": 12, "reason": "Recurring export bug needs engineering."}, "call_2")]),
        make_text_reply("must never be reached", tokens=10),
    ])
    fake_input = FakeInput(["y"])

    result = run_with_prompt(
        client,
        "Triage ticket 12 and if it requires engineering investigation, escalate it to a human.",
        fake_input,
    )

    assert len(fake_input.questions) == 1
    assert result["stop_reason"] == "escalated"
    assert result["final_text"] == "Ticket 12 escalated to the human support queue."
    assert ticket_status(12) == "escalated"
