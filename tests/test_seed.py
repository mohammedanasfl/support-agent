"""Tests for scripts/seed.py."""

import runpy
from collections import Counter
from pathlib import Path

import pytest

from support_desk.db import connect

# scripts/ is not a package, so load seed.py by its path. run_path does not
# set __name__ to "__main__", so main() does not run on import.
SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture
def conn(tmp_path):
    """A freshly seeded database in a temporary folder."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_minimum_row_counts(conn):
    assert count(conn, "SELECT COUNT(*) FROM customers") >= 12
    assert count(conn, "SELECT COUNT(*) FROM tickets") >= 25


def test_every_closed_ticket_has_history_and_open_ones_do_not(conn):
    closed = count(conn, "SELECT COUNT(*) FROM tickets WHERE status = 'closed'")
    assert count(conn, "SELECT COUNT(*) FROM ticket_history") == closed
    assert count(conn, """
        SELECT COUNT(*) FROM ticket_history h
        JOIN tickets t ON t.id = h.ticket_id
        WHERE t.status != 'closed'
    """) == 0


def test_running_twice_does_not_duplicate(tmp_path):
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    SEED["seed"](db_path)
    conn = connect(db_path)
    assert count(conn, "SELECT COUNT(*) FROM tickets") == len(SEED["TICKETS"])
    conn.close()


def test_seed_is_deterministic(tmp_path):
    dumps = []
    for name in ("a.db", "b.db"):
        SEED["seed"](tmp_path / name)
        conn = connect(tmp_path / name)
        dumps.append(list(conn.iterdump()))
        conn.close()
    assert dumps[0] == dumps[1]


def test_history_customer_matches_ticket_customer(conn):
    assert count(conn, """
        SELECT COUNT(*) FROM ticket_history h
        JOIN tickets t ON t.id = h.ticket_id
        WHERE h.customer_id != t.customer_id
    """) == 0


def test_timestamps_are_in_a_sensible_order(conn):
    # ISO 8601 strings compare correctly as text.
    assert count(conn, """
        SELECT COUNT(*) FROM tickets t
        JOIN customers c ON c.id = t.customer_id
        WHERE t.created_at < c.signed_up_at
    """) == 0
    assert count(conn, """
        SELECT COUNT(*) FROM ticket_history h
        JOIN tickets t ON t.id = h.ticket_id
        WHERE h.resolved_at < t.created_at
    """) == 0


def test_category_mix():
    categories = Counter(ticket["category"] for ticket in SEED["TICKETS"])
    for name in ("billing", "bug_report", "feature_request", "password_reset"):
        assert categories[name] >= 3
    assert 2 <= categories["ambiguous"] <= 3


def test_injection_ticket_is_open_and_contains_instructions(conn):
    status, body = conn.execute(
        "SELECT status, body FROM tickets WHERE id = ?", (SEED["INJECTION_TICKET_ID"],)
    ).fetchone()
    assert status == "open"
    assert "ignore your previous instructions" in body.lower()
