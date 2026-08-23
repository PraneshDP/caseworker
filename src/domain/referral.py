from __future__ import annotations
"""Referral and resident-history types — the unit of work for a morning run.

A referral is external input. Every free-text field on it was written by someone
outside the Department: a district office, a health visitor, the Counter-Fraud
Unit, or -- for the self-referrals -- the resident, through a web form. Three of
the twelve referrals in the queue are self-referrals.

That has two consequences the types below are built around:

  1. VALIDATION IS PER-RECORD. One malformed referral must not take out the
     morning. `from_dict` raises, the loader catches, and the run continues with
     the record recorded as skipped. Policy 4.3 says escalation of one referral
     must not stop the others; the same logic applies to a bad record.

  2. `requested_action` IS AN ATTACK SURFACE. It is the string the authority
     engine matches against. It is also resident-writable. The engine is
     deterministic code over a data file, so there is no prompt to talk out of a
     decision -- and because the boundary is default-deny, text that matches
     nothing lands on section 6.1 and escalates. Injection can make the
     assistant do less, never more.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


class ReferralValidationError(ValueError):
    """A referral record cannot be safely loaded.

    Caught per-record by the loader so one bad row does not abort the run.
    """


_REQUIRED_FIELDS = ("referral_id", "resident_ref", "requested_action")

#: Urgency as stated by the referring party. The data pack is explicit that this
#: is "as assessed by the referring party, not by the Department", so it orders
#: the queue and nothing else. Policy 6.2 forbids it influencing authority.
URGENCY_ORDER = {"high": 0, "standard": 1, "low": 2}
DEFAULT_URGENCY_RANK = 1


@dataclass
class Referral:
    """One referral from the overnight queue."""

    referral_id: str
    resident_ref: str
    requested_action: str
    received_at: str = ""
    source: str = ""
    summary: str = ""
    urgency: str = "Standard"

    #: Sanitised copy of `requested_action`, shown to the model instead of the
    #: original. The authority engine deliberately reads the original -- see the
    #: docstring of `src/security/screen.py` for why that is the fail-closed
    #: direction. Empty means nothing was redacted.
    requested_action_for_prompt: str = ""

    #: Set by the injection screen when a field was redacted, so the triage note
    #: and the ledger can both say so.
    redacted_fields: list[str] = field(default_factory=list)

    @property
    def prompt_action(self) -> str:
        """The action text safe to put in a prompt."""
        return self.requested_action_for_prompt or self.requested_action

    @property
    def id(self) -> str:
        """Alias used wherever a generic record id is wanted (audit, logging)."""
        return self.referral_id

    @property
    def urgency_rank(self) -> int:
        return URGENCY_ORDER.get(self.urgency.strip().lower(), DEFAULT_URGENCY_RANK)

    # -- validation ---------------------------------------------------------

    @staticmethod
    def _as_str(value: Any, name: str, *, required: bool = False) -> str:
        if value is None:
            if required:
                raise ReferralValidationError(f"field '{name}' is required")
            return ""
        if not isinstance(value, str):
            raise ReferralValidationError(
                f"field '{name}' must be a string, got {type(value).__name__}"
            )
        text = value.strip()
        if required and not text:
            raise ReferralValidationError(f"field '{name}' must not be empty")
        return text

    @classmethod
    def from_dict(cls, raw: Any) -> "Referral":
        if not isinstance(raw, dict):
            raise ReferralValidationError(
                f"referral record must be an object, got {type(raw).__name__}"
            )
        for name in _REQUIRED_FIELDS:
            if name not in raw:
                raise ReferralValidationError(f"missing required field '{name}'")

        received = cls._as_str(raw.get("received_at", ""), "received_at")
        if received:
            try:
                datetime.fromisoformat(received)
            except ValueError:
                raise ReferralValidationError(
                    f"field 'received_at' must be an ISO-8601 timestamp, got {received!r}"
                )

        return cls(
            referral_id=cls._as_str(raw.get("referral_id"), "referral_id", required=True),
            resident_ref=cls._as_str(raw.get("resident_ref"), "resident_ref", required=True),
            requested_action=cls._as_str(
                raw.get("requested_action"), "requested_action", required=True
            ),
            received_at=received,
            source=cls._as_str(raw.get("source", ""), "source"),
            summary=cls._as_str(raw.get("summary", ""), "summary"),
            urgency=cls._as_str(raw.get("urgency", "Standard"), "urgency") or "Standard",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "referral_id": self.referral_id,
            "resident_ref": self.resident_ref,
            "requested_action": self.requested_action,
            "received_at": self.received_at,
            "source": self.source,
            "summary": self.summary,
            "urgency": self.urgency,
            "redacted_fields": list(self.redacted_fields),
        }

    def one_line(self) -> str:
        return (
            f"{self.referral_id} | {self.resident_ref} | {self.source or 'unknown source'} "
            f"| urgency {self.urgency} | asks: {self.requested_action}"
        )


# ---------------------------------------------------------------------------
# Resident history (from the Resident History API)
# ---------------------------------------------------------------------------

@dataclass
class HouseholdMember:
    name: str = ""
    date_of_birth: str = ""
    relationship: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "HouseholdMember":
        if not isinstance(raw, dict):
            return cls(name=str(raw))
        return cls(
            name=str(raw.get("name") or ""),
            date_of_birth=str(raw.get("date_of_birth") or ""),
            relationship=str(raw.get("relationship") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "date_of_birth": self.date_of_birth,
            "relationship": self.relationship,
        }

    def age_as_of(self, reference_date: Optional[Any] = None) -> Optional[int]:
        """Compute member's age as of reference_date (defaults to referral date 2026-03-17)."""
        if not self.date_of_birth or not str(self.date_of_birth).strip():
            return None
        try:
            dob = datetime.strptime(str(self.date_of_birth).strip()[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
        if reference_date is None:
            ref = datetime(2026, 3, 17).date()
        elif isinstance(reference_date, datetime):
            ref = reference_date.date()
        elif hasattr(reference_date, "year") and hasattr(reference_date, "month") and hasattr(reference_date, "day"):
            ref = reference_date
        else:
            try:
                ref = datetime.fromisoformat(str(reference_date)[:10]).date()
            except Exception:
                ref = datetime(2026, 3, 17).date()

        years = ref.year - dob.year
        if (ref.month, ref.day) < (dob.month, dob.day):
            years -= 1
        return max(0, years)

    def is_under_18(self, reference_date: Optional[Any] = None) -> Optional[bool]:
        """True if age is known and < 18; False if age is known and >= 18; None if DOB missing/invalid."""
        age = self.age_as_of(reference_date)
        if age is None:
            return None
        return age < 18

    def one_line(self) -> str:
        parts = [self.name or "(unnamed)"]
        if self.relationship:
            parts.append(self.relationship)
        if self.date_of_birth:
            age = self.age_as_of()
            age_info = f", age {age}" if age is not None else ""
            parts.append(f"b. {self.date_of_birth}{age_info}")
        return " — ".join(parts)


@dataclass
class CaseEvent:
    date: str = ""
    type: str = ""
    detail: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "CaseEvent":
        if not isinstance(raw, dict):
            return cls(detail=str(raw))
        return cls(
            date=str(raw.get("date") or ""),
            type=str(raw.get("type") or ""),
            detail=str(raw.get("detail") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "type": self.type, "detail": self.detail}

    def one_line(self) -> str:
        return f"{self.date or '(undated)'}  {self.type or 'event'} — {self.detail}".strip()


@dataclass
class ResidentHistory:
    """What the Resident History API returned for one resident.

    `available` is False when the lookup failed. It is a first-class field rather
    than an exception because a failed lookup does not stop the referral being
    triaged -- it makes the triage note say what the caseworker is missing, and
    raises the data_incomplete risk signal.
    """

    resident_ref: str
    available: bool = False
    status: str = ""
    benefit_code: str = ""
    district: str = ""
    award_monthly: Optional[float] = None
    household: list[HouseholdMember] = field(default_factory=list)
    events: list[CaseEvent] = field(default_factory=list)
    error: str = ""
    source: str = ""            # "api" | "local_snapshot" | "unavailable"
    retrieved_at: str = field(default_factory=lambda: datetime.now().isoformat())
    latency_ms: Optional[float] = None

    @classmethod
    def unavailable(cls, resident_ref: str, error: str) -> "ResidentHistory":
        return cls(resident_ref=resident_ref, available=False, error=error,
                   source="unavailable")

    @classmethod
    def from_api(
        cls,
        resident_ref: str,
        raw: dict,
        *,
        source: str = "api",
        latency_ms: Optional[float] = None,
    ) -> "ResidentHistory":
        award = raw.get("award_monthly")
        try:
            award_value = float(award) if award is not None else None
        except (TypeError, ValueError):
            award_value = None
        return cls(
            resident_ref=str(raw.get("resident_ref") or resident_ref),
            available=True,
            status=str(raw.get("status") or ""),
            benefit_code=str(raw.get("benefit_code") or ""),
            district=str(raw.get("district") or ""),
            award_monthly=award_value,
            household=[HouseholdMember.from_dict(m) for m in raw.get("household") or []],
            events=[CaseEvent.from_dict(e) for e in raw.get("events") or []],
            source=source,
            latency_ms=latency_ms,
        )

    @property
    def household_size(self) -> int:
        return len(self.household)

    def child_members(self, reference_date: Optional[Any] = None) -> list[HouseholdMember]:
        """Return household members confirmed to be under age 18."""
        return [m for m in self.household if m.is_under_18(reference_date) is True]

    def applies_section_3_9(self, reference_date: Optional[Any] = None) -> tuple[bool, str]:
        """Check whether Policy Amendment ACA-2026/2 s.3.9 applies.

        Returns:
            (applies: bool, reason: str)
            - True if a child under 18 is confirmed in the household composition.
            - True if household composition cannot be established (per ACA-2026/2 s.5.2 & ACA-2026/1 s.6.1).
            - False if household composition is established and contains no persons under 18.
        """
        if not self.available:
            return True, (
                "Household composition cannot be established because resident history was unavailable "
                f"({self.error or 'lookup failed'}). Under Policy Amendment ACA-2026/2 s.5.2 and "
                "ACA-2026/1 s.6.1, section 3.9 is treated as applying."
            )

        if not self.household:
            return True, (
                "Household composition is unrecorded/empty in Department records. Under Policy Amendment "
                "ACA-2026/2 s.5.2 and s.6.1, section 3.9 is treated as applying."
            )

        children = self.child_members(reference_date)
        if children:
            details = []
            for c in children:
                age = c.age_as_of(reference_date)
                age_str = f"age {age}" if age is not None else "under 18"
                details.append(f"{c.name or 'Unnamed'} ({c.relationship or 'Child'}, b. {c.date_of_birth}, {age_str})")
            return True, (
                f"Household includes {len(children)} person(s) under 18: {'; '.join(details)}. "
                "Safeguarding rule ACA-2026/2 s.3.9 prohibits automated drafting of triage notes; "
                "referral must be handed to a caseworker under s.3.2."
            )

        unknown_dob = [m for m in self.household if m.is_under_18(reference_date) is None]
        if unknown_dob:
            names = [m.name or "Unnamed member" for m in unknown_dob]
            return True, (
                f"Household member(s) with unconfirmed date of birth ({', '.join(names)}). "
                "Under ACA-2026/2 s.5.2, section 3.9 is treated as applying when composition cannot be established."
            )

        return False, "Household composition confirmed: all recorded members are 18 or older."

    def recent_events(self, limit: int = 4) -> list[CaseEvent]:
        """Most recent events last-first. Undated events sort to the end."""
        return sorted(self.events, key=lambda e: (e.date == "", e.date), reverse=True)[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resident_ref": self.resident_ref,
            "available": self.available,
            "status": self.status,
            "benefit_code": self.benefit_code,
            "district": self.district,
            "award_monthly": self.award_monthly,
            "household_size": self.household_size,
            "household": [m.to_dict() for m in self.household],
            "events": [e.to_dict() for e in self.events],
            "error": self.error,
            "source": self.source,
            "retrieved_at": self.retrieved_at,
            "latency_ms": self.latency_ms,
        }

    def digest(self) -> str:
        """Compact plain-text summary for the triage note and the escalation.

        Policy 4.2 requires an escalation to carry enough context for a
        supervisor to act "without re-reading the case from the beginning". This
        is that context.
        """
        if not self.available:
            return (
                f"Resident history for {self.resident_ref} could not be retrieved "
                f"({self.error or 'reason not recorded'}). A caseworker must open "
                f"the record before acting."
            )
        lines = [
            f"Resident {self.resident_ref} — status {self.status or 'unknown'}, "
            f"benefit {self.benefit_code or 'unknown'}, district {self.district or 'unknown'}.",
        ]
        if self.award_monthly is not None:
            lines.append(f"Current award: {self.award_monthly:,.2f} per month.")
        if self.household:
            lines.append(f"Household ({self.household_size}):")
            lines.extend(f"  - {m.one_line()}" for m in self.household)
        else:
            lines.append("Household: no members recorded.")
        recent = self.recent_events()
        if recent:
            lines.append(f"Most recent case events ({len(recent)} of {len(self.events)}):")
            lines.extend(f"  - {e.one_line()}" for e in recent)
        else:
            lines.append("Case events: none recorded.")
        if self.source != "api":
            lines.append(
                f"NOTE: retrieved from {self.source}, not the live Resident History API."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loading the queue
# ---------------------------------------------------------------------------

@dataclass
class ReferralLoadResult:
    referrals: list[Referral] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def loaded(self) -> int:
        return len(self.referrals)

    @property
    def skipped(self) -> int:
        return len(self.errors)


def load_referrals(queue_path: str, *, order_by_urgency: bool = True) -> ReferralLoadResult:
    """Load the overnight queue, skipping records that cannot be validated.

    Ordering is by the referring party's urgency, then by time received. That is
    a processing-order choice under section 2.3 (categorise or prioritise). It has
    no effect on authority -- section 6.2 forbids that -- so a High-urgency
    request for a section 3 action is escalated exactly like a Standard one.
    """
    import json

    result = ReferralLoadResult()
    with open(queue_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if not isinstance(raw, list):
        raise ValueError(
            f"{queue_path} must contain a JSON array of referral objects, "
            f"got {type(raw).__name__}"
        )

    for index, record in enumerate(raw):
        try:
            result.referrals.append(Referral.from_dict(record))
        except ReferralValidationError as exc:
            referral_id = record.get("referral_id", "") if isinstance(record, dict) else ""
            result.errors.append({
                "index": index,
                "referral_id": str(referral_id),
                "reason": str(exc),
            })

    if order_by_urgency:
        result.referrals.sort(key=lambda r: (r.urgency_rank, r.received_at, r.referral_id))
    return result
