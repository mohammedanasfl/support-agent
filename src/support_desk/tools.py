"""Tools the agent can ask for.

There are five tools:
    get_ticket            read one ticket and the customer who sent it
    search_tickets        find tickets by keyword, optionally in one category
    get_customer_history  read a customer's past resolved tickets
    get_refund_policy     read the refund rules for one plan
    send_reply            record a reply to a ticket (the only tool that writes)

Every tool has two halves:
  1. A Python function that our loop runs.
  2. A declaration (name, description, arguments) that we send to the model,
     so the model knows the tool exists and can ASK for it.

Tools always return a string, even when something is wrong. That way the model
can read the problem and try again, instead of the program crashing.

Every argument is checked before the database is used. The declarations tell
the model which values are allowed, but the model can still send anything, so
the checks in Python are what actually protect the database.
"""

import json
import sqlite3
from datetime import datetime

from support_desk.db import DEFAULT_DB_PATH, connect

# Which database file the tools read. Tests change this to a temporary file.
DB_PATH = DEFAULT_DB_PATH

# Limits that keep tool results small. Every tool result stays in the message
# history and is sent to the model again on every later call, so a large
# result costs tokens again and again.
MAX_SEARCH_RESULTS = 5  # the most tickets search_tickets returns
MAX_HISTORY_ROWS = 5  # the most past tickets get_customer_history returns
SNIPPET_LENGTH = 200  # longer ticket bodies and summaries are cut to this length
MAX_QUERY_LENGTH = 100  # the longest search text search_tickets accepts
MAX_REPLY_LENGTH = 2000  # the longest reply send_reply accepts

# The database has no category column, because deciding what kind of problem a
# ticket is, is the agent's job. So search_tickets filters by category with
# these keywords instead: a ticket counts as "billing" if its subject or body
# contains at least one billing keyword. This is a rough filter, not a perfect
# classifier. Keywords are lowercase because we compare against lowercase text.
CATEGORY_KEYWORDS = {
    "billing": ["charge", "refund", "invoice", "billing", "payment", "subscription", "credit"],
    "bug_report": ["bug", "error", "crash", "broken", "blank", "not working", "not supported", "missing"],
    "feature_request": ["feature", "would love", "would it be possible", "any plans", "do you support", "roadmap"],
    "password_reset": ["password", "reset", "locked"],
}

# The refund rules for each plan. The keys are the same three plans that the
# customers table allows. The rules agree with the seeded ticket_history:
# ticket 2 (a duplicate $29 Pro charge was refunded) and ticket 6 (an
# accidental annual upgrade was refunded when asked about within 3 days).
REFUND_POLICIES = {
    "free": (
        "The free plan has no charges, so there is nothing to refund. If a "
        "free-plan customer reports a charge from us, do not promise a refund: "
        "the billing team must first find out what the charge is."
    ),
    "pro": (
        "Pro costs $29 per month, or can be billed yearly. A duplicate charge "
        "for the same period is refunded in full. An accidental switch to annual "
        "billing is refunded in full if the customer asks within 14 days of the "
        "charge, and the account goes back to monthly billing. Other payments "
        "are not refunded: the customer can cancel at any time and keeps access "
        "until the end of the period they paid for. Refunds go back to the "
        "original card."
    ),
    "enterprise": (
        "Enterprise billing follows each customer's signed contract. Support "
        "cannot issue Enterprise refunds or credits, including credits for "
        "removed seats: these are handled by the customer's account manager, so "
        "the request must be passed on to them."
    ),
}


# ---------- Small helpers used by the tools ----------

def is_whole_number(value):
    """Return True if value is an int such as 12, and False for anything else.

    In Python, bool is a kind of int: isinstance(True, int) is True. Without
    the first check, a model sending {"ticket_id": true} would be accepted and
    treated as ticket 1. So True and False are rejected first.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, int)


def make_snippet(text):
    """Cut text down to SNIPPET_LENGTH characters, adding "..." if it was cut."""
    if len(text) <= SNIPPET_LENGTH:
        return text
    return text[:SNIPPET_LENGTH] + "..."


def has_category_keyword(subject, body, category):
    """Return True if the subject or body contains any keyword of the category."""
    # .lower() so that "Refund" and "refund" both match the keyword "refund".
    text = (subject + " " + body).lower()
    for keyword in CATEGORY_KEYWORDS[category]:
        if keyword in text:
            return True
    return False


def open_database():
    """Connect to the tools' database, with rows readable by column name."""
    conn = connect(DB_PATH)
    # sqlite3.Row lets us read columns by name, e.g. row["subject"].
    conn.row_factory = sqlite3.Row
    return conn


# ---------- The five tools ----------

def get_ticket(ticket_id):
    """Return one ticket and the customer who sent it, as JSON text.

    The full body is returned (no snippet), because reading the whole ticket is
    the reason to call this tool.

    Why the description is worded this way:
    - "numeric id" and "e.g. 12" show the exact form of the id, so the model
      sends 12 and not "#12" or "ticket 12".
    - It lists everything that comes back, including the customer's id and
      plan. The model then knows it already has what get_customer_history and
      get_refund_policy need, and does not have to guess them.
    - It says an unknown id gives an error message, so the model expects that
      case and can correct the id instead of stopping.
    """
    if not is_whole_number(ticket_id):
        return f"Error: ticket_id must be a whole number, e.g. 12. Got {ticket_id!r}."

    conn = open_database()
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


def search_tickets(query, category=None):
    """Find tickets whose subject or body contains the query, as JSON text.

    The query is matched as one piece of text, ignoring upper/lower case:
    "safari" finds "Safari", but "Safari blank" only finds tickets that contain
    exactly "safari blank". If a category is given, only tickets that also
    contain one of that category's keywords are kept.

    At most MAX_SEARCH_RESULTS tickets are returned, newest first, each with a
    short snippet of its body instead of the full body. total_matches and shown
    tell the model when some matches were left out.

    Why the description is worded this way (this is the description I rewrote,
    see docs/tool-description-note.md):
    - It explains that the query is matched as ONE exact piece of text, with an
      example of a search that finds nothing. The first, short description
      did not say this, and the model searched with whole phrases such as
      "Safari dashboard blank", which missed the earlier ticket.
    - It says to retry with a single different word when nothing is found,
      which is the step the model most often skipped.
    - It says results are limited to 5 snippets plus total_matches and shown,
      so the model knows a result may be incomplete and that get_ticket gives
      the full text.
    - category is described as "Optional ... leave it out", so the model does
      not feel it must pick one; a wrong category would hide relevant tickets.
    """
    if not isinstance(query, str):
        return f"Error: query must be text, e.g. \"refund\". Got {query!r}."
    query = query.strip()
    if query == "":
        return "Error: query must not be empty. Give a keyword, e.g. \"refund\"."
    if len(query) > MAX_QUERY_LENGTH:
        return (
            f"Error: query is too long ({len(query)} characters, the limit is "
            f"{MAX_QUERY_LENGTH}). Use one or two keywords."
        )

    # category is optional: None means "do not filter by category".
    if category is not None:
        # Check for text first: a list such as ["billing"] cannot be looked up
        # in a dictionary and would raise a TypeError.
        if not isinstance(category, str) or category not in CATEGORY_KEYWORDS:
            return (
                f"Error: unknown category {category!r}. Use one of: billing, "
                f"bug_report, feature_request, password_reset, or leave it out."
            )

    # LIKE '%refund%' matches "refund" anywhere in the text, and SQLite's LIKE
    # ignores upper/lower case for English letters. The ? placeholders keep the
    # query as plain data, so it can never change the SQL itself.
    # (A % or _ inside the query also acts as a LIKE wildcard; that is harmless here.)
    pattern = "%" + query + "%"

    conn = open_database()
    try:
        rows = conn.execute(
            """
            SELECT id, customer_id, subject, body, status, created_at
            FROM tickets
            WHERE subject LIKE ? OR body LIKE ?
            ORDER BY created_at DESC, id DESC
            """,
            (pattern, pattern),
        ).fetchall()
    finally:
        conn.close()

    # Keep only the tickets in the requested category (all of them if none was given).
    matches = []
    for row in rows:
        if category is None:
            matches.append(row)
        elif has_category_keyword(row["subject"], row["body"], category):
            matches.append(row)

    # Trim: the newest few matches only, and a snippet instead of the full body.
    results = []
    for row in matches[:MAX_SEARCH_RESULTS]:
        results.append({
            "id": row["id"],
            "subject": row["subject"],
            "status": row["status"],
            "created_at": row["created_at"],
            "customer_id": row["customer_id"],
            "snippet": make_snippet(row["body"]),
        })

    search_result = {
        "query": query,
        "category": category,
        "total_matches": len(matches),
        "shown": len(results),
        "results": results,
    }
    if len(matches) == 0:
        search_result["note"] = (
            "No tickets matched. Try a shorter query, a different word, or no category."
        )
    elif len(matches) > len(results):
        search_result["note"] = (
            f"Showing the {len(results)} newest of {len(matches)} matches. "
            f"Use a more specific query to narrow them down."
        )
    return json.dumps(search_result)


def get_customer_history(customer_id):
    """Return a customer and their past resolved tickets, as JSON text.

    Past tickets come from the ticket_history table, newest first. At most
    MAX_HISTORY_ROWS are returned, with each summary cut to a snippet, plus the
    total count. A customer with no history gets an empty list and a note:
    that is not an error, the customer simply has no resolved tickets yet.

    Why the description is worded this way:
    - "Use it to check whether the customer has had the same problem before"
      tells the model WHEN the tool is useful, not only what it returns.
    - "The customer id is in get_ticket's result" matters because ticket ids
      and customer ids are both small numbers that overlap (ticket 5 and
      customer 5 both exist). If the model passed a ticket id by mistake, it
      would get the wrong customer's history and no error to warn it.
    - It says an empty history means "no resolved tickets yet", so the model
      treats it as an answer and does not keep retrying.
    """
    if not is_whole_number(customer_id):
        return f"Error: customer_id must be a whole number, e.g. 5. Got {customer_id!r}."

    conn = open_database()
    try:
        customer_row = conn.execute(
            "SELECT id, name, email, plan, signed_up_at FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        history_rows = conn.execute(
            """
            SELECT ticket_history.ticket_id, tickets.subject,
                   ticket_history.summary, ticket_history.resolved_at
            FROM ticket_history
            JOIN tickets ON tickets.id = ticket_history.ticket_id
            WHERE ticket_history.customer_id = ?
            ORDER BY ticket_history.resolved_at DESC, ticket_history.id DESC
            """,
            (customer_id,),
        ).fetchall()
    finally:
        conn.close()

    if customer_row is None:
        return f"Error: no customer with id {customer_id}. Check the id and try again."

    # Trim: the newest few past tickets only, each with a summary snippet.
    history = []
    for row in history_rows[:MAX_HISTORY_ROWS]:
        history.append({
            "ticket_id": row["ticket_id"],
            "subject": row["subject"],
            "summary": make_snippet(row["summary"]),
            "resolved_at": row["resolved_at"],
        })

    customer_history = {
        "customer": {
            "id": customer_row["id"],
            "name": customer_row["name"],
            "email": customer_row["email"],
            "plan": customer_row["plan"],
            "signed_up_at": customer_row["signed_up_at"],
        },
        "total_resolved_tickets": len(history_rows),
        "shown": len(history),
        "history": history,
    }
    if len(history_rows) == 0:
        customer_history["note"] = "This customer has no resolved tickets yet."
    elif len(history_rows) > len(history):
        customer_history["note"] = (
            f"Showing the {len(history)} most recent of {len(history_rows)} resolved tickets."
        )
    return json.dumps(customer_history)


def get_refund_policy(plan):
    """Return the refund rules for one plan, as JSON text.

    The rules come from the REFUND_POLICIES dictionary above, not from the
    database, because they are fixed text that never changes during a run.

    Why the description is worded this way:
    - "Use the plan stored in our records, as shown by get_ticket or
      get_customer_history" is there because customers are not always right
      about their own plan (ticket 13 says "I don't think I ever upgraded").
      The policy must be chosen from the database, not from the ticket text.
    - The enum lists the three exact plan names, so the model sends "pro",
      not "Pro" or "premium", which the code would reject.
    """
    # Check for text first: a list such as ["pro"] cannot be looked up in a
    # dictionary and would raise a TypeError.
    if not isinstance(plan, str):
        return f"Error: plan must be text: free, pro, or enterprise. Got {plan!r}."
    if plan not in REFUND_POLICIES:
        return f"Error: unknown plan {plan!r}. Use one of: free, pro, enterprise."

    return json.dumps({"plan": plan, "policy": REFUND_POLICIES[plan]})


def send_reply(ticket_id, message):
    """Record a reply to a ticket and mark the ticket as 'replied'.

    Nothing is emailed: "sending" means saving the reply in the replies table.
    The reply is saved and the status changed in one transaction, so the
    database never ends up with one change and not the other.

    This is the only tool that changes data. Human approval before it runs
    belongs to Part 5; this function only checks its arguments and writes.

    Why the description is worded this way:
    - It says what calling it changes (the reply is recorded and the status
      becomes 'replied'), because this is the only tool with an effect. The
      model should not treat it like a harmless lookup.
    - "Write the complete text the customer will read" stops the model from
      passing a note to itself, such as "tell them how to reset".
    - It states the 2000-character limit and that closed tickets cannot get
      replies, so the model avoids calls that would only return an error.
    - "Only call this after reading the ticket and checking what to say"
      asks the model to look things up first and reply last.
    """
    if not is_whole_number(ticket_id):
        return f"Error: ticket_id must be a whole number, e.g. 12. Got {ticket_id!r}."
    if not isinstance(message, str):
        return f"Error: message must be text. Got {message!r}."
    message = message.strip()
    if message == "":
        return "Error: message must not be empty. Write the reply the customer will read."
    if len(message) > MAX_REPLY_LENGTH:
        return (
            f"Error: message is too long ({len(message)} characters, the limit is "
            f"{MAX_REPLY_LENGTH}). Shorten the reply."
        )

    conn = open_database()
    try:
        ticket_row = conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if ticket_row is None:
            return f"Error: no ticket with id {ticket_id}. Check the id and try again."
        if ticket_row["status"] == "closed":
            return f"Error: ticket {ticket_id} is closed, so it cannot get a new reply."

        # ISO 8601 text, e.g. '2026-10-04T14:05:00', like every other date we store.
        sent_at = datetime.now().isoformat(timespec="seconds")

        # "with conn:" is one transaction: if either statement fails, neither
        # change is saved. If both succeed, both are saved together.
        with conn:
            cursor = conn.execute(
                "INSERT INTO replies (ticket_id, message, sent_at) VALUES (?, ?, ?)",
                (ticket_id, message, sent_at),
            )
            reply_id = cursor.lastrowid  # the id SQLite gave the new reply row
            conn.execute(
                "UPDATE tickets SET status = 'replied' WHERE id = ?", (ticket_id,)
            )
    finally:
        conn.close()

    return json.dumps({
        "reply_id": reply_id,
        "ticket_id": ticket_id,
        "status": "replied",
        "sent_at": sent_at,
    })


# ---------- What the model is told about each tool ----------
# The model only ever sees these declarations, never the Python functions.
# "parameters" is a JSON Schema describing the arguments the tool accepts.
# "required" lists the arguments the model must always send. "enum" lists the
# only values an argument may have. The names must match TOOL_FUNCTIONS below.
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
    {
        "type": "function",
        "function": {
            "name": "search_tickets",
            # Rewritten after testing: see docs/tool-description-note.md.
            "description": (
                "Search all tickets, open and closed, for a keyword in the subject or "
                "body. The query is matched as ONE exact piece of text (ignoring "
                "upper/lower case), not as separate words: 'Safari' finds every ticket "
                "that mentions Safari, but 'Safari dashboard blank' only finds tickets "
                "containing those three words in that exact order. So pass one or two "
                "keywords, not a sentence, and if a search finds nothing, try a single "
                "different word. Optionally pass a category to keep only that kind of "
                "ticket. Returns at most 5 tickets, newest first, each with a short "
                "snippet of its body, plus total_matches and shown. Call get_ticket to "
                "read a whole ticket."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "One keyword or a short phrase, e.g. 'Safari' or 'refund'. "
                            "Not a full sentence."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": ["billing", "bug_report", "feature_request", "password_reset"],
                        "description": (
                            "Optional. Only return tickets of this kind. Leave it out to "
                            "search every ticket."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_history",
            "description": (
                "Look up a customer by their numeric id and list their past resolved "
                "tickets, newest first: ticket id, subject, a summary of how it was "
                "resolved, and when. Use it to check whether the customer has had the "
                "same problem before. The customer id is in get_ticket's result. "
                "Returns at most 5 past tickets plus the total count; an empty history "
                "means the customer has no resolved tickets yet. Returns an error "
                "message if no customer has that id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "integer",
                        "description": "The customer id, e.g. 5.",
                    },
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_refund_policy",
            "description": (
                "Return the refund rules for one subscription plan. Use the plan "
                "stored in our records, as shown by get_ticket or "
                "get_customer_history. Returns an error message for an unknown plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "enum": ["free", "pro", "enterprise"],
                        "description": "The customer's plan.",
                    },
                },
                "required": ["plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_reply",
            "description": (
                "Send a reply to the customer who opened a ticket. The reply is "
                "recorded on the ticket and the ticket's status becomes 'replied'. "
                "Write the complete text the customer will read (at most 2000 "
                "characters). Only call this after reading the ticket and checking "
                "what to say. Closed tickets cannot get replies. Returns the saved "
                "reply's id, or an error message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "integer",
                        "description": "The id of the ticket to reply to, e.g. 12.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The full reply text for the customer.",
                    },
                },
                "required": ["ticket_id", "message"],
            },
        },
    },
]

# Our tool dispatch table: the tool name the model asks for -> the Python function we run.
TOOL_FUNCTIONS = {
    "get_ticket": get_ticket,
    "search_tickets": search_tickets,
    "get_customer_history": get_customer_history,
    "get_refund_policy": get_refund_policy,
    "send_reply": send_reply,
}
