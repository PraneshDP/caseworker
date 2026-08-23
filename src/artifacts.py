from __future__ import annotations
"""Artifact persistence — the durable side of `execute()`.

A demo where "execute" only appends a line to a log has an honesty problem: the
audit trail claims a notice was drafted and there is no notice. These helpers
write the actual effect to `data/artifacts/`, so an approved draft notice
produces a file a judge can open and a reviewer can check against what they
approved.

Deliberately a directory of files rather than a database: there is one writer,
no concurrent updates, no queries beyond "show me this run's artifacts", and the
files are human-readable without tooling. A database here would add a dependency
and a schema migration story to buy nothing.
"""

import json
import os
import re
from datetime import datetime
from typing import Any, Optional

from src.observability.logging_setup import get_logger, log_event

logger = get_logger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(part: str) -> str:
    """Make a path component safe. Case ids come from a JSON file we don't own."""
    cleaned = _SAFE.sub("_", str(part or "unknown")).strip("._-")
    return cleaned[:80] or "unknown"


def write_artifact(
    artifacts_dir: str,
    kind: str,
    case_id: str,
    action_id: str,
    data: dict[str, Any],
    *,
    run_id: str = "",
    body: Optional[str] = None,
) -> dict[str, str]:
    """Persist an executed effect.

    Writes a JSON record always, plus a plain-text rendering when `body` is
    given (the notice text a caseworker would actually read).

    Returns the paths written, for the audit entry.
    """
    run_dir = os.path.join(artifacts_dir, _safe(run_id) if run_id else "adhoc")
    os.makedirs(run_dir, exist_ok=True)

    stem = f"{_safe(kind)}_{_safe(case_id)}_{_safe(action_id)}"
    json_path = os.path.join(run_dir, f"{stem}.json")

    record = {
        "kind": kind,
        "run_id": run_id,
        "case_id": case_id,
        "action_id": action_id,
        "written_at": datetime.now().isoformat(),
        **data,
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, default=str)

    paths = {"json": json_path}
    if body is not None:
        text_path = os.path.join(run_dir, f"{stem}.txt")
        with open(text_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        paths["text"] = text_path

    log_event(logger, "artifact.written", kind=kind, case_id=case_id,
              action_id=action_id, run_id=run_id, **paths)
    return paths
