"""Tests for the fix found with the Part 7 trace (ded38774, ticket 17).

A reply said "I have forwarded your case to our billing team" before any
escalation had happened. Later runs promised a follow-up and then did not
escalate, or the escalation was rejected. The fixes, tested here:
  1. The system prompt says a reply may only claim what really happened, and
     a reply may only say that a team will look at the case when escalate
     follows it (one rule for when to escalate, see the escalate entry).
  2. The human approving a reply sees the tools that really ran in this run.
  3. The human approving an escalation also sees a reply that was already
     sent, so they know what the customer was promised before rejecting.
"""

import runpy
from pathlib import Path

import pytest

from prompts.system_prompt import SYSTEM_PROMPT
from support_desk import agent, tools
from support_desk.guardrails import show_actions_done, show_reply_proposal, tool_was_run
from tests.test_agent import FakeClient, make_text_reply
from tests.test_guardrails import FakeInput
from tests.test_limits import make_tool_calls_reply

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))

PROMPT_TEXT = " ".join(SYSTEM_PROMPT.split())


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


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


def reply_proposals(printed):
    """Split the printed output into one piece per reply proposal."""
    pieces = printed.split("--- Proposed reply ---")
    return pieces[1:]  # pieces[0] is everything before the first proposal


# ---------- 1. The rules are in the system prompt ----------

def test_prompt_says_a_reply_states_only_facts():
    assert "In your answer and in a reply, state only facts the ticket or a tool result shows" in PROMPT_TEXT
    assert "if something is unconfirmed, say so" in PROMPT_TEXT
    assert "Never promise a refund, credit, account change, or outcome." in PROMPT_TEXT


def test_prompt_says_a_reply_claims_only_what_is_done():
    assert (
        "Never say a reply was sent or a ticket was escalated unless the tool "
        "result confirms it."
    ) in PROMPT_TEXT
    assert (
        "never say you or a team noted, logged, recorded, forwarded, or sent "
        "anything that no tool result confirms"
    ) in PROMPT_TEXT


def test_prompt_has_one_rule_for_when_to_escalate():
    # "handle" + a ticket that needs a human: escalate, a recommendation is not enough.
    assert "Then recommending escalation is not enough: call escalate, after any reply the task asks for." in PROMPT_TEXT
    assert "a person still has to follow up with the customer" in PROMPT_TEXT
    # Every other task: only recommend it, and the reply promises no follow-up.
    assert "the task only asks you to triage or to reply), do not call it: recommend escalation in the Next step" in PROMPT_TEXT
    assert "do not tell the customer in a reply that anyone will look at or follow up on the case" in PROMPT_TEXT


def test_output_format_counts_escalate_as_an_action_of_a_handle_task():
    # The model decides how to finish while reading OUTPUT FORMAT. In the Part
    # 8 evals it often only recommended escalating a "handle" task there.
    assert (
        "Give the final answer only after every action the task asks for is done, "
        "including escalate when the task says handle and the ticket needs a human"
    ) in PROMPT_TEXT


def test_prompt_no_longer_has_the_conflicting_hand_over_rule():
    # In the Part 8 evals this rule made the model escalate triage-only tasks
    # after a reply that said "our team will look into this".
    assert "If a reply tells the customer that a team will look at their case" not in PROMPT_TEXT


def test_prompt_no_longer_gives_a_hand_over_wording_to_copy():
    # The model copied this example and then did not escalate.
    assert "I am passing your case to our billing team" not in PROMPT_TEXT


# ---------- 2. The human sees what really ran ----------

def test_reply_proposal_lists_the_tools_that_ran(capsys):
    client = FakeClient([
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Hello."}, "call_2")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])

    run_loop(client, FakeInput(["y"]))

    proposal = reply_proposals(capsys.readouterr().out)[0]
    assert "Done so far in this run:" in proposal
    assert '- get_ticket({"ticket_id": 20})' in proposal
    assert "the ticket has NOT been escalated or forwarded to anyone" in proposal


def test_failed_rejected_and_blocked_calls_are_not_listed(capsys):
    client = FakeClient([
        # get_ticket on a ticket that does not exist: an error, nothing done.
        make_tool_calls_reply([("get_ticket", {"ticket_id": 999}, "call_1")]),
        # A second get_ticket is blocked by the limit of 1 call per tool.
        make_tool_calls_reply([("get_ticket", {"ticket_id": 20}, "call_2")]),
        # The first reply is rejected, so it was not sent.
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Draft 1."}, "call_3")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])

    run_loop(client, FakeInput(["n"]), max_tool_calls=1)

    proposal = reply_proposals(capsys.readouterr().out)[0]
    # Nothing really ran before the reply was proposed.
    assert "(nothing yet)" in proposal
    assert "- get_ticket" not in proposal


def test_a_rejected_reply_is_not_listed_as_done(capsys):
    client = FakeClient([
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Draft 1."}, "call_1")]),
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Draft 2."}, "call_2")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])

    run_loop(client, FakeInput(["n", "y"]))

    proposals = reply_proposals(capsys.readouterr().out)
    assert len(proposals) == 2
    # When Draft 2 is proposed, the rejected Draft 1 is not listed as sent.
    assert "- send_reply" not in proposals[1]


def test_an_escalation_in_the_list_is_not_reported_as_missing(capsys):
    actions_done = ['get_ticket({"ticket_id": 20})', 'escalate({"ticket_id": 20, "reason": "r"})']

    show_reply_proposal(20, "Hello.", actions_done)

    printed = capsys.readouterr().out
    assert '- escalate({"ticket_id": 20, "reason": "r"})' in printed
    assert "has NOT been escalated" not in printed


def test_empty_list_says_nothing_was_done(capsys):
    show_actions_done([])

    assert "(nothing yet)" in capsys.readouterr().out


def test_tool_was_run_matches_the_whole_tool_name():
    actions_done = ['get_ticket({"ticket_id": 20})', 'send_reply({"ticket_id": 20})']

    assert tool_was_run(actions_done, "send_reply") is True
    assert tool_was_run(actions_done, "escalate") is False
    assert tool_was_run(actions_done, "get") is False  # only whole names count


# ---------- 3. The escalation screen shows a reply that was already sent ----------

def test_escalation_proposal_shows_the_reply_already_sent(capsys):
    client = FakeClient([
        make_tool_calls_reply([("send_reply", {"ticket_id": 20, "message": "Billing will check."}, "call_1")]),
        make_tool_calls_reply([("escalate", {"ticket_id": 20, "reason": "Needs billing."}, "call_2")]),
        make_text_reply("Ticket: 20 ...", tokens=10),
    ])

    run_loop(client, FakeInput(["y", "n"]))

    printed = capsys.readouterr().out
    escalation = printed.split("--- Proposed escalation ---")[1]
    assert "Done so far in this run:" in escalation
    # The reply that was sent is listed with its message, so the human can
    # see what the customer was told before rejecting the escalation.
    assert "- send_reply(" in escalation
    assert "Billing will check." in escalation
    assert "a reply was already sent to the customer" in escalation


def test_escalation_proposal_without_a_reply_has_no_reply_note(capsys):
    client = FakeClient([
        make_tool_calls_reply([("escalate", {"ticket_id": 20, "reason": "Needs billing."}, "call_1")]),
    ])

    run_loop(client, FakeInput(["y"]))

    escalation = capsys.readouterr().out.split("--- Proposed escalation ---")[1]
    assert "(nothing yet)" in escalation
    assert "a reply was already sent" not in escalation
