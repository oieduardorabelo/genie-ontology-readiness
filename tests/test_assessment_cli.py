"""Unattended assessment behavior, with no Databricks or Lakebase calls."""

import io
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader

from server import config, pdf
from server.assessment import cli, reporting
from server.pillars import PILLARS
from server.workspace_filter import get_catalog_scope, get_workspace_filter


@pytest.fixture
def scorecard():
    return {
        "overall": {
            "score": 25.0,
            "level": 1,
            "level_label": "Foundation",
            "readiness_stage": "Foundation building",
            "assessed_at": "2026-09-18T00:00:00+00:00",
        },
        "pillars": [
            {
                "key": p["key"], "name": p["name"], "score": 25.0, "weight": p["weight"],
                "available": True, "level": 1, "level_label": "Foundation",
                "signals": [{"label": "Coverage", "value": 25, "unit": "%"}],
                "gaps": ["Improve metadata coverage."], "best_practices": ["Document important tables."],
            }
            for p in PILLARS
        ],
        "top_gaps": [{"pillar": PILLARS[0]["name"], "gap": "Improve metadata coverage."}],
    }


@pytest.fixture
def runner(monkeypatch, scorecard):
    monkeypatch.setattr(config, "WAREHOUSE_ID", "test-warehouse")
    monkeypatch.setattr(config, "get_workspace_host", lambda: "https://workspace.example.com")
    monkeypatch.setattr(config, "get_auth_headers", lambda **kw: {"Authorization": "test-auth"})
    sql = AsyncMock(return_value=[{"readiness_preflight": 1}])
    assessment = AsyncMock(return_value=deepcopy(scorecard))
    monkeypatch.setattr(cli, "execute_sql", sql)
    monkeypatch.setattr(cli, "run_assessment", assessment)
    return sql, assessment


def test_complete_assessment_writes_matching_reports(tmp_path, runner, scorecard):
    assert cli.main(["--output-dir", str(tmp_path), "--title", "CI readiness"]) == 0
    sql, assessment = runner
    sql.assert_awaited_once_with("SELECT 1 AS readiness_preflight", force_sp=True, record=False)
    assessment.assert_awaited_once()
    assert json.loads((tmp_path / "assessment.json").read_text()) == scorecard
    assert (tmp_path / "readiness.md").read_text() == "# CI readiness\n\n" + reporting.assessment_markdown(scorecard)
    content = (tmp_path / "readiness.pdf").read_bytes()
    assert content.startswith(b"%PDF-")
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)
    assert text.count("CI readiness") == 1
    assert "25.0/100" in text
    for pillar in scorecard["pillars"]:
        assert pillar["name"] in text


@pytest.mark.parametrize("mode", ["include", "exclude"])
def test_scope_reaches_assessment(tmp_path, runner, scorecard, mode):
    async def assess():
        assert get_catalog_scope() == ["gold", "silver"]
        assert get_workspace_filter() == {"mode": mode, "workspace_ids": ["101", "102"]}
        assert config.get_user_token() is None
        return scorecard

    runner[1].side_effect = assess
    assert cli.main([
        "--output-dir", str(tmp_path), "--catalogs", " gold, silver,gold, ",
        "--workspace-ids", "101,102", "--workspace-mode", mode,
    ]) == 0


@pytest.mark.parametrize("allow_partial, expected", [(False, 1), (True, 0)])
def test_unavailable_pillar_retains_artifacts_and_controls_exit(tmp_path, runner, allow_partial, expected, capsys):
    pillar = runner[1].return_value["pillars"][0]
    pillar.update(available=False, score=0, unavailable_reason="insufficient_permission", note="Access unavailable.")
    args = ["--output-dir", str(tmp_path)] + (["--allow-partial"] if allow_partial else [])
    assert cli.main(args) == expected
    assert "Unavailable pillars" in capsys.readouterr().err
    assert "not available" in (tmp_path / "readiness.md").read_text()
    assert (tmp_path / "readiness.pdf").read_bytes().startswith(b"%PDF-")
    assert json.loads((tmp_path / "assessment.json").read_text())["pillars"][0]["score"] == 0


@pytest.mark.parametrize("failure", ["warehouse", "host", "auth", "dependencies", "warehouse_access"])
def test_preflight_failure_prevents_assessment(tmp_path, monkeypatch, runner, failure):
    if failure == "warehouse":
        monkeypatch.setattr(config, "WAREHOUSE_ID", "")
    elif failure == "host":
        monkeypatch.setattr(config, "get_workspace_host", lambda: "")
    elif failure == "auth":
        monkeypatch.setattr(config, "get_auth_headers", lambda **kw: {})
    elif failure == "dependencies":
        def unavailable():
            raise pdf.PdfUnavailableError("Report dependencies unavailable.")
        monkeypatch.setattr(cli, "ensure_pdf_dependencies", unavailable)
    else:
        runner[0].side_effect = RuntimeError("Warehouse unavailable.")
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    runner[1].assert_not_awaited()
    assert not list(tmp_path.iterdir())


def test_pdf_failure_preserves_json_and_markdown_and_removes_old_pdf(tmp_path, monkeypatch, runner):
    (tmp_path / "readiness.pdf").write_bytes(b"old-report")

    def fail(html):
        raise pdf.PdfRenderError("PDF render failed.")

    monkeypatch.setattr(cli, "render_pdf_bytes", fail)
    assert cli.main(["--output-dir", str(tmp_path), "--allow-partial"]) == 1
    assert (tmp_path / "assessment.json").exists()
    assert (tmp_path / "readiness.md").exists()
    assert not (tmp_path / "readiness.pdf").exists()


def test_preflight_failure_removes_reports_from_previous_run(tmp_path, monkeypatch, runner):
    for name in ("assessment.json", "readiness.md", "readiness.pdf"):
        (tmp_path / name).write_text("old-report")
    monkeypatch.setattr(config, "WAREHOUSE_ID", "")
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())


def test_execution_failure_is_nonzero_even_when_partial_allowed(tmp_path, runner):
    runner[1].side_effect = RuntimeError("Assessment failed.")
    assert cli.main(["--output-dir", str(tmp_path), "--allow-partial"]) == 1
    assert not list(tmp_path.iterdir())


def test_unwritable_output_prevents_assessment(tmp_path, runner):
    output = tmp_path / "file"
    output.write_text("occupied")
    assert cli.main(["--output-dir", str(output)]) == 1
    runner[1].assert_not_awaited()
    runner[0].assert_not_awaited()


@pytest.mark.parametrize("title", ["", "a" * 201, "two\nlines"])
def test_invalid_title_fails_before_preflight(tmp_path, runner, title):
    with pytest.raises(SystemExit) as error:
        cli.main(["--output-dir", str(tmp_path), "--title", title])
    assert error.value.code == 2
    runner[0].assert_not_awaited()


def test_default_scope_uses_engine_defaults(tmp_path, runner, scorecard):
    async def assess():
        assert get_catalog_scope() is None
        assert get_workspace_filter() is None
        return scorecard

    runner[1].side_effect = assess
    assert cli.main(["--output-dir", str(tmp_path)]) == 0


def test_http_pdf_export_uses_shared_renderer(monkeypatch):
    monkeypatch.setattr(pdf, "render_pdf_bytes", lambda html: b"%PDF-test")
    response = pdf.render_pdf_response("<h1>Assessment</h1>", "Assessment")
    assert response.status_code == 200
    assert response.body == b"%PDF-test"
    assert response.media_type == "application/pdf"


@pytest.mark.parametrize("error, status", [
    (pdf.PdfUnavailableError("missing"), 503),
    (pdf.PdfRenderError("failed"), 500),
    (pdf.ExternalResourceBlocked("file:///private"), 400),
])
def test_shared_renderer_http_failure_mapping(monkeypatch, error, status):
    def fail(html):
        raise error

    monkeypatch.setattr(pdf, "render_pdf_bytes", fail)
    assert pdf.render_pdf_response("<h1>Assessment</h1>", "Assessment").status_code == status


def test_renderer_retains_external_resource_guard(monkeypatch):
    from xhtml2pdf import pisa

    def render(**kwargs):
        kwargs["link_callback"]("file:///private", "")

    monkeypatch.setattr(pisa, "CreatePDF", render)
    with pytest.raises(pdf.ExternalResourceBlocked):
        pdf.render_pdf_bytes("<h1>Assessment</h1>")


def test_missing_pdf_dependency_is_reported(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "markdown", None)
    with pytest.raises(pdf.PdfUnavailableError):
        pdf.ensure_pdf_dependencies()
