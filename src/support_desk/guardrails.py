"""Guardrails enforced in code, not by asking the model nicely in the prompt.

Part 5: the human approval gates for the two tools that change data,
send_reply and escalate. The model can only ASK for them. Before our code
runs one, a person at the terminal sees what the model proposes and must type
"y" or "yes". Anything else is a rejection, and nothing is changed.

The functions here only deal with the human side: showing the proposal and
reading the answer. agent.py decides when to call them.

Also the message for the per-tool call limit: agent.py counts how often each
tool was called, and once a tool reaches the limit, the model gets this
message instead of the tool running.
"""

# The tool result the model gets when the human says no. It is returned as a
# normal "tool" message, so the run continues and the model can try again.
REJECTION_MESSAGE = (
    "Human rejected the proposed reply. The reply was NOT sent. "
    "Please revise the reply and propose it again."
)

APPROVAL_QUESTION = "Approve this reply? [y/N]: "

# The same two things for escalate. The rejection tells the model it may keep
# working on the ticket, because a rejected escalation does not end the run.
ESCALATION_REJECTION_MESSAGE = (
    "Human rejected the proposed escalation. The ticket was NOT escalated. "
    "Continue triaging the ticket yourself, or propose escalation again with a "
    "clearer reason."
)

ESCALATION_QUESTION = "Approve this escalation? [y/N]: "


def make_tool_limit_message(tool_name, max_tool_calls):
    """The tool result the model gets when a tool has reached its call limit.

    It starts with "Error:" like every other tool error, says clearly that
    the tool was NOT run, and points the model at what it can still do, so
    the run can continue with other tools or a final answer.
    """
    return (
        f"Error: {tool_name} was NOT run. It has already been called "
        f"{max_tool_calls} times in this run, which is the limit for one tool. "
        f"Use the information you already have, use a different tool, or give "
        f"your final answer."
    )


def is_approved(answer):
    """Return True only for an explicit yes: "y" or "yes".

    Upper/lower case and spaces around the answer are ignored, so "Y" and
    " yes " also approve. Everything else is a no, including an empty answer
    (just pressing Enter). That is why the question shows [y/N]: the capital
    N means "no" is what you get if you type nothing.
    """
    cleaned = answer.strip().lower()
    if cleaned == "y" or cleaned == "yes":
        return True
    return False


def show_reply_proposal(ticket_id, message):
    """Print the reply the model wants to send, so a human can read it."""
    print()
    print("--- Proposed reply ---")
    print(f"Ticket: {ticket_id}")
    print("Message:")
    print(message)
    print("----------------------")


def ask_human_to_approve_reply(ticket_id, message, input_function):
    """Show the proposed reply, ask the human, and return True if they approved.

    input_function is the function that reads the human's answer. The real
    program passes Python's built-in input(), which waits for someone to type
    at the terminal. Tests pass a small fake function that returns a fixed
    answer, so the test suite never waits for a person.
    """
    show_reply_proposal(ticket_id, message)
    answer = input_function(APPROVAL_QUESTION)
    return is_approved(answer)


def show_escalation_proposal(ticket_id, reason):
    """Print the escalation the model wants to make, so a human can read it."""
    print()
    print("--- Proposed escalation ---")
    print(f"Ticket: {ticket_id}")
    print("Reason:")
    print(reason)
    print("---------------------------")


def ask_human_to_approve_escalation(ticket_id, reason, input_function):
    """Show the proposed escalation, ask the human, and return True if they approved.

    Works exactly like ask_human_to_approve_reply, including the same rule
    for what counts as a yes.
    """
    show_escalation_proposal(ticket_id, reason)
    answer = input_function(ESCALATION_QUESTION)
    return is_approved(answer)
