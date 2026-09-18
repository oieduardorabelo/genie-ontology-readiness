"""Unattended assessment behavior, with no Databricks or Lakebase calls."""

import io
import json
from copy import deepcopy
from types import SimpleNamespace
from zipfile import ZipFile
import csv
from unittest.mock import AsyncMock, Mock

import pytest
from pypdf import PdfReader

from server import pdf
from server.assessment import cli, reporting
from server.pillars import PILLARS
from functools import partial
from server.assessment.runtime import StandaloneServices


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
    client = SimpleNamespace(
        config=SimpleNamespace(
            warehouse_id="test-warehouse", host="https://workspace.example.com", profile="test-profile",
            auth_type="databricks-cli", authenticate=Mock(return_value={"Authorization": "test-auth"}),
        ),
        api_client=SimpleNamespace(do=Mock(return_value={
            "id": "test-user", "userName": "tester@example.com",
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "X-Databricks-Org-Id": "777",
            "token": "must-not-be-exported",
        })),
    )
    sql = AsyncMock(return_value=[{"readiness_preflight": 1}])
    assessment = AsyncMock(return_value=deepcopy(scorecard))
    engine = SimpleNamespace(run=assessment, resolved_sources=lambda: {"system_ok": False, "catalogs": ["gold", "silver"]})
    assess_factory = Mock(return_value=engine)
    models = SimpleNamespace(list_available_models=AsyncMock(return_value=[{"id": "test-chat-model"}]), close_llm_session=AsyncMock())
    services = StandaloneServices(client, SimpleNamespace(execute=sql), models, assess_factory)
    factory = Mock(return_value=services)
    monkeypatch.setattr(cli, "main", partial(cli.main, services_factory=factory))
    return SimpleNamespace(sql=sql, assessment=assessment, client=client, factory=factory,
                           assess_factory=assess_factory, models=models)


def test_complete_assessment_writes_matching_reports(tmp_path, runner, scorecard):
    assert cli.main(["--output-dir", str(tmp_path), "--title", "CI readiness"]) == 0
    sql, assessment = runner.sql, runner.assessment
    sql.assert_awaited_once_with("SELECT 1 AS readiness_preflight", force_sp=True, record=False)
    assessment.assert_awaited_once()
    actual = json.loads((tmp_path / "assessment.json").read_text())
    assert {key: actual[key] for key in scorecard} == scorecard
    assert actual["run_metadata"]["identity"]["type"] == "user"
    assert actual["run_metadata"]["scope"]["metadata"]["resolved_catalogs"] == ["gold", "silver"]
    assert "must-not-be-exported" not in (tmp_path / "assessment.json").read_text()
    assert (tmp_path / "readiness.md").read_text() == "# CI readiness\n\n" + reporting.assessment_markdown(actual)
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
        assert runner.assess_factory.call_args.args[1] == ["gold", "silver"]
        activity = runner.assess_factory.call_args.args[0]
        assert activity["mode"] == mode
        assert activity["workspace_ids"] == ["101", "102"]
        return scorecard

    runner.assessment.side_effect = assess
    assert cli.main([
        "--output-dir", str(tmp_path), "--catalogs", " gold, silver,gold, ",
        "--workspace-ids", "101,102", "--workspace-mode", mode,
    ]) == 0


@pytest.mark.parametrize("allow_partial, expected", [(False, 1), (True, 0)])
def test_unavailable_pillar_retains_artifacts_and_controls_exit(tmp_path, runner, allow_partial, expected, capsys):
    pillar = runner.assessment.return_value["pillars"][0]
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
        runner.client.config.warehouse_id = ""
    elif failure == "host":
        runner.client.config.host = ""
    elif failure == "auth":
        runner.client.config.authenticate.return_value = {}
    elif failure == "dependencies":
        def unavailable():
            raise pdf.PdfUnavailableError("Report dependencies unavailable.")
        monkeypatch.setattr(cli, "ensure_pdf_dependencies", unavailable)
    else:
        runner.sql.side_effect = RuntimeError("Warehouse unavailable.")
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    runner.assessment.assert_not_awaited()
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
    runner.client.config.warehouse_id = ""
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())


def test_execution_failure_is_nonzero_even_when_partial_allowed(tmp_path, runner):
    runner.assessment.side_effect = RuntimeError("Assessment failed.")
    assert cli.main(["--output-dir", str(tmp_path), "--allow-partial"]) == 1
    assert not list(tmp_path.iterdir())


def test_unwritable_output_prevents_assessment(tmp_path, runner):
    output = tmp_path / "file"
    output.write_text("occupied")
    assert cli.main(["--output-dir", str(output)]) == 1
    runner.assessment.assert_not_awaited()
    runner.sql.assert_not_awaited()


@pytest.mark.parametrize("title", ["", "a" * 201, "two\nlines"])
def test_invalid_title_fails_before_preflight(tmp_path, runner, title):
    with pytest.raises(SystemExit) as error:
        cli.main(["--output-dir", str(tmp_path), "--title", title])
    assert error.value.code == 2
    runner.sql.assert_not_awaited()


def test_default_scope_is_connected_workspace(tmp_path, runner, scorecard):
    async def assess():
        assert runner.assess_factory.call_args.args[1] is None
        assert runner.assess_factory.call_args.args[0] == {"mode": "include", "workspace_ids": ["777"], "selection": "current_workspace"}
        return scorecard

    runner.assessment.side_effect = assess
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


def test_connection_flags_reach_sdk_configuration(tmp_path, runner):
    assert cli.main([
        "--profile", "selected", "--host", "https://override.example.com", "--warehouse-id", "selected-warehouse",
        "--output-dir", str(tmp_path),
    ]) == 0
    runner.factory.assert_called_once_with("selected", "https://override.example.com", "selected-warehouse")


def test_all_workspaces_is_explicit(tmp_path, runner, scorecard):
    async def assess():
        assert runner.assess_factory.call_args.args[0]["mode"] == "all"
        return scorecard

    runner.assessment.side_effect = assess
    assert cli.main(["--all-workspaces", "--output-dir", str(tmp_path)]) == 0
    metadata = json.loads((tmp_path / "assessment.json").read_text())["run_metadata"]
    assert metadata["scope"]["activity"] == {"mode": "all", "workspace_ids": [], "selection": "all_workspaces"}


@pytest.mark.parametrize("args", [
    ["--all-workspaces", "--workspace-ids", "101"],
    ["--workspace-ids", " , "],
    ["--workspace-mode", "exclude"],
    ["--all-workspaces", "--workspace-mode", "exclude"],
    ["--model", "test-model"],
    ["--generate-plan", "--model", "invalid/path"],
])
def test_invalid_options_do_not_connect(tmp_path, runner, args):
    with pytest.raises(SystemExit) as error:
        cli.main(args + ["--output-dir", str(tmp_path)])
    assert error.value.code == 2
    runner.factory.assert_not_called()


def test_unknown_workspace_does_not_broaden_default_scope(tmp_path, runner):
    runner.client.api_client.do.return_value.pop("X-Databricks-Org-Id")
    assert cli.main(["--output-dir", str(tmp_path)]) == 2
    runner.assessment.assert_not_awaited()


def test_unavailable_identity_is_unknown_with_explicit_scope(tmp_path, runner):
    runner.client.api_client.do.side_effect = RuntimeError("Identity lookup unavailable.")
    assert cli.main(["--workspace-ids", "101", "--output-dir", str(tmp_path)]) == 0
    metadata = json.loads((tmp_path / "assessment.json").read_text())["run_metadata"]
    assert metadata["identity"] == {"type": "unknown", "id": None, "name": None}


def test_service_principal_is_recorded_accurately(tmp_path, runner):
    runner.client.api_client.do.return_value = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal"],
        "displayName": "CI assessment", "id": "test-sp", "applicationId": "application", "X-Databricks-Org-Id": "777",
    }
    assert cli.main(["--output-dir", str(tmp_path)]) == 0
    metadata = json.loads((tmp_path / "assessment.json").read_text())["run_metadata"]
    assert metadata["identity"] == {"type": "service_principal", "id": "test-sp", "name": "CI assessment"}


def test_resolved_identity_reaches_assessment_dependencies(tmp_path, runner):
    assert cli.main(["--output-dir", str(tmp_path)]) == 0
    assert runner.assess_factory.call_args.args[2] == {
        "type": "user", "id": "test-user", "name": "tester@example.com",
    }


def test_summary_and_per_pillar_csv_and_zip(tmp_path, runner):
    pillar = runner.assessment.return_value["pillars"][0]
    pillar["drill_down"] = {
        "columns": [
            {"key": "catalog", "label": "Catalog"}, {"key": "coverage", "label": "Coverage", "unit": "%"},
            {"key": "note", "label": "Note"}, {"key": "certified", "label": "Certified"},
        ],
        "rows": [{"catalog": "gold, silver", "coverage": 25, "note": 'Quoted "value"\nand another line', "certified": True},
                 {"catalog": None, "coverage": 0, "note": "≥ 25", "certified": False}],
    }
    assert cli.main(["--output-dir", str(tmp_path)]) == 0
    with (tmp_path / "csv" / "summary.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(PILLARS)
    assert rows[0]["Score"] == "25.0"
    assert rows[0]["Gaps"] == "Improve metadata coverage."
    with (tmp_path / "csv" / f"{pillar['key']}.csv").open(newline="") as stream:
        details = list(csv.DictReader(stream))
    assert details[0] == {"Catalog": "gold, silver", "Coverage (%)": "25", "Note": 'Quoted "value"\nand another line', "Certified": "true"}
    assert details[1]["Catalog"] == ""
    assert details[1]["Note"] == "≥ 25"
    with ZipFile(tmp_path / "readiness-csv.zip") as archive:
        assert set(archive.namelist()) == {"summary.csv", f"{pillar['key']}.csv"}
        assert archive.read("summary.csv") == (tmp_path / "csv" / "summary.csv").read_bytes()


def test_stale_csv_and_plan_outputs_are_removed(tmp_path, runner):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "old.csv").write_text("old")
    (tmp_path / "action-plan.md").write_text("old")
    (tmp_path / "action-plan.pdf").write_text("old")
    assert cli.main(["--output-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "csv" / "old.csv").exists()
    assert not (tmp_path / "action-plan.md").exists()
    assert not (tmp_path / "action-plan.pdf").exists()


def test_ai_is_not_called_without_plan_option(tmp_path, monkeypatch, runner):
    generate = AsyncMock()
    monkeypatch.setattr(cli, "generate_action_plan", generate)
    assert cli.main(["--output-dir", str(tmp_path)]) == 0
    generate.assert_not_awaited()
    runner.models.list_available_models.assert_not_awaited()


@pytest.mark.parametrize("explicit_model", [False, True])
def test_ai_plan_exports_and_model_selection(tmp_path, monkeypatch, runner, explicit_model):
    generate = AsyncMock(return_value="## Assessment summary\n\nOverall: 25.0/100\n\n## Where you are\n\nImprove metadata.\n")
    models = runner.models.list_available_models
    close = runner.models.close_llm_session
    monkeypatch.setattr(cli, "generate_action_plan", generate)
    args = ["--generate-plan", "--output-dir", str(tmp_path)]
    if explicit_model:
        args.extend(["--model", "test-chat-model"])
    assert cli.main(args) == 0
    generate.assert_awaited_once()
    assert generate.call_args.args[1] == "test-chat-model"
    assert "run_metadata" in generate.call_args.args[0]
    assert "AI model:** test-chat-model" in (tmp_path / "action-plan.md").read_text()
    assert "## Where you are" in (tmp_path / "action-plan.md").read_text()
    reader = PdfReader(tmp_path / "action-plan.pdf")
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Improve metadata" in text
    assert "test-chat-model" in text
    assert "tester@example.com" in text
    close.assert_awaited_once()
    if explicit_model:
        models.assert_not_awaited()


def test_ai_failure_retains_assessment_artifacts(tmp_path, monkeypatch, runner):
    monkeypatch.setattr(cli, "generate_action_plan", AsyncMock(side_effect=RuntimeError("Model unavailable.")))
    assert cli.main(["--generate-plan", "--model", "test-model", "--allow-partial", "--output-dir", str(tmp_path)]) == 1
    assert (tmp_path / "readiness.pdf").exists()
    assert (tmp_path / "readiness-csv.zip").exists()
    assert (tmp_path / "assessment.json").exists()
    assert not (tmp_path / "action-plan.md").exists()
