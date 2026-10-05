"""SQLite connection and schema for customers, tickets, ticket history, and replies.

This module only creates the database structure. Seed data belongs in
scripts/seed.py, and the queries used by the tools will be added later.
"""

import sqlite3
from pathlib import Path

# This file is src/support_desk/db.py, so parents[2] is the project root.
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "support_desk.db"

# Dates are stored as ISO 8601 text (e.g. '2025-03-14T09:30:00') because
# SQLite has no date type, and ISO strings sort in chronological order.
SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    email        TEXT    NOT NULL UNIQUE,
    plan         TEXT    NOT NULL CHECK (plan IN ('free', 'pro', 'enterprise')),
    signed_up_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    subject     TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'replied', 'escalated', 'closed')),
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_history (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    ticket_id   INTEGER NOT NULL REFERENCES tickets (id),
    summary     TEXT    NOT NULL,
    resolved_at TEXT    NOT NULL
);

-- Replies recorded by the send_reply tool. Nothing is emailed: a row here IS
-- the sent reply.
CREATE TABLE IF NOT EXISTS replies (
    id        INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets (id),
    message   TEXT    NOT NULL,
    sent_at   TEXT    NOT NULL
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with foreign key enforcement turned on.

    SQLite ignores foreign keys unless this pragma is set, and the setting
    only lasts for one connection, so every connection must come from here.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the four tables. Safe to run twice: existing tables are kept."""
    conn.executescript(SCHEMA)


def table_names(conn: sqlite3.Connection) -> list[str]:
    """Return the names of the tables in the database, sorted alphabetically."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def create_database(db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    """Create the database file (and its folder) with an empty schema.

    Returns the table names so the caller can confirm what was created.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        create_schema(conn)
        return table_names(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    tables = create_database()
    print(f"Database: {DEFAULT_DB_PATH}")
    print(f"Tables:   {', '.join(tables)}")
