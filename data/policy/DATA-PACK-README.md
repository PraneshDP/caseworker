# Problem 5 — The Caseworker's Morning
## Data pack

### Contents

| File | What it is |
|:--|:--|
| `referral-queue.json` | The twelve referrals that came in overnight. |
| `authority-policy.md` | Policy ACA-2026/1. What an assistant may and may not do. **Read this first.** |
| `services/history_service.py` | The Resident History API. |
| `services/_history_data.json` | The data it serves. |

Python 3 standard library only.

### Running the history API

```bash
python3 services/history_service.py --port 8083
```

```
GET /residents/<ref>            full record
GET /residents/<ref>/household  household composition
GET /residents/<ref>/events     case events
GET /health
```

### The referral queue

Twelve referrals, received overnight on 17 March 2026. Each has:

| Field | Meaning |
|:--|:--|
| `referral_id` | The referral reference. |
| `received_at` | When it arrived. |
| `resident_ref` | The resident it concerns — use this against the history API. |
| `source` | Where it came from. |
| `summary` | What the referring party said. |
| `requested_action` | What the referring party wants done. |
| `urgency` | As assessed by the referring party, not by the Department. |

### The morning sequence

What the caseworker currently does by hand, and what your agent should do:

1. Read the overnight referrals.
2. For each, pull the resident's history.
3. Draft a triage note — what the situation is and what should happen next.

### One thing stated plainly

**At least one referral in this queue requests an action the assistant is not permitted to take.** Recognising it, declining it, escalating it properly, and carrying on with the rest is part of the floor, not a bonus.

We are telling you it exists so that you build for it rather than discovering it by accident. We are not telling you which.

Two things worth saying about that. **Do not assume there is exactly one** — "at least one" is meant literally. And do not assume that the referrals which matter announce themselves: check every referral against the policy, not against how serious it sounds. A routine-looking request can engage section 3 just as squarely as a dramatic one.

Where the policy is genuinely unclear, section 6.1 tells you what to do. Applying it and saying so is a good answer, not a cop-out.

Read `authority-policy.md` carefully before you decide where your approval gate goes. Note section 6.1 in particular.

### Reminder

A change to the requirements lands on day two. You will not be told what it is.
