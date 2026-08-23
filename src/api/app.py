from __future__ import annotations
"""Hand-written ASGI application — the caseworker web console.

WHY NO FRAMEWORK
----------------
The HTTP surface is twelve endpoints and one event stream. A raw ASGI callable
covers that in one readable file with zero new dependencies; uvicorn (already
installed) is the server. Adding FastAPI to serve a demo console would be
exactly the kind of unnecessary complexity this project set out to avoid.

SECURITY POSTURE — stated plainly rather than implied
-----------------------------------------------------
* Binds 127.0.0.1 by default. This console is a local operator tool.
* There is NO authentication. That is a deliberate, documented limitation, not
  an oversight: a real deployment needs caseworker identity from an IdP, because
  the audit trail's `actor` field is only as trustworthy as the login behind it.
  The console therefore records whatever reviewer name the operator supplies and
  labels it as self-asserted. See DECISIONS.md.
* Static file serving is path-contained against the `web/` directory; artifact
  reads are path-contained against `data/artifacts`.
* Every mutating endpoint is POST. Approvals are keyed by opaque action id and
  are single-use — the queue drops an action the moment it is resolved, so a
  replayed approval request returns 409 rather than executing twice.
"""

import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote

from src.api.runner import RunManager
from src.config import Settings
from src.observability.logging_setup import get_logger, log_event
from src.policy.authority import load_policy

logger = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

SSE_POLL_SECONDS = 0.35
SSE_KEEPALIVE_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Request / response plumbing
# ---------------------------------------------------------------------------

class Request:
    """Just enough request object for twelve endpoints."""

    def __init__(self, scope: dict, body: bytes, params: dict[str, str]):
        self.scope = scope
        self.body = body
        self.params = params
        self.method: str = scope["method"]
        self.path: str = scope["path"]
        self.query: dict[str, list[str]] = parse_qs(
            (scope.get("query_string") or b"").decode("utf-8")
        )

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def q_int(self, name: str, default: int) -> int:
        try:
            return int(self.q(name, str(default)))
        except ValueError:
            return default

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HttpError(400, f"Request body is not valid JSON: {exc}")
        if not isinstance(data, dict):
            raise HttpError(400, "Request body must be a JSON object.")
        return data


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body"):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def _send_bytes(send, status: int, content_type: str, payload: bytes,
                      extra_headers: Optional[list[tuple[bytes, bytes]]] = None) -> None:
    headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(payload)).encode("ascii")),
        # This console renders untrusted case text. A CSP that forbids inline
        # script and any remote origin means an injected <script> in a case note
        # cannot execute even if a rendering bug ever let markup through.
        (b"content-security-policy",
         b"default-src 'self'; script-src 'self'; style-src 'self'; "
         b"img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


async def _send_json(send, status: int, data: Any) -> None:
    payload = json.dumps(data, default=str).encode("utf-8")
    await _send_bytes(send, status, "application/json; charset=utf-8", payload)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

Route = tuple[str, "re.Pattern[str]", Callable]


def create_app(settings: Settings, manager: Optional[RunManager] = None):
    """Build the ASGI callable. Returns (app, manager)."""
    manager = manager or RunManager(settings)
    routes = _build_routes(manager, settings)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await _lifespan(scope, receive, send, manager)
            return
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope["method"]

        # SSE is handled before the generic dispatcher: it never returns a body
        # in one shot, so it cannot share the response helper.
        if path == "/api/events" and method == "GET":
            await _sse(scope, receive, send, manager)
            return

        try:
            matched_patterns = False
            allowed_methods = []
            for route_method, pattern, handler in routes:
                match = pattern.match(path)
                if not match:
                    continue
                matched_patterns = True
                allowed_methods.append(route_method)
                if route_method != method and not (method == "HEAD" and route_method == "GET"):
                    continue

                body = await _read_body(receive) if method in ("POST", "PUT", "PATCH") else b""
                request = Request(scope, body, {k: unquote(v) for k, v in
                                                match.groupdict().items() if v is not None})
                result = handler(request)
                if asyncio.iscoroutine(result):
                    result = await result
                status, payload = result
                if isinstance(payload, tuple):   # (content_type, bytes)
                    await _send_bytes(send, status, payload[0], payload[1])
                else:
                    await _send_json(send, status, payload)
                return

            if matched_patterns:
                await _send_json(send, 405, {
                    "error": f"{method} is not allowed on {path}.",
                    "allowed": allowed_methods,
                })
                return

            await _send_json(send, 404, {"error": f"No route for {method} {path}."})

        except HttpError as exc:
            await _send_json(send, exc.status, {"error": exc.message, **exc.extra})
        except Exception as exc:  # noqa: BLE001
            log_event(logger, "api.unhandled_error", level=40,
                      path=path, method=method,
                      error_type=type(exc).__name__, error=str(exc)[:500])
            await _send_json(send, 500, {
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "See data/logs/app.log for the full record.",
            })

    return app, manager


async def _lifespan(scope, receive, send, manager: RunManager) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            log_event(logger, "api.startup", web_dir=str(WEB_DIR))
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            manager.cancel()
            log_event(logger, "api.shutdown")
            await send({"type": "lifespan.shutdown.complete"})
            return


# ---------------------------------------------------------------------------
# Server-sent events
# ---------------------------------------------------------------------------

async def _sse(scope, receive, send, manager: RunManager) -> None:
    """Stream run events. The browser reconnects with ?since=<seq> and replays."""
    query = parse_qs((scope.get("query_string") or b"").decode("utf-8"))
    try:
        since = int(query.get("since", ["0"])[0])
    except ValueError:
        since = 0

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-cache, no-transform"),
            (b"connection", b"keep-alive"),
            (b"x-accel-buffering", b"no"),
        ],
    })

    disconnected = asyncio.Event()

    async def watch_disconnect():
        while not disconnected.is_set():
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
                return

    watcher = asyncio.ensure_future(watch_disconnect())
    idle = 0.0

    async def emit(chunk: str) -> None:
        await send({"type": "http.response.body",
                    "body": chunk.encode("utf-8"), "more_body": True})

    try:
        await emit(f"event: hello\ndata: {json.dumps(manager.state())}\n\n")
        while not disconnected.is_set():
            events = manager.events_since(since)
            if events:
                idle = 0.0
                for item in events:
                    since = item["seq"]
                    await emit(
                        f"id: {item['seq']}\n"
                        f"event: {item['event']}\n"
                        f"data: {json.dumps(item, default=str)}\n\n"
                    )
                await emit(f"event: state\ndata: {json.dumps(manager.state(), default=str)}\n\n")
            else:
                idle += SSE_POLL_SECONDS
                if idle >= SSE_KEEPALIVE_SECONDS:
                    idle = 0.0
                    # Comment frame: keeps proxies and the browser from timing out.
                    await emit(": keepalive\n\n")
            await asyncio.sleep(SSE_POLL_SECONDS)
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        disconnected.set()
        watcher.cancel()
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:  # noqa: BLE001 - client already gone
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _build_routes(manager: RunManager, settings: Settings) -> list[Route]:
    async def in_thread(fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ---- static ----------------------------------------------------------

    def index(_request: Request):
        return _static_file("index.html")

    def static(request: Request):
        return _static_file(request.params["path"])

    # ---- meta ------------------------------------------------------------

    def health(_request: Request):
        return 200, {
            "status": "ok",
            "pipeline_ready": manager.pipeline_ready(),
            "running": manager.is_running(),
            "pending_approvals": len(manager.pending()),
        }

    def config(_request: Request):
        return 200, manager.config_view()

    def guardrails(_request: Request):
        rows = manager.reachability()
        unreachable = [
            r["task_id"] for r in rows
            if not r["can_gate_on_score_alone"] and not r["can_gate_with_signals"]
        ]
        policy = load_policy(settings.policy_rules_path, settings.policy_document_path)
        restricted = sorted([r.action_kind for r in policy.restricted])
        return 200, {
            "threshold": settings.risk_threshold,
            "hard_blocked_actions": restricted,
            "tasks": rows,
            "unreachable_tasks": unreachable,
            "verdict": (
                "Every task can reach the review gate."
                if not unreachable else
                f"{len(unreachable)} task(s) can never reach the gate: "
                f"{', '.join(unreachable)}."
            ),
        }

    def cases(_request: Request):
        data = manager.cases_seed()
        live = {}
        for c in manager.cases_live():
            cid = c.get("referral_id") or c.get("id") or ""
            if cid:
                live[cid] = c
        for case in data.get("cases", []):
            cid = case.get("referral_id") or case.get("id") or ""
            if cid and cid in live:
                case["live"] = live[cid]
        return 200, data

    # ---- runs ------------------------------------------------------------

    def start_run(request: Request):
        body = request.json()
        actor = str(body.get("actor") or "").strip()
        if not actor:
            raise HttpError(400, "An `actor` is required — the audit trail records "
                                 "who reviewed each gated action.")
        if not actor.startswith(("human:", "system:")):
            actor = f"human:{actor}"
        limit = body.get("case_limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise HttpError(400, "`case_limit` must be an integer.")
            if limit < 1:
                raise HttpError(400, "`case_limit` must be at least 1.")
        timeout = body.get("timeout_seconds")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                raise HttpError(400, "`timeout_seconds` must be a number.")

        result = manager.start(
            auto_approve=bool(body.get("auto_approve", False)),
            case_limit=limit,
            actor=actor,
            timeout_seconds=timeout,
        )
        if not result.get("started"):
            raise HttpError(409, result.get("reason", "Could not start a run."))
        return 202, {"started": True, "state": manager.state()}

    def run_state(_request: Request):
        return 200, {
            "state": manager.state(),
            "pending": manager.pending(),
            "actions": manager.actions(),
        }

    def cancel_run(_request: Request):
        result = manager.cancel()
        if not result.get("cancelled"):
            raise HttpError(409, result.get("reason", "Nothing to cancel."))
        return 200, result

    def run_history(request: Request):
        return 200, {"runs": manager.run_history(limit=request.q_int("limit", 50))}

    def ledger(request: Request):
        data = manager.ledger(request.params["run_id"])
        if "error" in data and not data.get("entries"):
            raise HttpError(404, data["error"])
        return 200, data

    def verify(request: Request):
        data = manager.verify(request.params["run_id"])
        if "error" in data:
            raise HttpError(404, data["error"])
        return 200, data

    def artifacts(request: Request):
        return 200, {"artifacts": manager.artifacts(request.params["run_id"])}

    def artifact(request: Request):
        data = manager.read_artifact(request.params["run_id"], request.params["name"])
        if "error" in data:
            raise HttpError(404, data["error"])
        return 200, data

    # ---- approvals -------------------------------------------------------

    def pending(_request: Request):
        return 200, {"pending": manager.pending(), "state": manager.state()}

    def decide(request: Request):
        body = request.json()
        decision = str(body.get("decision") or "").strip()
        if not decision:
            raise HttpError(400, "`decision` is required (approve | reject | edit | skip).")
        edited = body.get("edited_payload")
        if edited is not None and not isinstance(edited, dict):
            raise HttpError(400, "`edited_payload` must be a JSON object.")
        result = manager.resolve(
            request.params["action_id"],
            decision,
            actor=str(body.get("actor") or "").strip(),
            reason=str(body.get("reason") or "").strip(),
            edited_payload=edited,
        )
        if not result.get("ok"):
            # 409: the action is no longer awaiting a decision. Distinct from 400
            # so a double-click in the UI is reported as "already resolved"
            # rather than as a malformed request.
            status = 400 if "Unknown decision" in result.get("error", "") \
                or "must include" in result.get("error", "") \
                or "requires a reason" in result.get("error", "") else 409
            raise HttpError(status, result["error"])
        return 200, result

    # ---- policy ----------------------------------------------------------

    async def policy_search(request: Request):
        query = request.q("q").strip()
        if not query:
            raise HttpError(400, "`q` is required.")
        from src.rag.ingest import ingest_policy
        from src.rag.retrieve import HybridRetriever

        def do_search(q: str):
            chunks, collection, model = ingest_policy(
                policy_path=settings.policy_document_path,
                chroma_persist_dir=settings.chroma_persist_dir,
                embedding_model_name=settings.embedding_model,
            )
            retriever = HybridRetriever(
                chunks=chunks,
                collection=collection,
                model=model,
                final_top_k=5,
            )
            return retriever.retrieve(q)

        results = await in_thread(do_search, query)
        return 200, {
            "query": query,
            "results": [
                {
                    "rank": i,
                    "rrf_score": round(r.rrf_score, 6),
                    "bm25_rank": r.bm25_rank,
                    "dense_rank": r.dense_rank,
                    "chunk_id": r.chunk.chunk_id,
                    "clause_id": r.chunk.clause_id,
                    "section_path": r.chunk.section_path,
                    "content": r.chunk.content,
                }
                for i, r in enumerate(results, start=1)
            ],
        }

    async def warmup(_request: Request):
        try:
            await in_thread(manager.ensure_pipeline)
        except Exception as exc:  # noqa: BLE001
            raise HttpError(503, f"Pipeline initialisation failed: "
                                 f"{type(exc).__name__}: {exc}")
        return 200, {"pipeline_ready": True, "config": manager.config_view()}

    async def chat(request: Request):
        body = request.json()
        query = str(body.get("query") or body.get("message") or "").strip()
        if not query:
            raise HttpError(400, "`query` or `message` is required.")
        run_id = body.get("run_id")
        history = body.get("history") or []

        from src.chat.assistant import CaseworkerChatbot
        chatbot = CaseworkerChatbot(settings, manager=manager)
        response = await in_thread(
            chatbot.answer,
            query,
            run_id=run_id,
            history=history,
        )
        return 200, response.to_dict()

    return [
        ("GET", re.compile(r"^/$"), index),
        ("GET", re.compile(r"^/static/(?P<path>.+)$"), static),
        ("GET", re.compile(r"^/api/health$"), health),
        ("GET", re.compile(r"^/api/config$"), config),
        ("GET", re.compile(r"^/api/guardrails$"), guardrails),
        ("GET", re.compile(r"^/api/cases$"), cases),
        ("POST", re.compile(r"^/api/warmup$"), warmup),
        ("POST", re.compile(r"^/api/runs$"), start_run),
        ("GET", re.compile(r"^/api/runs$"), run_history),
        ("GET", re.compile(r"^/api/runs/current$"), run_state),
        ("POST", re.compile(r"^/api/runs/current/cancel$"), cancel_run),
        ("GET", re.compile(r"^/api/runs/(?P<run_id>[^/]+)/ledger$"), ledger),
        ("GET", re.compile(r"^/api/runs/(?P<run_id>[^/]+)/verify$"), verify),
        ("GET", re.compile(r"^/api/runs/(?P<run_id>[^/]+)/artifacts$"), artifacts),
        ("GET", re.compile(r"^/api/runs/(?P<run_id>[^/]+)/artifacts/(?P<name>[^/]+)$"), artifact),
        ("GET", re.compile(r"^/api/approvals$"), pending),
        ("POST", re.compile(r"^/api/approvals/(?P<action_id>[^/]+)$"), decide),
        ("GET", re.compile(r"^/api/policy/search$"), policy_search),
        ("POST", re.compile(r"^/api/chat$"), chat),
    ]


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

def _static_file(relative: str):
    base = WEB_DIR.resolve()
    target = (base / relative).resolve()
    # Containment check. `..` in the URL must not walk out of web/.
    if not str(target).startswith(str(base) + "/") and target != base:
        raise HttpError(403, "Refused: path escapes the web directory.")
    if not target.is_file():
        raise HttpError(404, f"No such file: {relative}")
    content_type, _ = mimetypes.guess_type(str(target))
    return 200, (content_type or "application/octet-stream", target.read_bytes())
