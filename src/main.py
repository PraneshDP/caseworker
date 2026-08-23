import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
from dotenv import load_dotenv


@click.group()
def cli():
    """🏛️  Calder County — Automated Casework Assistant (ACA-2026/1 & ACA-2026/2)."""
    load_dotenv()


@cli.command("run-morning")
@click.option("--auto-approve", is_flag=True, default=False,
              help="Auto-approve all scored HITL gates (for automated tests)")
@click.option("--limit", "-n", type=int, default=None,
              help="Process only first N referrals")
@click.option("--actor", default="human:cli-caseworker",
              help="Identity of the operator running the morning routine")
def run_morning_cmd(auto_approve: bool, limit: int, actor: str):
    """Execute the morning routine across the overnight referral queue."""
    from src.config import get_settings
    from src.orchestrator import run_morning

    settings = get_settings()
    result = run_morning(
        settings,
        auto_approve=auto_approve,
        echo=True,
        actor=actor,
        referral_limit=limit,
    )
    click.echo(f"\n✅ Morning run complete. Ledger: {result.ledger_path}")


@cli.command("verify-guardrails")
def verify_guardrails():
    """Verify structural guardrails, capability invariants, and task reachability."""
    from src.config import get_settings
    from src.orchestrator import build_pipeline

    settings = get_settings()
    click.echo("\n🛡️  Verifying Structural Guardrails & Policy Invariants...")
    pipeline = build_pipeline(settings)
    report = pipeline.registry.verify()
    if report.ok:
        click.echo("  ✅ Effect Registry: 0 restricted or unknown actions are performable.")
    else:
        click.echo(f"  ❌ Effect Registry FAULT: {report.problems}")

    click.echo("\n📋 Task Guardrail Reachability Report:")
    for row in pipeline.guardrail_report():
        click.echo(f"  - Task: {row['task_id']} (order {row['order']}) | default: {row['default_action_kind']}")
        click.echo(f"    Can gate on score: {row['can_gate_on_score_alone']} | "
                   f"Can gate on signals: {row['can_gate_with_signals']} | max score: {row['max_score']}")

    click.echo("\n✅ Guardrail verification complete.\n")


@cli.command("capability")
def capability():
    """Print the assistant's authoritative capability statement."""
    from src.config import get_settings
    from src.orchestrator import build_pipeline

    settings = get_settings()
    pipeline = build_pipeline(settings)
    click.echo("\n" + pipeline.registry.capability_statement() + "\n")


@cli.command("list-tasks")
def list_tasks():
    """List all registered tasks in execution order."""
    from src.tasks import discover

    report = discover()
    tasks = report.ordered()
    click.echo(f"\n📋 Discovered tasks ({len(tasks)} in order):\n")
    for task in tasks:
        rp = task.risk_profile
        click.echo(f"  [{task.order}] {task.id} (s.{task.provision})")
        click.echo(f"      Description: {task.description}")
        click.echo(f"      Default action: {rp.default_action_kind}")
        click.echo(f"      Risk: reversibility={rp.reversibility}, scope={rp.scope_of_impact}, financial={rp.financial_impact}")
        click.echo()


@cli.command("verify-chain")
@click.argument("ledger_path")
def verify_chain_cmd(ledger_path: str):
    """Verify the hash chain of an audit ledger for tamper evidence."""
    from src.audit.log import verify_chain

    result = verify_chain(ledger_path)
    if result["valid"]:
        click.echo(f"\n✅ Chain valid: {result['records']} entries verified with 0 tampering.")
        click.echo(f"   Final hash: {result.get('final_hash', 'N/A')}\n")
    else:
        click.echo(f"\n❌ Chain INVALID at line {result.get('broken_at', '?')}: {result.get('error', 'Unknown error')}\n")


@cli.command("ingest")
@click.option("--source", default=None, help="Path to policy document markdown file")
def ingest_cmd(source: str):
    """Ingest policy document into ChromaDB and build RAG indices."""
    from src.config import get_settings
    from src.rag.ingest import ingest_policy

    settings = get_settings()
    src_path = source or settings.policy_document_path
    click.echo(f"\n📥 Ingesting policy document from {src_path}...")
    chunks, coll, model = ingest_policy(
        policy_path=src_path,
        chroma_persist_dir=settings.chroma_persist_dir,
        embedding_model_name=settings.embedding_model,
    )
    click.echo(f"✅ Ingested {len(chunks)} chunks into ChromaDB at {settings.chroma_persist_dir}.\n")


@cli.command("search")
@click.argument("query")
@click.option("--limit", "-k", type=int, default=3, help="Number of results to return")
def search_cmd(query: str, limit: int):
    """Search the policy knowledge base using hybrid BM25 + dense retrieval."""
    from src.config import get_settings
    from src.rag.ingest import ingest_policy
    from src.rag.retrieve import HybridRetriever

    settings = get_settings()
    chunks, collection, model = ingest_policy(
        policy_path=settings.policy_document_path,
        chroma_persist_dir=settings.chroma_persist_dir,
        embedding_model_name=settings.embedding_model,
    )
    retriever = HybridRetriever(
        chunks=chunks,
        collection=collection,
        model=model,
        final_top_k=limit,
    )
    results = retriever.retrieve(query)
    click.echo(f"\n🔍 Search results for: \"{query}\"\n")
    for i, r in enumerate(results, 1):
        click.echo(f"  [{i}] Clause: {r.chunk.clause_id or 'N/A'} | Heading: {r.chunk.heading}")
        click.echo(f"      RRF score: {r.rrf_score:.4f} (BM25 rank: {r.bm25_rank}, Dense rank: {r.dense_rank})")
        preview = r.chunk.content.replace("\n", " ")[:160]
        click.echo(f"      {preview}...\n")


@cli.command("serve")
@click.option("--host", default=None, help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", type=int, default=None, help="Port to listen on (default: 8000)")
def serve_cmd(host: str, port: int):
    """Launch the local caseworker web console."""
    import uvicorn
    from src.api.app import create_app
    from src.config import get_settings

    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port
    app, _ = create_app(settings)

    click.echo(f"\n🌐 Starting Caseworker Web Console on http://{bind_host}:{bind_port}...")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


if __name__ == "__main__":
    cli()


