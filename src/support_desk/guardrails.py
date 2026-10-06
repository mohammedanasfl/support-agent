"""Guardrails enforced in code, not by asking the model nicely in the prompt.

Part 5: the human approval gates for the two tools that change data,
send_reply and escalate. The model can only ASK for them. Before our code
runs one, a person at the terminal sees what the model proposes and must type
"y" or "yes". Anything else is a rejection, and nothing is changed.

The functions here only deal with the human side: showing the proposal and
reading the answer. agent.py decides when to call them.

Both proposals also show the tools that really ran in this run, so the
human can check a reply against what was actually done, and see a reply
that was already sent before deciding on an escalation (added after the
Part 7 trace investigation, see show_actions_done).

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


def show_actions_done(actions_done):
    """Print the tools that really ran in this run, for the human to check a reply against.

    actions_done is a list like ['get_ticket({"ticket_id":17})', ...], built
    by agent.py. It only holds calls that really ran: a call that was blocked
    by a limit, rejected by the human, or returned an error did nothing, so
    it is not in the list.

    Why: in the Part 7 trace ded38774, a reply said "I have forwarded your
    case to our billing team" before anything had been escalated, and it was
    approved. With this list in front of them, the human can see that no
    escalate ran, and reject the reply.
    """
    print("Done so far in this run:")
    if len(actions_done) == 0:
        print("  (nothing yet)")
    for action in actions_done:
        print(f"  - {action}")


def tool_was_run(actions_done, tool_name):
    """Return True if actions_done contains a call of tool_name.

    Every entry starts with the tool name and "(", e.g. 'escalate({...})',
    so checking the start is enough. The "(" makes sure that, for example,
    a tool called "escalate_later" would not count as "escalate".
    """
    for action in actions_done:
        if action.startswith(tool_name + "("):
            return True
    return False


def show_reply_proposal(ticket_id, message, actions_done=None):
    """Print the reply the model wants to send, so a human can read it.

    actions_done is the list of tools that really ran (see show_actions_done).
    None means the caller has no such list, for example a test that calls the
    gate directly; then that part is simply left out.
    """
    print()
    print("--- Proposed reply ---")
    print(f"Ticket: {ticket_id}")
    if actions_done is not None:
        show_actions_done(actions_done)
        if not tool_was_run(actions_done, "escalate"):
            print("  Not done: the ticket has NOT been escalated or forwarded to anyone.")
        print("  (Reject the reply if it claims anything that is not listed here.)")
    print("Message:")
    print(message)
    print("----------------------")


def ask_human_to_approve_reply(ticket_id, message, input_function, actions_done=None):
    """Show the proposed reply, ask the human, and return True if they approved.

    input_function is the function that reads the human's answer. The real
    program passes Python's built-in input(), which waits for someone to type
    at the terminal. Tests pass a small fake function that returns a fixed
    answer, so the test suite never waits for a person.

    actions_done is passed on to show_reply_proposal.
    """
    show_reply_proposal(ticket_id, message, actions_done)
    answer = input_function(APPROVAL_QUESTION)
    return is_approved(answer)


def show_escalation_proposal(ticket_id, reason, actions_done=None):
    """Print the escalation the model wants to make, so a human can read it.

    actions_done works as in show_reply_proposal. It matters here because a
    reply is always sent before an escalation: that reply may already have
    told the customer that a team will look at their case. If the human
    rejects the escalation, that promise is left to a person, so the human
    should see the reply before deciding.
    """
    print()
    print("--- Proposed escalation ---")
    print(f"Ticket: {ticket_id}")
    if actions_done is not None:
        show_actions_done(actions_done)
        if tool_was_run(actions_done, "send_reply"):
            print("  Note: a reply was already sent to the customer (see above).")
            print("  If you reject, check whether it promised that a team will follow up.")
    print("Reason:")
    print(reason)
    print("---------------------------")


def ask_human_to_approve_escalation(ticket_id, reason, input_function, actions_done=None):
    """Show the proposed escalation, ask the human, and return True if they approved.

    Works exactly like ask_human_to_approve_reply, including the same rule
    for what counts as a yes.
    """
    show_escalation_proposal(ticket_id, reason, actions_done)
    answer = input_function(ESCALATION_QUESTION)
    return is_approved(answer)
