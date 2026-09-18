"""Run a readiness assessment and write reports without a Databricks App.

From the repository root: PYTHONPATH=app python -m server.assessment.cli --help
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from server.action_plan import PLAN_TITLE, _build_plan_pdf_html, _strip_document_h1, generate_action_plan
from server.ai_client import resolve_default_model
from server.assessment.connection import identity_from_response
from server.assessment.runtime import StandaloneServices, create_standalone_services
from server.assessment.exports import export_csvs
from server.assessment.reporting import (
    ASSESSMENT_TITLE, assessment_context_markdown, assessment_markdown, build_assessment_pdf_html,
)
from server.pdf import ensure_pdf_dependencies, render_pdf_bytes
from server.security import install_log_redaction, safe_error

logger = logging.getLogger(__name__)


def _csv(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assess Databricks readiness and export JSON, Markdown, PDF, and CSV.")
    parser.add_argument("--profile", help="Databricks CLI profile (default: DATABRICKS_CLI_PROFILE or SDK configuration).")
    parser.add_argument("--host", help="Workspace URL; overrides the selected profile or environment host.")
    parser.add_argument("--warehouse-id", help="SQL warehouse ID; overrides DATABRICKS_WAREHOUSE_ID or profile configuration.")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"), help="Report directory (default: reports).")
    parser.add_argument("--title", default=ASSESSMENT_TITLE, help="Report title.")
    parser.add_argument("--catalogs", type=_csv, default=None, help="Comma-separated catalog names; omit for engine defaults.")
    workspace = parser.add_mutually_exclusive_group()
    workspace.add_argument("--workspace-ids", type=_csv, help="Comma-separated workspace IDs (default: connected workspace).")
    workspace.add_argument("--all-workspaces", action="store_true", help="Read activity across all visible workspaces.")
    parser.add_argument("--workspace-mode", choices=("include", "exclude"), default="include")
    parser.add_argument("--allow-partial", action="store_true", help="Allow unavailable pillars without failing the job.")
    parser.add_argument("--generate-plan", action="store_true", help="Generate an AI action plan using workspace model serving.")
    parser.add_argument("--model", help="Chat serving endpoint for the AI plan; omit to use the App's model selection.")
    return parser


async def _preflight(args: argparse.Namespace, services: StandaloneServices) -> dict:
    profile = args.profile or os.environ.get("DATABRICKS_CLI_PROFILE") or None
    client = services.workspace
    warehouse_id = client.config.warehouse_id or ""
    if not warehouse_id.strip():
        raise ValueError("Provide --warehouse-id, DATABRICKS_WAREHOUSE_ID, or a profile warehouse_id.")
    if not client.config.host:
        raise ValueError("Set DATABRICKS_HOST or configure a Databricks CLI profile.")
    if not client.config.authenticate():
        raise ValueError("Configure Databricks OAuth credentials, DATABRICKS_TOKEN, or a CLI profile.")
    ensure_pdf_dependencies()
    response = {}
    try:
        response = await asyncio.to_thread(
            client.api_client.do, "GET", "/api/2.0/preview/scim/v2/Me",
            response_headers=["X-Databricks-Org-Id"],
        )
    except Exception as exc:
        reference, _ = safe_error(exc, "configured identity lookup", logger)
        logger.warning("Identity metadata could not be resolved (reference %s).", reference)
    identity = identity_from_response(response)
    current_workspace_id = response.get("X-Databricks-Org-Id")
    if args.all_workspaces:
        activity = {"mode": "all", "workspace_ids": [], "selection": "all_workspaces"}
    else:
        ids = args.workspace_ids
        selection = "explicit" if ids else "current_workspace"
        if ids is None:
            if not current_workspace_id:
                raise ValueError("Could not resolve the connected workspace. Provide --workspace-ids or --all-workspaces.")
            ids = [str(current_workspace_id)]
        activity = {"mode": args.workspace_mode, "workspace_ids": ids, "selection": selection}
    # Use the same Statement Execution API and identity as the probes. This is
    # read-only and checks warehouse connectivity and CAN USE before fan-out.
    await services.sql.execute("SELECT 1 AS readiness_preflight", force_sp=True, record=False)
    return {
        "connection": {
            "profile": client.config.profile or profile,
            "host": client.config.host,
            "warehouse_id": warehouse_id,
            "auth_type": client.config.auth_type,
            "current_workspace_id": str(current_workspace_id) if current_workspace_id else None,
        },
        "identity": identity,
        "scope": {"activity": activity, "metadata": {"requested_catalogs": args.catalogs}},
    }


async def _run(args: argparse.Namespace, services_factory) -> int:
    try:
        # Clear existing report names before preflight so a failed rerun cannot
        # leave old results for CI to upload as if they came from this run.
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("assessment.json", "readiness.md", "readiness.pdf", "readiness-csv.zip", "action-plan.md", "action-plan.pdf"):
            (args.output_dir / name).unlink(missing_ok=True)
        for path in (args.output_dir / "csv").glob("*.csv"):
            path.unlink()
    except Exception as exc:
        reference, _ = safe_error(exc, "assessment output directory", logger)
        print(f"Cannot prepare the report directory (reference {reference}).", file=sys.stderr)
        return 1

    services = None
    try:
        profile = args.profile or os.environ.get("DATABRICKS_CLI_PROFILE") or None
        services = await asyncio.to_thread(services_factory, profile, args.host, args.warehouse_id)
        metadata = await _preflight(args, services)
    except Exception as exc:
        if services is not None:
            await services.models.close_llm_session()
        reference, _ = safe_error(exc, "assessment configuration or warehouse preflight", logger)
        print(f"Assessment preflight failed. Check the configuration and logs (reference {reference}).", file=sys.stderr)
        return 2

    try:
        return await _assess_and_export(args, services, metadata)
    finally:
        await services.models.close_llm_session()


async def _assess_and_export(args, services, metadata) -> int:
    try:
        assessment = services.assessment_factory(metadata["scope"]["activity"], args.catalogs, metadata["identity"])
        scorecard = await assessment.run()
        sources = assessment.resolved_sources()
        metadata["scope"]["metadata"].update({
            "resolved_catalogs": list(sources["catalogs"]) if sources is not None else None,
            "source": ("system.information_schema" if sources["system_ok"] else "catalog_information_schema")
            if sources is not None else None,
        })
        scorecard = {**scorecard, "run_metadata": metadata}
        (args.output_dir / "assessment.json").write_text(
            json.dumps(scorecard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (args.output_dir / "readiness.md").write_text(
            f"# {args.title}\n\n" + assessment_markdown(scorecard), encoding="utf-8"
        )
        export_csvs(scorecard, args.output_dir)
        # JSON and Markdown remain available if PDF generation fails.
        (args.output_dir / "readiness.pdf").write_bytes(
            render_pdf_bytes(build_assessment_pdf_html(scorecard, args.title))
        )
    except Exception as exc:
        reference, _ = safe_error(exc, "assessment execution or report generation", logger)
        print(f"Assessment or report generation failed (reference {reference}).", file=sys.stderr)
        return 1

    if args.generate_plan:
        try:
            model = args.model or await resolve_default_model(await services.models.list_available_models())
            plan = await generate_action_plan(scorecard, model, services.models)
            body = assessment_context_markdown(scorecard) + f"**AI model:** {model}\n\n" + _strip_document_h1(plan)
            (args.output_dir / "action-plan.md").write_text(f"# {PLAN_TITLE}\n\n" + body, encoding="utf-8")
            (args.output_dir / "action-plan.pdf").write_bytes(render_pdf_bytes(_build_plan_pdf_html(body, PLAN_TITLE)))
            print(f"AI action plan generated using {model}.")
        except Exception as exc:
            reference, _ = safe_error(exc, "AI action plan generation or export", logger)
            print(f"AI action plan failed; assessment reports were retained (reference {reference}).", file=sys.stderr)
            return 1

    unavailable = [p["name"] for p in scorecard["pillars"] if not p["available"]]
    print(f"Readiness: {scorecard['overall']['score']}/100. Reports: {args.output_dir.resolve()}")
    if unavailable:
        print("Unavailable pillars: " + ", ".join(unavailable), file=sys.stderr)
        if not args.allow_partial:
            print("Incomplete assessment. Fix access or availability, or use --allow-partial.", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None, *, services_factory=create_standalone_services) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.title.strip() or len(args.title) > 200 or "\n" in args.title or "\r" in args.title:
        parser.error("--title must contain 1–200 characters on a single line.")
    if args.workspace_ids is not None and not args.workspace_ids:
        parser.error("--workspace-ids requires at least one workspace ID.")
    if args.workspace_mode == "exclude" and not args.workspace_ids:
        parser.error("--workspace-mode exclude requires explicit --workspace-ids.")
    if args.model and not args.generate_plan:
        parser.error("--model requires --generate-plan.")
    if args.model and (len(args.model) > 200 or any(not (c.isalnum() or c in "_.-") for c in args.model)):
        parser.error("--model must be a chat serving endpoint name.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    try:
        return asyncio.run(_run(args, services_factory))
    except KeyboardInterrupt:
        print("Assessment interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
