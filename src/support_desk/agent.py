"""The agent loop.

run_agent() works like this:

    messages = [system prompt, user goal]
    repeat, at most max_iterations times:
        if there are too many messages, drop the oldest tool exchanges
        send ALL the messages to the model
        add the model's reply to messages
        if the model did not ask for a tool -> stop: that reply is the final answer
        otherwise run the tool ourselves and add the result to messages
        (send_reply and escalate only run if a human approves them,
         and a successful escalate ends the run)
        (a tool that was already called max_tool_calls times is not run;
         the model gets an error message instead)

The model never runs our code. It can only ASK for a tool by name. Our Python
code decides which function to run, runs it, and decides when to stop.

Every message is a plain Python dictionary with a "role":
    "system"     instructions for the model
    "user"       the goal we give the agent
    "assistant"  a reply from the model (text, tool requests, or both)
    "tool"       the result of one tool call, linked to its request by id
"""

import json
import time
from datetime import datetime, timezone

from support_desk.guardrails import (
    ESCALATION_REJECTION_MESSAGE,
    REJECTION_MESSAGE,
    ask_human_to_approve_escalation,
    ask_human_to_approve_reply,
    make_tool_limit_message,
)
from support_desk.tools import TOOL_DECLARATIONS, TOOL_FUNCTIONS


def run_tool(tool_name, arguments_json):
    """Find the Python function for a tool name, run it, and return its result.

    Always returns a string, so a mistake becomes a message the model can read
    and recover from, instead of crashing the program.
    """
    if tool_name not in TOOL_FUNCTIONS:
        return f"Error: unknown tool '{tool_name}'."

    # The model sends the arguments as JSON text, e.g. '{"ticket_id": 20}'.
    # json.loads turns that text into a Python dictionary: {"ticket_id": 20}.
    try:
        tool_args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return f"Error: the arguments for {tool_name} are not valid JSON: {arguments_json}"

    tool_function = TOOL_FUNCTIONS[tool_name]

    # **tool_args turns {"ticket_id": 20} into get_ticket(ticket_id=20).
    # If the model sent a missing or misspelled argument name, Python raises
    # a TypeError, which we turn into an error message for the model.
    try:
        return tool_function(**tool_args)
    except TypeError as error:
        return f"Error: wrong arguments for {tool_name}: {error}"


def fix_escaped_line_breaks(text):
    """Turn the two characters backslash + n into a real line break.

    The model sends tool arguments as JSON text. In JSON, a line break is
    written as a backslash followed by n. Gemini sometimes adds an extra
    backslash, and then json.loads (correctly) reads it as the two characters
    backslash + n instead of a line break. The customer would see those two
    characters in the reply instead of a new paragraph. A support reply never
    needs the characters backslash + n, so replacing them is safe, and it
    works whatever the model sends.

    Anything that is not text (for example None, when the model left the
    argument out) is returned unchanged, so the normal error handling for a
    missing or wrong argument still applies.
    """
    if not isinstance(text, str):
        return text
    # "\\n" in Python code is the two characters backslash + n;
    # "\n" is one real line break.
    return text.replace("\\n", "\n")


def run_send_reply_with_approval(arguments_json, input_function, actions_done=None):
    """Run send_reply only if a human approves the proposed reply.

    send_reply is the only tool that changes data, so the model's request is
    treated as a PROPOSAL. A person sees the ticket id, the tools that really
    ran so far (actions_done, when given), and the message, and answers yes
    or no:
      - yes: run_tool runs the real send_reply, and its result is returned.
      - no:  send_reply is never called, so nothing is saved, and the model
             gets REJECTION_MESSAGE as the tool result so it can revise.

    The check is here in Python code, so the model cannot skip it, whatever
    the prompt or a ticket says.
    """
    # We need the ticket id and message to show the human, so we read the
    # arguments here. If they cannot be read, there is nothing to show, and
    # send_reply is not run either.
    try:
        tool_args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return f"Error: the arguments for send_reply are not valid JSON: {arguments_json}"
    if not isinstance(tool_args, dict):
        return f"Error: the arguments for send_reply must be a JSON object. Got {arguments_json}"

    # .get() returns None instead of crashing when the model left an argument
    # out. The human then sees "None" and can reject.
    ticket_id = tool_args.get("ticket_id")
    message = tool_args.get("message")

    # Fix the line breaks BEFORE the human sees the message, so the human
    # approves exactly the text that will be saved.
    message = fix_escaped_line_breaks(message)
    if "message" in tool_args:
        tool_args["message"] = message

    approved = ask_human_to_approve_reply(ticket_id, message, input_function, actions_done)

    if not approved:
        print("[approval] rejected: send_reply was NOT run")
        return REJECTION_MESSAGE

    print("[approval] approved: running send_reply")
    # json.dumps turns the arguments, with the fixed message, back into JSON
    # text, so run_tool sends the approved message, not the model's original.
    return run_tool("send_reply", json.dumps(tool_args))


def run_escalate_with_approval(arguments_json, input_function, actions_done=None):
    """Run escalate only if a human approves it.

    Works like run_send_reply_with_approval, but returns TWO values:
        result     the text that goes back to the model as the tool result
        escalated  True only if the ticket really was escalated

    The loop needs the second value because a successful escalation ends the
    run, while a rejection or an error does not.
    """
    # Read the arguments so we can show them to the human.
    try:
        tool_args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return f"Error: the arguments for escalate are not valid JSON: {arguments_json}", False
    if not isinstance(tool_args, dict):
        return f"Error: the arguments for escalate must be a JSON object. Got {arguments_json}", False

    ticket_id = tool_args.get("ticket_id")
    reason = tool_args.get("reason")

    # The same line-break fix as for a reply: the reason is read by people too.
    reason = fix_escaped_line_breaks(reason)
    if "reason" in tool_args:
        tool_args["reason"] = reason

    approved = ask_human_to_approve_escalation(ticket_id, reason, input_function, actions_done)

    if not approved:
        print("[approval] rejected: escalate was NOT run")
        return ESCALATION_REJECTION_MESSAGE, False

    print("[approval] approved: running escalate")
    result = run_tool("escalate", json.dumps(tool_args))

    # Every tool and run_tool start their error messages with "Error:". So if
    # the result does not, the escalation succeeded.
    if result.startswith("Error:"):
        return result, False
    return result, True


def get_call_tokens(response):
    """Return how many tokens one model call used.

    total_tokens = prompt_tokens (the whole history we sent)
                 + completion_tokens (the model's reply).
    """
    if response.usage is None:
        return 0
    return response.usage.total_tokens


def format_tool_call(tool_call):
    """Turn a tool call into readable text for the log, e.g. get_ticket({"ticket_id":20})."""
    return f"{tool_call.function.name}({tool_call.function.arguments})"


def make_assistant_message(reply_text, tool_calls):
    """Turn the model's reply into a plain dictionary for the message history.

    We copy the fields we need instead of storing the SDK's object, so every
    message in the history is a simple dictionary we can print or save.
    """
    assistant_message = {"role": "assistant", "content": reply_text}

    if len(tool_calls) > 0:
        assistant_message["tool_calls"] = []
        for tool_call in tool_calls:
            tool_call_message = {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }

            # Gemini 3 attaches a "thought signature" to every tool call, in an
            # extra field called extra_content. Gemini rejects the next call
            # (error 400) unless the signature comes back unchanged in the
            # history, so we copy it when it is there. getattr(..., None) gives
            # None for replies without the field, such as the test fakes.
            extra_content = getattr(tool_call, "extra_content", None)
            if extra_content is not None:
                tool_call_message["extra_content"] = extra_content

            assistant_message["tool_calls"].append(tool_call_message)

    return assistant_message


def compact_messages(messages, keep_exchanges):
    """Drop the oldest tool exchanges from messages, changing the list in place.

    messages[0] is the system prompt and messages[1] is the goal. Both are
    always kept. Everything after them is a series of tool exchanges. One
    exchange is an assistant message that asked for tools, followed by one
    "tool" message for each request:

        [system] [goal] [assistant, tool] [assistant, tool, tool] [assistant, tool]
                        |-- exchange 1 -| |------ exchange 2 ---| |-- exchange 3 -|

    Only the newest keep_exchanges exchanges are kept (keep_exchanges must be
    at least 1). An exchange is kept or
    dropped as a whole: the API rejects a tool message whose request is
    missing, and a request without its result would confuse the model.

    What is dropped can be fetched again: the tools only read data, and the
    goal (which names the ticket) is never dropped.

    Returns how many messages were removed.
    """
    # Find where each exchange starts: every assistant message after the goal.
    exchange_starts = []
    for index in range(2, len(messages)):
        if messages[index]["role"] == "assistant":
            exchange_starts.append(index)

    # Not more exchanges than we want to keep: nothing to drop.
    if len(exchange_starts) <= keep_exchanges:
        return 0

    # The first message to keep is the start of the oldest exchange we keep.
    # Everything between the goal and that message is removed. "del" with a
    # slice removes those positions from the list itself, so this is still the
    # same list object that the rest of the loop is using.
    first_kept = exchange_starts[len(exchange_starts) - keep_exchanges]
    removed = first_kept - 2
    del messages[2:first_kept]
    return removed


def elapsed_ms(start_time):
    """Return the milliseconds since start_time, a value from time.perf_counter().

    perf_counter() is a stopwatch: it is only good for measuring how long
    something took, and it never jumps if the computer's clock is changed.
    """
    return round((time.perf_counter() - start_time) * 1000)


def make_result(stop_reason, final_text, iterations, total_tokens, messages,
                iteration_records, started_at, run_start_time):
    """Collect everything about how a run ended into one dictionary."""
    return {
        "stop_reason": stop_reason,  # why the loop stopped
        "final_text": final_text,  # the model's answer, the escalation result,
                                   # why a limit stopped the run, or None
        "iterations": iterations,  # how many times we called the model
        "total_tokens": total_tokens,  # tokens used, added up over all calls
        "messages": messages,  # the complete history of the run
        # For the trace (see tracing.py):
        "started_at": started_at,  # when the run started (UTC date and time)
        "total_duration_ms": elapsed_ms(run_start_time),  # how long the whole run took
        "iteration_records": iteration_records,  # what happened in each iteration
    }


def run_agent(
    client,
    goal,
    model,
    system_prompt,
    max_iterations,
    max_total_tokens,
    max_tool_calls,
    context_message_threshold,
    context_keep_exchanges,
    input_function=input,
):
    """Run the agent loop on one goal and return a result dictionary.

    The three hard limits are checked here in Python, so the model cannot
    talk its way past them:
        max_iterations    the most model calls in one run
        max_total_tokens  the most tokens in one run, added up over all calls
        max_tool_calls    the most times EACH tool may be called in one run

    input_function reads the human's answer when send_reply or escalate needs
    approval. It is Python's built-in input() unless a test passes a fake one.
    """

    # The ONE message list for the whole run. It starts with the system
    # prompt and the user's goal. Every model reply and every tool result is
    # appended to this same list, and the whole list is sent on every call.
    # When it grows too long, compaction (Step 0) removes old entries from
    # this same list; it never makes a new one.
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]

    total_tokens = 0

    # How many times each tool has been called in this run, by tool name, for
    # example {"get_ticket": 2, "search_tickets": 1}. A tool that has not been
    # called yet is simply not in the dictionary. Each tool has its own count,
    # so reaching the limit for one tool does not block the others.
    tool_call_counts = {}

    # The tool calls that really ran in this run, in order, as readable text
    # like 'get_ticket({"ticket_id":17})'. The human approving a reply or an
    # escalation sees this list, so they can spot a reply that claims an
    # action no tool did, or a reply already sent before an escalation.
    actions_done = []

    # For the trace: when the run started, a stopwatch for how long it takes,
    # and one record per iteration (see iteration_record below). Recording
    # only stores values; it never changes what the loop does.
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_start_time = time.perf_counter()
    iteration_records = []

    for iteration in range(1, max_iterations + 1):
        iteration_start_time = time.perf_counter()

        # Step 0: if the history has grown too long, drop the oldest tool
        # exchanges before sending it. This happens before the call, so the
        # call never pays for the dropped messages.
        # If the model asks for many tools at once, the newest exchanges alone
        # can be longer than the threshold. Then nothing is removed, and we
        # print nothing, because nothing was compacted.
        if len(messages) > context_message_threshold:
            messages_before = len(messages)
            removed = compact_messages(messages, context_keep_exchanges)
            if removed > 0:
                print(f"[context] compacted {messages_before} messages -> {len(messages)} messages")

        # Step 1: send the whole message history to the model.
        # tools=TOOL_DECLARATIONS shows the model which tools it may ASK for.
        # The model API never runs a tool itself; it only returns the request to us.
        messages_sent = len(messages)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_DECLARATIONS,
        )

        # Step 2: add the tokens this call used to the running total.
        call_tokens = get_call_tokens(response)
        total_tokens = total_tokens + call_tokens
        print(
            f"[iter {iteration}] messages={messages_sent} "
            f"call_tokens={call_tokens} total_tokens={total_tokens}"
        )

        # The trace record for this iteration. It is added to the list now and
        # filled in as the iteration goes on:
        #   tool_calls  stays None if the model asked for no tool (a final
        #               answer or an empty reply); otherwise a list, because
        #               one reply can ask for several tools
        #   elapsed_ms  set when the iteration ends, just before a return or
        #               at the bottom of the loop
        iteration_record = {
            "iteration": iteration,
            "call_tokens": call_tokens,
            "tool_calls": None,
            "elapsed_ms": None,
        }
        iteration_records.append(iteration_record)

        # Step 3: read the reply, and stop if it contains nothing usable.
        if not response.choices:
            print(f"[iter {iteration}] empty response")
            iteration_record["elapsed_ms"] = elapsed_ms(iteration_start_time)
            return make_result("empty_response", None, iteration, total_tokens, messages,
                               iteration_records, started_at, run_start_time)

        model_reply = response.choices[0].message
        reply_text = model_reply.content  # None when the model only asks for tools
        tool_calls = model_reply.tool_calls  # None when the model asks for no tools
        if tool_calls is None:
            tool_calls = []

        if len(tool_calls) == 0 and not reply_text:
            print(f"[iter {iteration}] empty response")
            iteration_record["elapsed_ms"] = elapsed_ms(iteration_start_time)
            return make_result("empty_response", None, iteration, total_tokens, messages,
                               iteration_records, started_at, run_start_time)

        # Step 4: add the model's reply to the history.
        messages.append(make_assistant_message(reply_text, tool_calls))

        # Step 5: no tool requested means the model has given its final answer.
        if len(tool_calls) == 0:
            print(f"[iter {iteration}] final answer")
            iteration_record["elapsed_ms"] = elapsed_ms(iteration_start_time)
            return make_result("final_answer", reply_text, iteration, total_tokens, messages,
                               iteration_records, started_at, run_start_time)

        # Record every requested tool in the trace. "result" starts as None and
        # is filled in by Step 7; it stays None for a tool that is never run
        # (the token budget stopped the run, or an escalation ended it first).
        # The arguments are kept exactly as the model sent them, as JSON text,
        # so the trace also shows arguments that were not valid JSON.
        iteration_record["tool_calls"] = []
        for tool_call in tool_calls:
            print(f"[iter {iteration}] tool requested: {format_tool_call(tool_call)}")
            iteration_record["tool_calls"].append({
                "tool": tool_call.function.name,
                "arguments": tool_call.function.arguments,
                "result": None,
            })

        # Step 6: stop if the token budget is used up.
        # This check comes after the final-answer check, so a finished answer is
        # kept even if it went over budget, and before running the tools,
        # because no further model call would read their results.
        # The API only reports tokens AFTER a call, so the total can go a bit
        # over the limit on the last call. We keep the real total; we never
        # cut it down to the limit.
        if total_tokens >= max_total_tokens:
            explanation = (
                f"Stopped: the token limit of {max_total_tokens} was reached "
                f"({total_tokens} tokens used) before the model gave a final answer."
            )
            print(f"[iter {iteration}] {explanation}")
            iteration_record["elapsed_ms"] = elapsed_ms(iteration_start_time)
            return make_result("token_budget", explanation, iteration, total_tokens, messages,
                               iteration_records, started_at, run_start_time)

        # Step 7: run each requested tool ourselves, and add each result to the
        # history as its own "tool" message. tool_call_id tells the model which
        # of its requests this result answers.
        # First the per-tool limit is checked: a tool that already reached it
        # is not run at all, so not even a human is asked. Otherwise,
        # send_reply and escalate change data, so they go through a human
        # approval gate instead of being run directly. The other tools only
        # read data.
        # enumerate gives each request's position too, so we can find its
        # entry in iteration_record["tool_calls"] (same order as tool_calls).
        for position, tool_call in enumerate(tool_calls):
            tool_name = tool_call.function.name
            escalated = False

            # .get(tool_name, 0) gives 0 for a tool that was not called yet.
            calls_so_far = tool_call_counts.get(tool_name, 0)

            if calls_so_far >= max_tool_calls:
                # The model still gets a tool message for this request (the API
                # needs a result for every request), but the tool is not run.
                print(f"[iter {iteration}] {tool_name} limit of {max_tool_calls} calls reached, NOT run")
                result = make_tool_limit_message(tool_name, max_tool_calls)
            else:
                # Count the call before running it. Every request that gets
                # past the limit counts, also one the human rejects, so the
                # human is asked at most max_tool_calls times per tool.
                tool_call_counts[tool_name] = calls_so_far + 1

                if tool_name == "send_reply":
                    result = run_send_reply_with_approval(
                        tool_call.function.arguments, input_function, actions_done
                    )
                elif tool_name == "escalate":
                    result, escalated = run_escalate_with_approval(
                        tool_call.function.arguments, input_function, actions_done
                    )
                else:
                    result = run_tool(tool_name, tool_call.function.arguments)

                # Add the call to actions_done only if it really did something.
                # Every error starts with "Error:", and a rejected send_reply
                # or escalate returns its rejection message: in both cases
                # nothing happened, so the call is not listed.
                if (
                    not result.startswith("Error:")
                    and result != REJECTION_MESSAGE
                    and result != ESCALATION_REJECTION_MESSAGE
                ):
                    actions_done.append(format_tool_call(tool_call))

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
            messages.append(tool_message)

            # The trace gets the same text the model got: the tool's output, or
            # the error / rejection / limit message that was sent instead.
            iteration_record["tool_calls"][position]["result"] = result

            # Step 8: a successful escalation hands the ticket to a person, so
            # the run ends here, straight after recording the result. The
            # model is not called again, and any later tool requests in this
            # same reply are not run.
            if escalated:
                print(f"[iter {iteration}] ticket escalated, stopping the run")
                iteration_record["elapsed_ms"] = elapsed_ms(iteration_start_time)
                return make_result("escalated", result, iteration, total_tokens, messages,
                                   iteration_records, started_at, run_start_time)

        # This iteration is over. Its time includes the model call, running the
        # tools, and any time a human took to answer an approval question.
        iteration_record["elapsed_ms"] = elapsed_ms(iteration_start_time)

        # The loop now goes round, and the model is called again with everything.

    # The for loop finished without a final answer: the iteration cap was
    # reached. range(1, max_iterations + 1) ran exactly max_iterations times,
    # so exactly max_iterations model calls were made, never one more.
    explanation = (
        f"Stopped: the iteration limit of {max_iterations} model calls was "
        f"reached before the model gave a final answer."
    )
    print(explanation)
    return make_result("max_iterations", explanation, max_iterations, total_tokens, messages,
                       iteration_records, started_at, run_start_time)
