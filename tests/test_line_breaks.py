"""Tests for the line-break fix in replies and escalation reasons.

Gemini sometimes writes a line break in its JSON arguments with one backslash
too many. json.loads then gives the two characters backslash + n instead of a
line break, and the customer would see "\\n" in the reply. These tests check
that the text the human approves, and the text that is saved, has real line
breaks.

In the Python strings below, "\\n" is the two characters backslash + n (what
the model wrongly sends), and "\n" is one real line break.
"""

import json
import runpy
from pathlib import Path

import pytest

from support_desk import tools
from support_desk.agent import (
    fix_escaped_line_breaks,
    run_escalate_with_approval,
    run_send_reply_with_approval,
)
from support_desk.db import connect
from tests.test_guardrails import FakeInput, escalate_arguments, send_reply_arguments

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


def stored_reply():
    conn = connect(tools.DB_PATH)
    try:
        return conn.execute("SELECT message FROM replies").fetchone()[0]
    finally:
        conn.close()


def stored_escalation_reason():
    conn = connect(tools.DB_PATH)
    try:
        return conn.execute("SELECT reason FROM escalations").fetchone()[0]
    finally:
        conn.close()


# ---------- fix_escaped_line_breaks ----------

def test_backslash_n_becomes_a_real_line_break():
    assert fix_escaped_line_breaks("Hi Rahul,\\n\\nThanks.") == "Hi Rahul,\n\nThanks."


def test_text_that_is_already_correct_is_unchanged():
    assert fix_escaped_line_breaks("Hi Rahul,\n\nThanks.") == "Hi Rahul,\n\nThanks."
    assert fix_escaped_line_breaks("One paragraph.") == "One paragraph."


def test_a_missing_or_non_text_argument_is_unchanged():
    assert fix_escaped_line_breaks(None) is None
    assert fix_escaped_line_breaks(17) == 17


# ---------- send_reply ----------

def test_reply_is_shown_and_saved_with_real_line_breaks(capsys):
    # The exact shape the model sent in the ticket 17 run.
    arguments = send_reply_arguments(17, "Hello Rahul,\\n\\nThank you.\\n\\nBest regards,\\nSupport Team")

    result = run_send_reply_with_approval(arguments, FakeInput(["y"]))

    assert json.loads(result)["status"] == "replied"
    # Saved with real line breaks, and no backslash left in the text.
    assert stored_reply() == "Hello Rahul,\n\nThank you.\n\nBest regards,\nSupport Team"
    # The human saw the same, fixed text on separate lines.
    shown = capsys.readouterr().out
    assert "Hello Rahul,\n\nThank you.\n\nBest regards,\nSupport Team" in shown
    assert "\\n" not in shown


def test_correct_reply_is_saved_exactly_as_written():
    arguments = send_reply_arguments(17, "Hello Rahul,\n\nThank you.")

    run_send_reply_with_approval(arguments, FakeInput(["y"]))

    assert stored_reply() == "Hello Rahul,\n\nThank you."


def test_missing_message_still_gives_the_normal_error():
    # The fix must not hide a missing argument: send_reply still reports it.
    result = run_send_reply_with_approval(json.dumps({"ticket_id": 17}), FakeInput(["y"]))

    assert result.startswith("Error: wrong arguments for send_reply")


# ---------- escalate ----------

def test_escalation_reason_is_saved_with_real_line_breaks(capsys):
    arguments = escalate_arguments(17, "Pending charge.\\nBilling must check it.")

    result, escalated = run_escalate_with_approval(arguments, FakeInput(["y"]))

    assert escalated is True
    assert stored_escalation_reason() == "Pending charge.\nBilling must check it."
    assert "\\n" not in capsys.readouterr().out
