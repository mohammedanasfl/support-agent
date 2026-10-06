# Part 7: Tracing

## Purpose

Every time the agent finishes a run, it saves one line in
`traces/runs.jsonl`. That line is one JSON object with:

- the goal
- when the run started and how long it took
- how many tokens it used
- why it stopped (the stop reason)
- the final answer
- every iteration: which tool the model asked for, the arguments, the
  result, and how long the iteration took

To read a run as a tree, I use the trace viewer:

```
python scripts/trace_viewer.py traces/runs.jsonl
```

## Failure investigated

Trace: `ded38774-a0b3-44c3-80ab-59b760e3c218`

Goal:

```
Please handle the refund request for ticket 17 and send a reply to the customer.
```

The run took 5.2 seconds, used 11,583 tokens and had 4 iterations. It
stopped because the ticket was escalated.

## Iteration analysis

**Iteration 1: `get_ticket(ticket_id=17)`**

Correct. The customer got an email saying their payment failed, but their bank
app shows a pending $29 charge. They ask if the payment went through, and they
don't want to be charged twice.

**Iteration 2: `get_refund_policy(plan="pro")`**

Makes sense, because the customer is worried about a refund. The policy says a
duplicate charge for the same period is refunded in full. But nothing in the
ticket shows that a duplicate charge really happened. There is only a failed
payment email and a pending charge.

**Iteration 3: `send_reply(ticket_id=17, ...)`**

This is the **first wrong step**. I approved the reply at the approval prompt
and it was really sent:

```
{"reply_id": 1, "ticket_id": 17, "status": "replied", "sent_at": "2026-10-06T13:37:09"}
```

The reply has two problems:

- It says "I have forwarded your case to our billing team". That was not true
  yet. The escalation only happened in the next iteration.
- It says the billing team "will ensure you are not charged twice". Nobody had
  checked the payment yet, so the agent should not promise this.

The better order was: escalate to the billing team first, and only then tell
the customer what was really done.

**Iteration 4: `escalate(ticket_id=17, ...)`**

The agent escalated the ticket to the billing team. This was the right action,
and the run stopped here with the final answer:

```
Ticket 17 escalated to the human support queue.
```

But it came after the customer had already been told the case was forwarded.

## Why tracing was useful

If I only looked at the final answer, the run looks fine: the ticket was
escalated, which is the right thing for a payment problem.

The trace shows the mistake happened one step earlier:

```
Iteration 1 → get_ticket
Iteration 2 → get_refund_policy
Iteration 3 → send_reply  ← FIRST WRONG STEP
Iteration 4 → escalate (run stops)
```

Because every iteration is saved, I can see the order of the actions and find
the first wrong step, not just the final answer.

## Result

The tracing and the trace viewer show each step the agent took, so I can find
where a run first went wrong.
