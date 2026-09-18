"""Run a readiness assessment and write reports without a Databricks App.

From the repository root: PYTHONPATH=app python -m server.assessment.cli --help
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from server import config
from server.assessment.reporting import ASSESSMENT_TITLE, assessment_markdown, build_assessment_pdf_html
from server.assessment.scoring import run_assessment
from server.pdf import ensure_pdf_dependencies, render_pdf_bytes
from server.security import install_log_redaction, safe_error
from server.sql_client import execute_sql
from server.workspace_filter import set_catalog_scope, set_workspace_filter

logger = logging.getLogger(__name__)


def _csv(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assess Databricks readiness and export JSON, Markdown, and PDF.")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"), help="Report directory (default: reports).")
    parser.add_argument("--title", default=ASSESSMENT_TITLE, help="Report title.")
    parser.add_argument("--catalogs", type=_csv, default=None, help="Comma-separated catalog names; omit for engine defaults.")
    parser.add_argument("--workspace-ids", type=_csv, default=[], help="Comma-separated workspace IDs for activity scope.")
    parser.add_argument("--workspace-mode", choices=("include", "exclude"), default="include")
    parser.add_argument("--allow-partial", action="store_true", help="Allow unavailable pillars without failing the job.")
    return parser


async def _preflight() -> None:
    # Configuration is checked before any queries or report files are created.
    if not config.WAREHOUSE_ID.strip():
        raise ValueError("Set DATABRICKS_WAREHOUSE_ID to an existing SQL warehouse ID.")
    if not config.get_workspace_host():
        raise ValueError("Set DATABRICKS_HOST or configure a Databricks CLI profile.")
    if not config.get_auth_headers(force_sp=True):
        raise ValueError("Configure Databricks OAuth credentials, DATABRICKS_TOKEN, or a CLI profile.")
    ensure_pdf_dependencies()
    # Use the same Statement Execution API and identity as the probes. This is
    # read-only and checks warehouse connectivity and CAN USE before fan-out.
    await execute_sql("SELECT 1 AS readiness_preflight", force_sp=True, record=False)


async def _run(args: argparse.Namespace) -> int:
    config.set_user_token(None)
    set_catalog_scope(args.catalogs)
    set_workspace_filter(
        {"mode": args.workspace_mode, "workspace_ids": args.workspace_ids} if args.workspace_ids else None
    )
    try:
        # Clear existing report names before preflight so a failed rerun cannot
        # leave old results for CI to upload as if they came from this run.
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("assessment.json", "readiness.md", "readiness.pdf"):
            (args.output_dir / name).unlink(missing_ok=True)
    except Exception as exc:
        reference, _ = safe_error(exc, "assessment output directory", logger)
        print(f"Cannot prepare the report directory (reference {reference}).", file=sys.stderr)
        return 1

    try:
        await _preflight()
    except Exception as exc:
        reference, _ = safe_error(exc, "assessment configuration or warehouse preflight", logger)
        print(f"Assessment preflight failed. Check the configuration and logs (reference {reference}).", file=sys.stderr)
        return 2

    try:
        scorecard = await run_assessment()
        (args.output_dir / "assessment.json").write_text(
            json.dumps(scorecard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (args.output_dir / "readiness.md").write_text(
            f"# {args.title}\n\n" + assessment_markdown(scorecard), encoding="utf-8"
        )
        # JSON and Markdown remain available if PDF generation fails.
        (args.output_dir / "readiness.pdf").write_bytes(
            render_pdf_bytes(build_assessment_pdf_html(scorecard, args.title))
        )
    except Exception as exc:
        reference, _ = safe_error(exc, "assessment execution or report generation", logger)
        print(f"Assessment or report generation failed (reference {reference}).", file=sys.stderr)
        return 1

    unavailable = [p["name"] for p in scorecard["pillars"] if not p["available"]]
    print(f"Readiness: {scorecard['overall']['score']}/100. Reports: {args.output_dir.resolve()}")
    if unavailable:
        print("Unavailable pillars: " + ", ".join(unavailable), file=sys.stderr)
        if not args.allow_partial:
            print("Incomplete assessment. Fix access or availability, or use --allow-partial.", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.title.strip() or len(args.title) > 200 or "\n" in args.title or "\r" in args.title:
        parser.error("--title must contain 1–200 characters on a single line.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Assessment interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
