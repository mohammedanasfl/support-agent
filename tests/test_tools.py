"""Tests for the tools, run against a freshly seeded temporary database."""

import json
import runpy
from pathlib import Path

import pytest

from support_desk import tools

SEED = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "seed.py"))


@pytest.fixture(autouse=True)
def seeded_db(tmp_path, monkeypatch):
    """Point the tools at a new seeded database for every test."""
    db_path = tmp_path / "test.db"
    SEED["seed"](db_path)
    monkeypatch.setattr(tools, "DB_PATH", db_path)


def test_get_ticket_returns_ticket_and_customer():
    ticket = json.loads(tools.get_ticket(20))
    assert ticket["subject"] == "Forgot my password"
    assert ticket["customer"]["name"] == "Aisha Bello"
    assert ticket["customer"]["plan"] == "free"


def test_missing_ticket_returns_error_string():
    assert tools.get_ticket(999) == "Error: no ticket with id 999. Check the id and try again."


def test_non_integer_ticket_id_returns_error_string():
    assert tools.get_ticket("20").startswith("Error: ticket_id must be a whole number")
