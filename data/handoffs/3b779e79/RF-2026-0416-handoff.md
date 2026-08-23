# Safeguarding Caseworker Hand-off — RF-2026-0416

> **SAFEGUARDING HAND-OFF (Policy Amendment ACA-2026/2 s.3.9 / s.3.2) — Ordinary casework that a person must do. No automated draft note has been produced. Established case facts are preserved below for caseworker review.**

## 1. Referral Overview

- **Referral ID:** RF-2026-0416
- **Resident Reference:** R-20528
- **Source:** District Office (Weybridge)
- **Received:** 2026-03-17T01:56:00
- **Referrer Urgency:** Standard
- **Requested Action:** Review household composition

> Resident attended counter; reports partner has left the household.

## 2. Reason for Hand-off (Policy Amendment ACA-2026/2)

Under **Section 3.9** of Authority Policy ACA-2026/1 (as amended by ACA-2026/2):
> *Drafting a triage note in respect of a referral concerning a household that includes a person under the age of 18.*

**Determination:** Household includes 1 person(s) under 18: Maria Carver (Son/daughter, b. 2022-04-20, age 3). Safeguarding rule ACA-2026/2 s.3.9 prohibits automated drafting of triage notes; referral must be handed to a caseworker under s.3.2.

An automated assistant is strictly prohibited from drafting a triage note for this case. Pursuant to section 3.2, all work already established is handed directly to the caseworker.

### Minor(s) Recorded in Household:

- **Maria Carver** — Son/daughter (b. 2022-04-20, age 3)

## 3. Preserved Triage Assessment (s.2.3)

- **Category:** Household change
- **Routing:** Assessment team
- **Priority:** Routine (Referring party stated urgency Standard; no circumstance in the referral raises it further.)

## 4. Preserved Resident Context (s.2.2)

```
Resident R-20528 — status Active, benefit HSP-A, district Ash Hill.
Current award: 1,094.80 per month.
Household (2):
  - Tomas Fowler — Applicant — b. 1984-10-01, age 41
  - Maria Carver — Son/daughter — b. 2022-04-20, age 3
Most recent case events (4 of 10):
  - 2025-08-30  Address change recorded — Routine contact, no action required.
  - 2025-07-08  Contact logged — Recalculation applied from start of month.
  - 2025-06-24  Address change recorded — Documents received and filed.
  - 2025-05-26  Evidence received — Left voicemail, no response.
NOTE: retrieved from local_snapshot, not the live Resident History API.
```

## 5. Work Already Completed

1. Read RF-2026-0416 (District Office (Weybridge)) for resident R-20528. (s.3.1) on The overnight queue entry for RF-2026-0416. → {"referral_id": "RF-2026-0416", "resident_ref": "R-20528", "fields_read": ["referral_id", "received_at", "resident_ref", "source", "summary", "requested_action", "urgency"], "requested_action": "Review household composition", "referrer_urgency": "Standard", "redacted_fields": [], "read_at": "2026-08-23T20:57:11"}
2. Retrieve history, household composition and case events for R-20528. (s.3.1) on Resident R-20528 from local_snapshot → {"resident_ref": "R-20528", "available": true, "status": "Active", "benefit_code": "HSP-A", "district": "Ash Hill", "award_monthly": 1094.8, "household_size": 2, "household": [{"name": "Tomas Fowler", "date_of_birth": "1984-10-01", "relationship": "Applicant"}, {"name": "Maria Carver", "date_of_birth": "2022-04-20", "relationship": "Son/daughter"}], "events": [{"date": "2024-12-21", "type": "Review completed", "detail": "Referred to employment support."}, {"date": "2025-01-20", "type": "Interview attended", "detail": "Routine contact, no action required."}, {"date": "2025-01-25", "type": "Interview scheduled", "detail": "Referred to employment support."}, {"date": "2025-02-11", "type": "Note added", "detail": "Referred to employment support."}, {"date": "2025-03-26", "type": "Evidence received", "detail": "Routine contact, no action required."}, {"date": "2025-05-01", "type": "Interview scheduled", "detail": "Correspondence returned undelivered."}, {"date": "2025-05-26", "type": "Evidence received", "detail": "Left voicemail, no response."}, {"date": "2025-06-24", "type": "Address change recorded", "detail": "Documents received and filed."}, {"date": "2025-07-08", "type": "Contact logged", "detail": "Recalculation applied from start of month."}, {"date": "2025-08-30", "type": "Address change recorded", "detail": "Routine contact, no action required."}], "error": "", "source": "local_snapshot", "retrieved_at": "2026-08-23T20:57:11.553853", "latency_ms": null}
3. Categorise as Household change, route to Assessment team, priority Routine. (s.3.1) on The referral text and the resident's record from local_snapshot. → {"category": "Household change", "routing": "Assessment team", "priority": "Routine", "priority_rationale": "Referring party stated urgency Standard; no circumstance in the referral raises it further.", "matched_on": "household", "referrer_urgency": "Standard"}

## 7. Next Step for Caseworker

Caseworker to review referral and resident history, assess safeguarding implications, and draft the triage note exercising human judgment.

---
Handed off by `human:j.alvarez` in run `3b779e79` at 2026-08-23T20:57:11. This is ordinary casework, not a supervisor escalation.
