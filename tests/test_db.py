"""Tests for the database schema."""

import sqlite3

import pytest

from support_desk.db import connect, create_schema, table_names


@pytest.fixture
def conn():
    """An in-memory database with the schema, discarded after each test."""
    conn = connect(":memory:")
    create_schema(conn)
    yield conn
    conn.close()


def test_creates_all_five_tables(conn):
    assert table_names(conn) == ["customers", "escalations", "replies", "ticket_history", "tickets"]


def test_create_schema_can_run_twice(conn):
    create_schema(conn)
    assert table_names(conn) == ["customers", "escalations", "replies", "ticket_history", "tickets"]


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tickets (customer_id, subject, body, created_at) "
            "VALUES (999, 'Hi', 'No such customer', '2025-01-01T00:00:00')"
        )


def test_plan_must_be_a_known_value(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO customers (name, email, plan, signed_up_at) "
            "VALUES ('Ada', 'ada@example.com', 'platinum', '2025-01-01T00:00:00')"
        )
