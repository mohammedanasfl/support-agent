"""Tests for the tools, run against a freshly seeded temporary database."""

import json
import runpy
from pathlib import Path

import pytest

from support_desk import tools
from support_desk.db import connect

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


def query_db(sql, params=()):
    """Run one SELECT against the test database and return all rows."""
    conn = connect(tools.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------- get_ticket ----------

def test_get_ticket_returns_ticket_and_customer():
    ticket = json.loads(tools.get_ticket(20))
    assert ticket["subject"] == "Forgot my password"
    assert ticket["customer"]["name"] == "Aisha Bello"
    assert ticket["customer"]["plan"] == "free"


def test_missing_ticket_returns_error_string():
    assert tools.get_ticket(999) == "Error: no ticket with id 999. Check the id and try again."


def test_non_integer_ticket_id_returns_error_string():
    assert tools.get_ticket("20").startswith("Error: ticket_id must be a whole number")


def test_get_ticket_rejects_bool():
    # True is an int in Python, so without the bool check this would read ticket 1.
    assert tools.get_ticket(True).startswith("Error: ticket_id must be a whole number")


# ---------- search_tickets ----------

def test_search_finds_matches_newest_first():
    result = json.loads(tools.search_tickets("Safari"))
    ids = []
    for ticket in result["results"]:
        ids.append(ticket["id"])
    assert ids == [19, 8]  # ticket 19 (September) is newer than ticket 8 (July)
    assert result["total_matches"] == 2
    assert result["shown"] == 2


def test_search_ignores_upper_and_lower_case():
    lower = json.loads(tools.search_tickets("safari"))
    upper = json.loads(tools.search_tickets("SAFARI"))
    assert lower["total_matches"] == 2
    assert upper["total_matches"] == 2


def test_search_with_category_keeps_only_that_category():
    # "twice" appears in billing tickets (2, 17, 28) and in password reset ticket 22.
    all_matches = json.loads(tools.search_tickets("twice"))
    billing_only = json.loads(tools.search_tickets("twice", category="billing"))

    all_ids = []
    for ticket in all_matches["results"]:
        all_ids.append(ticket["id"])
    billing_ids = []
    for ticket in billing_only["results"]:
        billing_ids.append(ticket["id"])

    assert 22 in all_ids
    assert billing_ids == [28, 17, 2]


def test_search_with_no_matches_returns_empty_result_and_note():
    result = json.loads(tools.search_tickets("xylophone"))
    assert result["total_matches"] == 0
    assert result["shown"] == 0
    assert result["results"] == []
    assert "No tickets matched" in result["note"]


def test_search_returns_at_most_five_results():
    # "the" appears in far more than five tickets.
    result = json.loads(tools.search_tickets("the"))
    assert result["total_matches"] > tools.MAX_SEARCH_RESULTS
    assert result["shown"] == tools.MAX_SEARCH_RESULTS
    assert len(result["results"]) == tools.MAX_SEARCH_RESULTS
    assert "Showing the 5 newest" in result["note"]


def test_search_truncates_long_bodies():
    # Ticket 28's body is far longer than SNIPPET_LENGTH.
    result = json.loads(tools.search_tickets("INV-2026-0917"))
    snippet = result["results"][0]["snippet"]
    assert len(snippet) == tools.SNIPPET_LENGTH + len("...")
    assert snippet.endswith("...")


def test_search_keeps_short_bodies_whole():
    result = json.loads(tools.search_tickets("Forgot my password"))
    assert result["results"][0]["snippet"] == (
        "Hi, I forgot my password and can't log in. Can you help me reset it?"
    )


def test_search_rejects_invalid_category():
    assert tools.search_tickets("refund", category="refunds").startswith(
        "Error: unknown category 'refunds'"
    )
    # A list cannot be a dictionary key; it must give an error, not a crash.
    assert tools.search_tickets("refund", category=["billing"]).startswith(
        "Error: unknown category"
    )


def test_search_rejects_empty_query():
    assert tools.search_tickets("   ").startswith("Error: query must not be empty")


def test_search_rejects_non_text_query():
    assert tools.search_tickets(42).startswith("Error: query must be text")


def test_search_rejects_very_long_query():
    assert tools.search_tickets("a" * 101).startswith("Error: query is too long")


# ---------- get_customer_history ----------

def test_customer_history_newest_first():
    result = json.loads(tools.get_customer_history(1))
    assert result["customer"]["name"] == "Priya Raman"
    assert result["customer"]["plan"] == "pro"
    assert result["total_resolved_tickets"] == 2

    ticket_ids = []
    for past_ticket in result["history"]:
        ticket_ids.append(past_ticket["ticket_id"])
    assert ticket_ids == [7, 2]  # dark mode (June) before the double charge (March)


def test_customer_with_no_history_gets_empty_list_and_note():
    result = json.loads(tools.get_customer_history(5))
    assert result["customer"]["name"] == "Aisha Bello"
    assert result["total_resolved_tickets"] == 0
    assert result["history"] == []
    assert result["note"] == "This customer has no resolved tickets yet."


def test_customer_history_is_trimmed():
    # Give customer 1 eight history rows in total, more than MAX_HISTORY_ROWS.
    conn = connect(tools.DB_PATH)
    with conn:
        for day in range(1, 7):
            conn.execute(
                "INSERT INTO ticket_history (customer_id, ticket_id, summary, resolved_at) "
                "VALUES (1, 2, ?, ?)",
                ("x" * 500, f"2026-09-0{day}T10:00:00"),
            )
    conn.close()

    result = json.loads(tools.get_customer_history(1))
    assert result["total_resolved_tickets"] == 8
    assert result["shown"] == tools.MAX_HISTORY_ROWS
    assert len(result["history"]) == tools.MAX_HISTORY_ROWS
    assert len(result["history"][0]["summary"]) == tools.SNIPPET_LENGTH + len("...")


def test_missing_customer_returns_error_string():
    assert tools.get_customer_history(999) == (
        "Error: no customer with id 999. Check the id and try again."
    )


def test_customer_history_rejects_bool_and_text():
    assert tools.get_customer_history(True).startswith("Error: customer_id must be a whole number")
    assert tools.get_customer_history("1").startswith("Error: customer_id must be a whole number")


# ---------- get_refund_policy ----------

def test_refund_policy_for_each_plan():
    for plan in ["free", "pro", "enterprise"]:
        result = json.loads(tools.get_refund_policy(plan))
        assert result["plan"] == plan
        assert result["policy"] == tools.REFUND_POLICIES[plan]


def test_refund_policy_rejects_unknown_plan():
    assert tools.get_refund_policy("platinum") == (
        "Error: unknown plan 'platinum'. Use one of: free, pro, enterprise."
    )
    # Exact values only, as listed in the declaration's enum.
    assert tools.get_refund_policy("Pro").startswith("Error: unknown plan")


def test_refund_policy_rejects_non_text_plan():
    assert tools.get_refund_policy(["pro"]).startswith("Error: plan must be text")


# ---------- send_reply ----------

def test_send_reply_records_the_reply():
    result = json.loads(tools.send_reply(20, "  We have sent you a reset link.  "))

    rows = query_db("SELECT id, ticket_id, message, sent_at FROM replies")
    assert len(rows) == 1
    reply_id, ticket_id, message, sent_at = rows[0]
    assert reply_id == result["reply_id"]
    assert ticket_id == 20
    assert message == "We have sent you a reset link."  # spaces at the ends removed
    assert sent_at == result["sent_at"]


def test_send_reply_changes_ticket_status_to_replied():
    assert query_db("SELECT status FROM tickets WHERE id = 20")[0][0] == "open"
    tools.send_reply(20, "We have sent you a reset link.")
    assert query_db("SELECT status FROM tickets WHERE id = 20")[0][0] == "replied"


def test_send_reply_rejects_missing_ticket():
    assert tools.send_reply(999, "Hello") == (
        "Error: no ticket with id 999. Check the id and try again."
    )
    assert query_db("SELECT COUNT(*) FROM replies")[0][0] == 0


def test_send_reply_rejects_closed_ticket():
    # Ticket 1 is closed in the seed data.
    assert tools.send_reply(1, "Hello") == (
        "Error: ticket 1 is closed, so it cannot get a new reply."
    )
    assert query_db("SELECT COUNT(*) FROM replies")[0][0] == 0
    assert query_db("SELECT status FROM tickets WHERE id = 1")[0][0] == "closed"


def test_send_reply_rejects_empty_message():
    assert tools.send_reply(20, "   ").startswith("Error: message must not be empty")
    assert query_db("SELECT COUNT(*) FROM replies")[0][0] == 0


def test_send_reply_rejects_too_long_message():
    assert tools.send_reply(20, "a" * 2001).startswith("Error: message is too long")
    # Exactly at the limit is fine.
    assert tools.send_reply(20, "a" * 2000).startswith("{")


def test_send_reply_rejects_bool_ticket_id_and_non_text_message():
    assert tools.send_reply(True, "Hello").startswith("Error: ticket_id must be a whole number")
    assert tools.send_reply(20, 123).startswith("Error: message must be text")
    assert query_db("SELECT COUNT(*) FROM replies")[0][0] == 0


# ---------- Declarations and dispatch table ----------

def test_declarations_and_functions_have_the_same_tools():
    declared_names = []
    for declaration in tools.TOOL_DECLARATIONS:
        declared_names.append(declaration["function"]["name"])
    assert sorted(declared_names) == sorted(tools.TOOL_FUNCTIONS.keys())
    assert len(declared_names) == 6


def test_every_declaration_has_description_schema_and_required():
    for declaration in tools.TOOL_DECLARATIONS:
        function = declaration["function"]
        assert declaration["type"] == "function"
        assert function["description"] != ""
        assert function["parameters"]["type"] == "object"
        # Every required argument must also be described in "properties".
        for argument in function["parameters"]["required"]:
            assert argument in function["parameters"]["properties"]


def find_declaration(name):
    for declaration in tools.TOOL_DECLARATIONS:
        if declaration["function"]["name"] == name:
            return declaration["function"]
    return None


def test_enums_match_the_values_the_code_accepts():
    search = find_declaration("search_tickets")
    category_enum = search["parameters"]["properties"]["category"]["enum"]
    assert sorted(category_enum) == sorted(tools.CATEGORY_KEYWORDS.keys())
    assert search["parameters"]["required"] == ["query"]  # category is optional

    refund = find_declaration("get_refund_policy")
    plan_enum = refund["parameters"]["properties"]["plan"]["enum"]
    assert sorted(plan_enum) == sorted(tools.REFUND_POLICIES.keys())


def test_ticket_id_descriptions_give_no_example_id_to_copy():
    # With "e.g. 12" here, a task that named no ticket made the model call
    # get_ticket(12). The descriptions must not offer an id to copy.
    for name in ["get_ticket", "send_reply", "escalate"]:
        description = find_declaration(name)["parameters"]["properties"]["ticket_id"]["description"]
        assert "e.g." not in description
        assert "never guess" in description.lower()


# ---------- escalate ----------

def test_escalate_assigns_ticket_to_human_queue():
    result = tools.escalate(23, "  Customer cannot say what is broken; needs a call.  ")

    assert result == "Ticket 23 escalated to the human support queue."
    assert query_db("SELECT status FROM tickets WHERE id = 23")[0][0] == "escalated"

    rows = query_db("SELECT ticket_id, reason FROM escalations")
    assert rows == [(23, "Customer cannot say what is broken; needs a call.")]


def test_escalate_keeps_the_rest_of_the_ticket():
    before = query_db("SELECT customer_id, subject, body, created_at FROM tickets WHERE id = 23")
    tools.escalate(23, "Needs a human.")
    after = query_db("SELECT customer_id, subject, body, created_at FROM tickets WHERE id = 23")
    assert after == before


def test_escalate_rejects_non_integer_and_bool_ticket_id():
    assert tools.escalate("23", "Needs a human.").startswith("Error: ticket_id must be a whole number")
    assert tools.escalate(True, "Needs a human.").startswith("Error: ticket_id must be a whole number")
    assert query_db("SELECT COUNT(*) FROM escalations")[0][0] == 0


def test_escalate_rejects_empty_or_non_text_reason():
    assert tools.escalate(23, "   ").startswith("Error: reason must not be empty")
    assert tools.escalate(23, None).startswith("Error: reason must be text")
    assert query_db("SELECT status FROM tickets WHERE id = 23")[0][0] == "open"


def test_escalate_rejects_too_long_reason():
    assert tools.escalate(23, "a" * 501).startswith("Error: reason is too long")
    # Exactly at the limit is fine.
    assert tools.escalate(23, "a" * 500) == "Ticket 23 escalated to the human support queue."


def test_escalate_rejects_missing_ticket():
    assert tools.escalate(999, "Needs a human.") == (
        "Error: no ticket with id 999. Check the id and try again."
    )
    assert query_db("SELECT COUNT(*) FROM escalations")[0][0] == 0


def test_escalate_rejects_closed_and_already_escalated_tickets():
    # Ticket 1 is closed in the seed data.
    assert tools.escalate(1, "Needs a human.") == (
        "Error: ticket 1 is closed, so it cannot be escalated."
    )
    tools.escalate(23, "Needs a human.")
    assert tools.escalate(23, "Again.") == (
        "Error: ticket 23 is already in the human support queue."
    )
    assert query_db("SELECT COUNT(*) FROM escalations")[0][0] == 1
