# Safeguarding Caseworker Hand-off — RF-2026-0418

> **SAFEGUARDING HAND-OFF (Policy Amendment ACA-2026/2 s.3.9 / s.3.2) — Ordinary casework that a person must do. No automated draft note has been produced. Established case facts are preserved below for caseworker review.**

## 1. Referral Overview

- **Referral ID:** RF-2026-0418
- **Resident Reference:** R-20542
- **Source:** Health Visitor
- **Received:** 2026-03-17T03:39:00
- **Referrer Urgency:** Standard
- **Requested Action:** Review award

> New baby in household. Resident asks whether award changes.

## 2. Reason for Hand-off (Policy Amendment ACA-2026/2)

Under **Section 3.9** of Authority Policy ACA-2026/1 (as amended by ACA-2026/2):
> *Drafting a triage note in respect of a referral concerning a household that includes a person under the age of 18.*

**Determination:** Household includes 2 person(s) under 18: Michael Crowley (Son/daughter, b. 2013-08-13, age 12); Rosa Vance (Son/daughter, b. 2025-11-19, age 0). Safeguarding rule ACA-2026/2 s.3.9 prohibits automated drafting of triage notes; referral must be handed to a caseworker under s.3.2.

An automated assistant is strictly prohibited from drafting a triage note for this case. Pursuant to section 3.2, all work already established is handed directly to the caseworker.

### Minor(s) Recorded in Household:

- **Michael Crowley** — Son/daughter (b. 2013-08-13, age 12)
- **Rosa Vance** — Son/daughter (b. 2025-11-19, age 0)

## 3. Preserved Triage Assessment (s.2.3)

- **Category:** Household change
- **Routing:** Assessment team
- **Priority:** Same day (A change in care or household circumstances is recorded; assistance may be understated while it waits.)

## 4. Preserved Resident Context (s.2.2)

```
Resident R-20542 — status Active, benefit HSP-C, district Ash Hill.
Current award: 1,449.67 per month.
Household (4):
  - Sarah Delgado — Applicant — b. 1993-09-28, age 32
  - Michael Crowley — Son/daughter — b. 2013-08-13, age 12
  - Patricia Kessler — Spouse/partner — b. 2002-09-29, age 23
  - Rosa Vance — Son/daughter — b. 2025-11-19, age 0
Most recent case events (4 of 8):
  - 2025-07-02  Evidence requested — Attended district office in person.
  - 2025-06-04  Interview scheduled — Left voicemail, no response.
  - 2025-05-14  Payment issued — Correspondence returned undelivered.
  - 2025-04-30  Evidence received — Correspondence returned undelivered.
NOTE: retrieved from local_snapshot, not the live Resident History API.
```

## 5. Work Already Completed

1. Read RF-2026-0418 (Health Visitor) for resident R-20542. (s.3.1) on The overnight queue entry for RF-2026-0418. → {"referral_id": "RF-2026-0418", "resident_ref": "R-20542", "fields_read": ["referral_id", "received_at", "resident_ref", "source", "summary", "requested_action", "urgency"], "requested_action": "Review award", "referrer_urgency": "Standard", "redacted_fields": [], "read_at": "2026-08-23T20:39:04"}
2. Retrieve history, household composition and case events for R-20542. (s.3.1) on Resident R-20542 from local_snapshot → {"resident_ref": "R-20542", "available": true, "status": "Active", "benefit_code": "HSP-C", "district": "Ash Hill", "award_monthly": 1449.67, "household_size": 4, "household": [{"name": "Sarah Delgado", "date_of_birth": "1993-09-28", "relationship": "Applicant"}, {"name": "Michael Crowley", "date_of_birth": "2013-08-13", "relationship": "Son/daughter"}, {"name": "Patricia Kessler", "date_of_birth": "2002-09-29", "relationship": "Spouse/partner"}, {"name": "Rosa Vance", "date_of_birth": "2025-11-19", "relationship": "Son/daughter"}], "events": [{"date": "2025-01-10", "type": "Note added", "detail": "Documents received and filed."}, {"date": "2025-01-24", "type": "Evidence received", "detail": "Referred to employment support."}, {"date": "2025-02-26", "type": "Review completed", "detail": "Correspondence returned undelivered."}, {"date": "2025-04-11", "type": "Interview scheduled", "detail": "Referred to employment support."}, {"date": "2025-04-30", "type": "Evidence received", "detail": "Correspondence returned undelivered."}, {"date": "2025-05-14", "type": "Payment issued", "detail": "Correspondence returned undelivered."}, {"date": "2025-06-04", "type": "Interview scheduled", "detail": "Left voicemail, no response."}, {"date": "2025-07-02", "type": "Evidence requested", "detail": "Attended district office in person."}], "error": "", "source": "local_snapshot", "retrieved_at": "2026-08-23T20:39:04.894882", "latency_ms": null}
3. Categorise as Household change, route to Assessment team, priority Same day. (s.3.1) on The referral text and the resident's record from local_snapshot. → {"category": "Household change", "routing": "Assessment team", "priority": "Same day", "priority_rationale": "A change in care or household circumstances is recorded; assistance may be understated while it waits.", "matched_on": "household", "referrer_urgency": "Standard"}

## 7. Next Step for Caseworker

Caseworker to review referral and resident history, assess safeguarding implications, and draft the triage note exercising human judgment.

---
Handed off by `human:j.alvarez` in run `e3b0d0c1` at 2026-08-23T20:39:04. This is ordinary casework, not a supervisor escalation.
