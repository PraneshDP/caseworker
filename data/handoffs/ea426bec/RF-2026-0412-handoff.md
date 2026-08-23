# Safeguarding Caseworker Hand-off — RF-2026-0412

> **SAFEGUARDING HAND-OFF (Policy Amendment ACA-2026/2 s.3.9 / s.3.2) — Ordinary casework that a person must do. No automated draft note has been produced. Established case facts are preserved below for caseworker review.**

## 1. Referral Overview

- **Referral ID:** RF-2026-0412
- **Resident Reference:** R-20500
- **Source:** Housing Options
- **Received:** 2026-03-17T04:42:00
- **Referrer Urgency:** Standard
- **Requested Action:** Review award

> Resident reports rent arrears following reduction in hours. Requests review of award.

## 2. Reason for Hand-off (Policy Amendment ACA-2026/2)

Under **Section 3.9** of Authority Policy ACA-2026/1 (as amended by ACA-2026/2):
> *Drafting a triage note in respect of a referral concerning a household that includes a person under the age of 18.*

**Determination:** Household includes 1 person(s) under 18: William Iverson (Son/daughter, b. 2021-02-26, age 5). Safeguarding rule ACA-2026/2 s.3.9 prohibits automated drafting of triage notes; referral must be handed to a caseworker under s.3.2.

An automated assistant is strictly prohibited from drafting a triage note for this case. Pursuant to section 3.2, all work already established is handed directly to the caseworker.

### Minor(s) Recorded in Household:

- **William Iverson** — Son/daughter (b. 2021-02-26, age 5)

## 3. Preserved Triage Assessment (s.2.3)

- **Category:** Income change
- **Routing:** Assessment team
- **Priority:** Same day (The resident has reported financial detriment.)

## 4. Preserved Resident Context (s.2.2)

```
Resident R-20500 — status Active, benefit HSP-A, district Ash Hill.
Current award: 988.04 per month.
Household (2):
  - Elizabeth Whitlock — Applicant — b. 1964-05-25, age 61
  - William Iverson — Son/daughter — b. 2021-02-26, age 5
Most recent case events (4 of 4):
  - 2025-08-21  Note added — Referred to employment support.
  - 2025-06-21  Evidence received — Award unchanged following review.
  - 2025-05-04  Contact logged — Documents received and filed.
  - 2025-03-18  Address change recorded — Referred to employment support.
```

## 5. Work Already Completed

1. Read RF-2026-0412 (Housing Options) for resident R-20500. (s.3.1) on The overnight queue entry for RF-2026-0412. → {"referral_id": "RF-2026-0412", "resident_ref": "R-20500", "fields_read": ["referral_id", "received_at", "resident_ref", "source", "summary", "requested_action", "urgency"], "requested_action": "Review award", "referrer_urgency": "Standard", "redacted_fields": [], "read_at": "2026-08-23T12:17:07"}
2. Retrieve history, household composition and case events for R-20500. (s.3.1) on Resident R-20500 from api → {"resident_ref": "R-20500", "available": true, "status": "Active", "benefit_code": "HSP-A", "district": "Ash Hill", "award_monthly": 988.04, "household_size": 2, "household": [{"name": "Elizabeth Whitlock", "date_of_birth": "1964-05-25", "relationship": "Applicant"}, {"name": "William Iverson", "date_of_birth": "2021-02-26", "relationship": "Son/daughter"}], "events": [{"date": "2025-03-18", "type": "Address change recorded", "detail": "Referred to employment support."}, {"date": "2025-05-04", "type": "Contact logged", "detail": "Documents received and filed."}, {"date": "2025-06-21", "type": "Evidence received", "detail": "Award unchanged following review."}, {"date": "2025-08-21", "type": "Note added", "detail": "Referred to employment support."}], "error": "", "source": "api", "retrieved_at": "2026-08-23T12:14:44.936599", "latency_ms": 217.5}
3. Categorise as Income change, route to Assessment team, priority Same day. (s.3.1) on The referral text and the resident's record from api. → {"category": "Income change", "routing": "Assessment team", "priority": "Same day", "priority_rationale": "The resident has reported financial detriment.", "matched_on": "reduction in hours", "referrer_urgency": "Standard"}

## 7. Next Step for Caseworker

Caseworker to review referral and resident history, assess safeguarding implications, and draft the triage note exercising human judgment.

---
Handed off by `human:j.alvarez` in run `ea426bec` at 2026-08-23T12:17:07. This is ordinary casework, not a supervisor escalation.
