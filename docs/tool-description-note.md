# Tool Description Note

The tool description I rewrote is `search_tickets` (Part 3).

## The original description

```
description: "Search tickets by text and category."
query:       "The search text."
category:    "The ticket category."
```

This says *what* the tool does but not *how it matches*. `search_tickets` uses
SQL `LIKE '%query%'`, so the whole query must appear in the ticket **as one
exact piece of text**. It is not a search engine that matches separate words.

## The revised description

```
description: "Search all tickets, open and closed, for a keyword in the subject
  or body. The query is matched as ONE exact piece of text (ignoring upper/lower
  case), not as separate words: 'Safari' finds every ticket that mentions
  Safari, but 'Safari dashboard blank' only finds tickets containing those
  three words in that exact order. So pass one or two keywords, not a sentence,
  and if a search finds nothing, try a single different word. Optionally pass a
  category to keep only that kind of ticket. Returns at most 5 tickets, newest
  first, each with a short snippet of its body, plus total_matches and shown.
  Call get_ticket to read a whole ticket."
query:    "One keyword or a short phrase, e.g. 'Safari' or 'refund'. Not a full
  sentence."
category: "Optional. Only return tickets of this kind. Leave it out to search
  every ticket."
```

## Why it is clearer

- **It explains how matching works, with an example of a search that fails.**
  The model now knows that `'Safari dashboard blank'` does not find a ticket
  titled "Dashboard charts are blank in Safari".
- **It says what to do when a search finds nothing:** try one different word.
- **It says what comes back:** at most 5 tickets, snippets only,
  `total_matches` and `shown`, and that `get_ticket` gives the full text.
- **It says `category` is optional.**

## What changed in the agent's behaviour

**Test goal:** "Look up ticket 19. Then use search_tickets to find earlier
tickets about the same problem, and say how that problem was resolved before."

Ticket 19 is "Safari dashboard blank again". The earlier ticket about the same
problem is ticket 8, "Dashboard charts are blank in Safari". A search for
`Safari` finds both tickets. A search for `Safari dashboard blank` finds only
ticket 19.

**Setup:** model `qwen/qwen3.8-27b` on Groq, the same goal and system prompt,
and 6 runs with each description. Only the description changed.

| | Original | Revised |
|---|---|---|
| Runs where a search returned ticket 8 | **2 / 6** | **5 / 6** |
| Total `search_tickets` calls (6 runs) | 16 | 11 |
| Runs where every search used 2+ words | 4 / 6 | 1 / 6 |

**Original description.** The first query was always a phrase copied from the
ticket subject: `"Safari dashboard blank"` or a re-ordering of it. When that
found only ticket 19, the model tried more phrases with different word orders
(`"dashboard charts blank Safari"`, `"Safari blank charts"`).
- In 4 runs it never got to a single word. In the 3 of those where I logged
  every tool call, it found ticket 8 through `get_customer_history` instead.
  That only worked because the same customer happened to file both tickets. A
  different customer's earlier ticket would have been missed.
- In 2 runs it reached `"Safari"` or `"dashboard"` after 3–4 searches.

**Revised description.** The model still often started with a phrase. But
after an empty or one-result search, it changed to a single keyword (`"Safari"`)
on its next search, as the description says to.
- In 5 of 6 runs a search returned ticket 8.
- It needed fewer searches: 11 in total, against 16.

**What it did not fix:** the first query was still a multi-word phrase in 5 of
6 runs. In 1 run the model searched only once with the full phrase and did not
retry. A description guides the model; it does not force it. A stricter fix
would be in code, such as matching each word of the query separately.
