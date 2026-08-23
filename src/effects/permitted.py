from __future__ import annotations
"""The seven acts this system can actually perform.

One function per permitted provision of Authority Policy ACA-2026/1 section 2, and
nothing else. If an act is not in this file, this process cannot do it -- see
`src/effects/registry.py` for why that is a structural fact rather than a promise.

    2.1  read a referral from the overnight queue
    2.2  retrieve a resident's history, household composition, or case events
    2.3  categorise or prioritise a referral
    2.4  draft a triage note for caseworker review
    2.5  record that a referral has been read and triaged
    2.6  flag a referral for human attention, including as urgent
    2.7  identify an action it may not take and escalate it under section 4

Every one of them is a read, a classification, a draft, or a note to a colleague.
None of them changes a resident's award, moves money, or says anything to a
resident -- those are section 3, and there is no function here for them.

`build_permitted_effects()` closes over the run's dependencies (the history client,
the escalation writer, the settings) and returns the mapping the registry binds.
Effects receive an `EffectRequest` and nothing else, so an effect cannot reach a
system it was not handed.
"""

import json
import os
from datetime import datetime
from typing import Any, Callable

from src.effects.registry import EffectOutcome, EffectRequest
from src.escalation import EscalationPacket, EscalationWriter, IncompleteEscalationError
from src.observability.logging_setup import get_logger, log_event
from src.triage.note import build_note, categorise

logger = get_logger(__name__)


def _append_jsonl(path: str, record: dict[str, Any]) -> str:
    """Append one record to a JSONL file, creating the directory if needed."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 2.1 — read a referral from the overnight queue
# ---------------------------------------------------------------------------

def make_read_referral() -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.1 — read a referral from the overnight queue.

    Reading is the whole act. What it produces is the record that it happened,
    which is the first line of the section 5.1 trace.
    """
    def read_referral(request: EffectRequest) -> EffectOutcome:
        referral = request.referral
        fields_read = [
            "referral_id", "received_at", "resident_ref", "source",
            "summary", "requested_action", "urgency",
        ]
        return EffectOutcome(
            ok=True,
            action_kind="read_referral",
            summary=(
                f"Read {referral.referral_id} from the overnight queue "
                f"({referral.source or 'source not recorded'}, received "
                f"{referral.received_at or 'time not recorded'})."
            ),
            detail={
                "referral_id": referral.referral_id,
                "resident_ref": referral.resident_ref,
                "fields_read": fields_read,
                "requested_action": referral.requested_action,
                "referrer_urgency": referral.urgency,
                "redacted_fields": list(referral.redacted_fields),
                "read_at": _now(),
            },
            value=referral,
        )

    return read_referral


# ---------------------------------------------------------------------------
# 2.2 — retrieve a resident's history, household composition, or case events
# ---------------------------------------------------------------------------

def make_retrieve_history(client: Any) -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.2 — retrieve resident history.

    A failed lookup is a successful *effect* with an unavailable history: the read
    was attempted and the outcome recorded. Returning ok=False here would read
    downstream as "this step could not run", when what actually happened is "this
    step ran and the service had nothing for us". The distinction matters in the
    ledger.
    """
    def retrieve_resident_history(request: EffectRequest) -> EffectOutcome:
        referral = request.referral
        history = client.fetch(referral.resident_ref)

        if history.available:
            summary = (
                f"Retrieved history for {history.resident_ref} from "
                f"{history.source}: status {history.status or 'unknown'}, household "
                f"of {history.household_size}, {len(history.events)} case event(s)."
            )
        else:
            summary = (
                f"History for {referral.resident_ref} could not be retrieved: "
                f"{history.error}. Triage continues on the referral text alone."
            )

        return EffectOutcome(
            ok=True,
            action_kind="retrieve_resident_history",
            summary=summary,
            detail=history.to_dict(),
            value=history,
        )

    return retrieve_resident_history


# ---------------------------------------------------------------------------
# 2.3 — categorise or prioritise a referral
# ---------------------------------------------------------------------------

def make_categorise() -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.3 — categorise or prioritise."""
    def categorise_referral(request: EffectRequest) -> EffectOutcome:
        assessment = categorise(request.referral, request.history)
        return EffectOutcome(
            ok=True,
            action_kind="categorise_referral",
            summary=(
                f"Categorised as {assessment.category}, routed to "
                f"{assessment.routing}, priority {assessment.priority}."
            ),
            detail=assessment.to_dict(),
            value=assessment,
        )

    return categorise_referral


# ---------------------------------------------------------------------------
# 2.4 — draft a triage note for caseworker review
# ---------------------------------------------------------------------------

def make_draft_triage_note(
    *,
    settings: Any = None,
    artifacts_dir: str = "data/artifacts",
) -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.4 — draft a triage note.

    The note is written to disk as a file whose first line says it is a proposal,
    because s.2.4 is explicit that a drafted note has no effect on the case until a
    caseworker adopts it. Nothing in this function touches the resident's record.
    """
    def draft_triage_note(request: EffectRequest) -> EffectOutcome:
        referral = request.referral
        determination = request.payload.get("determination")
        assessment = request.payload.get("assessment")
        if determination is None:
            raise ValueError(
                "draft_triage_note requires the authority determination in the "
                "payload: the note states which provision applies, and code -- not "
                "the model -- decides that"
            )
        if assessment is None:
            assessment = categorise(referral, request.history)

        note = build_note(
            referral,
            determination,
            assessment,
            history=request.history,
            settings=settings,
            run_id=request.run_id,
            actor=request.actor,
            data_quality_notes=request.payload.get("data_quality_notes"),
        )

        run_dir = os.path.join(artifacts_dir, request.run_id or "adhoc")
        os.makedirs(run_dir, exist_ok=True)
        safe = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in referral.referral_id
        )
        md_path = os.path.join(run_dir, f"{safe}-triage-note.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(note.render_markdown())

        return EffectOutcome(
            ok=True,
            action_kind="draft_triage_note",
            summary=(
                f"Drafted a triage note for {referral.referral_id} "
                f"({note.narrative_source} narrative). It is a proposal and has no "
                f"effect until a caseworker adopts it."
            ),
            detail=note.to_dict(),
            artifacts=[md_path],
            value=note,
        )

    return draft_triage_note


# ---------------------------------------------------------------------------
# 2.5 — record that a referral has been read and triaged
# ---------------------------------------------------------------------------

def make_record_triaged(
    *, record_path: str = "data/logs/triage-record.jsonl"
) -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.5 — record that a referral has been read and triaged.

    Note the narrowness. This records that *triage happened*. It does not record a
    change of address, an income change, or anything else about the case -- those
    write to the record the award is calculated from, which is s.3.1. The rule in
    `authority-rules.json` is written narrowly for exactly that reason, so
    "Record change of address" does not match it.
    """
    def record_referral_triaged(request: EffectRequest) -> EffectOutcome:
        referral = request.referral
        payload = request.payload
        record = {
            "recorded_at": _now(),
            "run_id": request.run_id,
            "actor": request.actor,
            "referral_id": referral.referral_id,
            "resident_ref": referral.resident_ref,
            "requested_action": referral.requested_action,
            "read": True,
            "triaged": True,
            "category": payload.get("category", ""),
            "priority": payload.get("priority", ""),
            "routing": payload.get("routing", ""),
            "authority": payload.get("authority", ""),
            "provision": payload.get("provision", ""),
            "escalated": bool(payload.get("escalated")),
            "declined": payload.get("declined", []),
            "history_source": payload.get("history_source", ""),
            "note_artifact": payload.get("note_artifact", ""),
            "record_scope": (
                "This entry records that the referral was read and triaged. It "
                "records no change to the resident's case."
            ),
        }
        path = _append_jsonl(record_path, record)
        return EffectOutcome(
            ok=True,
            action_kind="record_referral_triaged",
            summary=(
                f"Recorded that {referral.referral_id} was read and triaged. No "
                f"change to the resident's case was recorded."
            ),
            detail=record,
            artifacts=[path],
            value=record,
        )

    return record_referral_triaged


# ---------------------------------------------------------------------------
# 2.6 — flag a referral for human attention, including as urgent
# ---------------------------------------------------------------------------

def make_flag_for_attention(
    *, flag_path: str = "data/logs/flags.jsonl"
) -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.6 — flag for human attention.

    A flag is addressed to a colleague, never to the resident. The s.2.6 rule
    vetoes "contact the resident", "notify the resident" and "send", because
    reaching out to a resident is a communication under s.3.5 no matter how it is
    phrased in the request.
    """
    def flag_for_human_attention(request: EffectRequest) -> EffectOutcome:
        referral = request.referral
        payload = request.payload
        reason = payload.get("reason") or "Referred for human attention."
        urgent = bool(payload.get("urgent"))

        record = {
            "flagged_at": _now(),
            "run_id": request.run_id,
            "actor": request.actor,
            "referral_id": referral.referral_id,
            "resident_ref": referral.resident_ref,
            "urgent": urgent,
            "reason": reason,
            "for_attention_of": payload.get("routing", "Caseworker review"),
            "addressed_to": "Department staff",
            "not_a_resident_communication": (
                "This flag is internal. Contacting the resident would be a "
                "communication under s.3.5 and requires supervisor approval."
            ),
        }
        path = _append_jsonl(flag_path, record)
        return EffectOutcome(
            ok=True,
            action_kind="flag_for_human_attention",
            summary=(
                f"Flagged {referral.referral_id} for "
                f"{record['for_attention_of']}"
                + (" as URGENT" if urgent else "")
                + f". Reason: {reason}"
            ),
            detail=record,
            artifacts=[path],
            value=record,
        )

    return flag_for_human_attention


# ---------------------------------------------------------------------------
# 2.7 — identify an action it may not take and escalate under section 4
# ---------------------------------------------------------------------------

def make_escalate(
    *, writer: EscalationWriter
) -> Callable[[EffectRequest], EffectOutcome]:
    """s.2.7 and s.4 — escalate an action the assistant may not take.

    This is the one permitted act whose subject is a restricted action, and it is
    what makes refusal useful rather than merely correct. Naming the s.3 provision
    is not performing it.

    The writer validates against s.4.2 and raises `IncompleteEscalationError` if
    the packet would not let a supervisor act without re-reading the case. That
    surfaces as a failed effect, so an inadequate escalation is visible in the run
    rather than quietly filed.
    """
    def escalate_to_supervisor(request: EffectRequest) -> EffectOutcome:
        packet = request.payload.get("packet")
        if not isinstance(packet, EscalationPacket):
            raise ValueError(
                "escalate_to_supervisor requires an EscalationPacket in the payload"
            )

        try:
            paths = writer.write(packet)
        except IncompleteEscalationError as exc:
            log_event(logger, "escalation.incomplete", level=40,
                      referral_id=packet.referral_id, missing=exc.missing)
            return EffectOutcome(
                ok=False,
                action_kind="escalate_to_supervisor",
                summary=(
                    f"Escalation for {packet.referral_id} was NOT filed: it does not "
                    f"satisfy s.4.2. A supervisor could not act on it."
                ),
                detail={"missing": exc.missing, "referral_id": packet.referral_id},
                error=str(exc),
            )

        return EffectOutcome(
            ok=True,
            action_kind="escalate_to_supervisor",
            summary=(
                f"Escalated {packet.referral_id} to a supervisor under s.4: "
                f"s.{packet.provision} applies. No action was taken on the case."
            ),
            detail=packet.to_dict(),
            artifacts=[paths["markdown"], paths["json"]],
            value=packet,
        )
def make_handoff_caseworker(
    *, writer: Optional[Any] = None
) -> Callable[[EffectRequest], EffectOutcome]:
    """s.3.2 of ACA-2026/2 — hand off to caseworker with established context."""
    def handoff_to_caseworker(request: EffectRequest) -> EffectOutcome:
        packet = request.payload.get("packet")
        if packet is None:
            raise ValueError("handoff_to_caseworker requires a CaseworkerHandoffPacket in payload")

        artifacts = []
        if writer is not None:
            paths = writer.write(packet)
            artifacts = [paths["markdown"], paths["json"]]

        return EffectOutcome(
            ok=True,
            action_kind="handoff_to_caseworker",
            summary=(
                f"Handed off {packet.referral_id} to caseworker under ACA-2026/2 s.3.2. "
                f"Safeguarding: {packet.safeguarding_reason}"
            ),
            detail=packet.to_dict(),
            artifacts=artifacts,
            value=packet,
        )

    return handoff_to_caseworker


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def build_permitted_effects(
    *,
    history_client: Any,
    escalation_writer: EscalationWriter,
    handoff_writer: Optional[Any] = None,
    settings: Any = None,
    artifacts_dir: str = "data/artifacts",
    triage_record_path: str = "data/logs/triage-record.jsonl",
    flag_path: str = "data/logs/flags.jsonl",
) -> dict[str, Callable[[EffectRequest], EffectOutcome]]:
    """Build the effect map. Keys must be permitted action kinds or the registry refuses."""
    return {
        "read_referral": make_read_referral(),
        "retrieve_resident_history": make_retrieve_history(history_client),
        "categorise_referral": make_categorise(),
        "draft_triage_note": make_draft_triage_note(
            settings=settings, artifacts_dir=artifacts_dir
        ),
        "handoff_to_caseworker": make_handoff_caseworker(writer=handoff_writer),
        "record_referral_triaged": make_record_triaged(record_path=triage_record_path),
        "flag_for_human_attention": make_flag_for_attention(flag_path=flag_path),
        "escalate_to_supervisor": make_escalate(writer=escalation_writer),
    }
