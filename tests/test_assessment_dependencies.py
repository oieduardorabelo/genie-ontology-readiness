"""Independent assessment scopes, source caches, and identity attribution."""

import asyncio
import os
from unittest.mock import AsyncMock

from server.assessment.probes import AssessmentProbes, ProbeDependencies
from server.assessment.scoring import _run_probe
from server.ai_client import ModelClient


def suite(catalog, workspace):
    execute = AsyncMock(return_value=[{"total": "1", "active_30d": "1"}])
    probes = AssessmentProbes(ProbeDependencies(
        execute_sql=execute, get_workspace_host=lambda: f"https://{workspace}.example.com",
        get_auth_headers=lambda: {"Authorization": workspace}, get_user_token=lambda: None,
        accessible_catalogs=AsyncMock(return_value=None), record_rest_identity=lambda: None,
        catalog_scope=(catalog,), workspace_filter={"mode": "include", "workspace_ids": [workspace]},
        identity={"ran_as": "user", "label": workspace},
    ))
    return probes, execute


def test_concurrent_assessments_keep_sources_scope_and_identity_separate():
    first, first_sql = suite("gold", "101")
    second, second_sql = suite("silver", "202")

    async def run():
        await asyncio.gather(first.prime_request_sources(), second.prime_request_sources())
        first_calls, second_calls = first_sql.await_count, second_sql.await_count
        await asyncio.gather(first._resolve_sources(), second._resolve_sources())
        assert (first_sql.await_count, second_sql.await_count) == (first_calls, second_calls)
        results = await asyncio.gather(_run_probe("genie_agents", first), _run_probe("genie_agents", second))
        assert results[0][1]["identity"]["label"] == "101"
        assert results[1][1]["identity"]["label"] == "202"

    asyncio.run(run())
    assert first.resolved_sources()["catalogs"] == ["gold"]
    assert second.resolved_sources()["catalogs"] == ["silver"]
    assert first_sql.await_args.kwargs["parameters"] == {"wsf_0": "101"}
    assert second_sql.await_args.kwargs["parameters"] == {"wsf_0": "202"}


def test_model_discovery_and_compatibility_caches_belong_to_each_client():
    first = ModelClient(lambda: "https://first.example.com", lambda: {})
    second = ModelClient(lambda: "https://second.example.com", lambda: {})
    first._cache_set("serving_models", [{"id": "first-model"}])
    first._temperature_unsupported.add("custom-model")
    assert second._cache_get("serving_models") is None
    assert second._supports_temperature("custom-model")
    assert not first._supports_temperature("custom-model")


def test_app_adapter_retains_user_authorization_and_fallback_attribution():
    from server import config
    from server.routes.dependencies import create_app_assessment
    from server.sql_client import start_identity_capture, record_identity
    from server.workspace_filter import set_catalog_scope, set_workspace_filter

    try:
        config.set_user_token("forwarded-viewer-token")
        set_catalog_scope(["app-catalog"])
        set_workspace_filter({"mode": "include", "workspace_ids": ["303"]})
        probes = create_app_assessment()
        start_identity_capture()
        record_identity("obo")
        record_identity("sp_fallback")
        assert probes.identity["via"] == "sp_fallback"
        assert probes.dependencies.catalog_scope == ("app-catalog",)
        assert probes.dependencies.workspace_filter["workspace_ids"] == ["303"]
        assert probes.dependencies.get_user_token() == "forwarded-viewer-token"
    finally:
        config.set_user_token(None)
        set_catalog_scope(None)
        set_workspace_filter(None)
        start_identity_capture()


def test_cli_import_does_not_load_app_routes_or_startup():
    import subprocess
    import sys
    subprocess.run([
        sys.executable, "-c",
        "import server.assessment.cli, sys; assert 'server.routes' not in sys.modules; assert 'app' not in sys.modules",
    ], check=True, env={**os.environ, "PYTHONPATH": "app"})
