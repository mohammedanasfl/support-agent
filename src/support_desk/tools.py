"""Tools the agent can ask for.

Part 2 has one read-only tool, get_ticket, which is just enough to show the
loop working. Part 3 adds the rest of the tools.

Every tool has two halves:
  1. A Python function that our loop runs.
  2. A declaration (name, description, arguments) that we send to the model,
     so the model knows the tool exists and can ASK for it.

Tools always return a string, even when something is wrong. That way the model
can read the problem and try again, instead of the program crashing.
"""

import json
import sqlite3

from support_desk.db import DEFAULT_DB_PATH, connect

# Which database file the tools read. Tests change this to a temporary file.
DB_PATH = DEFAULT_DB_PATH


def get_ticket(ticket_id):
    """Return one ticket and the customer who sent it, as JSON text."""
    if not isinstance(ticket_id, int):
        return f"Error: ticket_id must be a whole number, e.g. 12. Got {ticket_id!r}."

    conn = connect(DB_PATH)
    # sqlite3.Row lets us read columns by name, e.g. row["subject"].
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT tickets.id, tickets.subject, tickets.body, tickets.status,
                   tickets.created_at, customers.id AS customer_id,
                   customers.name, customers.email, customers.plan
            FROM tickets
            JOIN customers ON customers.id = tickets.customer_id
            WHERE tickets.id = ?
            """,
            (ticket_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return f"Error: no ticket with id {ticket_id}. Check the id and try again."

    ticket = {
        "id": row["id"],
        "subject": row["subject"],
        "body": row["body"],
        "status": row["status"],
        "created_at": row["created_at"],
        "customer": {
            "id": row["customer_id"],
            "name": row["name"],
            "email": row["email"],
            "plan": row["plan"],
        },
    }
    return json.dumps(ticket)


# What the model is told about each tool. The model only ever sees these
# descriptions, never the Python functions themselves.
# "parameters" is a JSON Schema describing the arguments the tool accepts.
TOOL_DECLARATIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_ticket",
            "description": (
                "Look up one support ticket by its numeric id. Returns the ticket's "
                "subject, body, status and created date, plus the customer who sent "
                "it (name, email, plan). Returns an error message if no ticket has "
                "that id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The ticket id, e.g. 12.",
                    },
                },
                "required": ["ticket_id"],
            },
        },
    },
]

# Our tool dispatch table: the tool name the model asks for -> the Python function we run.
TOOL_FUNCTIONS = {
    "get_ticket": get_ticket,
}
