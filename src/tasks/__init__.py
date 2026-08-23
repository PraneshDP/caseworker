from __future__ import annotations
"""Task registry — auto-discovers Task subclasses from this package.

Adding a step is: drop a file in this directory, subclass Task, set
`id`/`description`/`risk_profile`/`order`/`provision`, implement `plan()`. There is
no `execute()` to implement — effects live in `src/effects/`, and that separation is
what makes the approval gate structural rather than procedural. The orchestrator is
not edited, because it sorts by `order` and hands every task the same dependency
bundle via `configure()`.

The previous version wrapped instantiation in `except Exception: pass`, so a task
with a broken import or a constructor bug vanished silently from the pipeline. A
morning run would then look like a clean success while one of the seven steps simply
did not happen — which is exactly the failure a caseworker cannot detect. Failures
are now collected, logged as errors, and returned so the caller can refuse to run.
"""

import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.observability.logging_setup import get_logger, log_event
from src.tasks.base import Task

logger = get_logger(__name__)

SKIP_MODULES = frozenset({"base"})


@dataclass
class DiscoveryReport:
    """What discovery found, and what it could not load."""
    tasks: dict[str, Task] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def ordered(self) -> list[Task]:
        """Tasks in pipeline order. Ties break on id so runs are reproducible."""
        return sorted(self.tasks.values(), key=lambda t: (t.order, t.id))


def discover(package_dir: Optional[Path] = None) -> DiscoveryReport:
    """Scan this package for Task subclasses.

    Returns a report rather than raising: the caller decides whether a partially
    loadable pipeline is acceptable. `caseworker run-morning` refuses; a unit
    test may not care.
    """
    report = DiscoveryReport()
    package_dir = package_dir or Path(__file__).parent

    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if module_info.name in SKIP_MODULES or module_info.name.startswith("_"):
            continue

        module_name = f"src.tasks.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            report.failures.append({
                "module": module_name,
                "stage": "import",
                "error": f"{type(exc).__name__}: {exc}",
            })
            log_event(logger, "task_registry.import_failed", level=40,
                      module=module_name, error_type=type(exc).__name__,
                      error=str(exc)[:500])
            continue

        for _, attr in inspect.getmembers(module, inspect.isclass):
            if not issubclass(attr, Task) or attr is Task:
                continue
            # Only take classes defined in this module, so a task that imports
            # another task is not registered twice.
            if attr.__module__ != module_name:
                continue
            if inspect.isabstract(attr):
                continue

            try:
                instance = attr()
            except Exception as exc:  # noqa: BLE001
                report.failures.append({
                    "module": module_name,
                    "stage": "instantiate",
                    "task_class": attr.__name__,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                log_event(logger, "task_registry.instantiation_failed", level=40,
                          module=module_name, task_class=attr.__name__,
                          error_type=type(exc).__name__, error=str(exc)[:500])
                continue

            if not instance.id:
                report.failures.append({
                    "module": module_name,
                    "stage": "validate",
                    "task_class": attr.__name__,
                    "error": "Task has an empty `id`; it cannot be registered or audited.",
                })
                log_event(logger, "task_registry.invalid_task", level=40,
                          module=module_name, task_class=attr.__name__,
                          error="empty id")
                continue

            if instance.id in report.tasks:
                existing = type(report.tasks[instance.id]).__name__
                report.failures.append({
                    "module": module_name,
                    "stage": "validate",
                    "task_class": attr.__name__,
                    "error": (
                        f"Duplicate task id {instance.id!r} — already registered by "
                        f"{existing}. Ids must be unique; the audit trail keys on them."
                    ),
                })
                log_event(logger, "task_registry.duplicate_id", level=40,
                          task_id=instance.id, existing=existing,
                          duplicate=attr.__name__)
                continue

            report.tasks[instance.id] = instance

    log_event(logger, "task_registry.discovered",
              task_count=len(report.tasks),
              task_ids=[t.id for t in report.ordered()],
              failure_count=len(report.failures))
    return report


def discover_tasks() -> dict[str, Task]:
    """Backwards-compatible view: {task_id: instance}, in pipeline order.

    Prefer `discover()` — it reports what failed to load.
    """
    report = discover()
    return {task.id: task for task in report.ordered()}
