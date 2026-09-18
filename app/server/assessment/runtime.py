"""Standalone composition root; no App routes, startup, or connection globals."""

import os
from dataclasses import dataclass
from collections.abc import Callable

from server.ai_client import ModelClient
from server.assessment.connection import create_workspace_client
from server.assessment.probes import AssessmentProbes, ProbeDependencies
from server.assessment.scoring import run_assessment
from server.bindings import CatalogBindingsClient
from server.sql_client import StatementExecutionClient


@dataclass
class AssessmentRunner:
    suite: AssessmentProbes

    async def run(self) -> dict:
        return await run_assessment(self.suite)

    def resolved_sources(self) -> dict | None:
        return self.suite.resolved_sources()


@dataclass
class StandaloneServices:
    workspace: object
    sql: StatementExecutionClient
    models: ModelClient
    assessment_factory: Callable


def create_standalone_services(profile: str | None, host: str | None, warehouse_id: str | None) -> StandaloneServices:
    workspace = create_workspace_client(profile, host, warehouse_id)
    host_provider = lambda: workspace.config.host
    auth_provider = lambda **kwargs: workspace.config.authenticate()
    sql = StatementExecutionClient(
        workspace.config.host, workspace.config.warehouse_id or "", auth_provider,
        os.environ.get("CATALOG_NAME", "system"), os.environ.get("SCHEMA_NAME", "information_schema"),
    )
    bindings = CatalogBindingsClient(host_provider, auth_provider, lambda: None)
    models = ModelClient(host_provider, auth_provider)
    default_catalogs = tuple(c.strip() for c in os.environ.get("ASSESS_CATALOGS", "").split(",") if c.strip())

    def assessment_factory(activity, catalogs, identity):
        kind = identity["type"]
        label = f"{kind.replace('_', ' ').title()}: {identity['name']}" if identity["name"] else "Configured identity (unresolved)"
        attribution = {
            "ran_as": kind, "label": label, "via": "configured_credentials",
            "detail": "Reads used the credentials configured for this standalone assessment.",
            "id": identity["id"],
        }
        return AssessmentRunner(AssessmentProbes(ProbeDependencies(
            execute_sql=sql.execute, get_workspace_host=host_provider, get_auth_headers=auth_provider,
            get_user_token=lambda: None, accessible_catalogs=bindings.accessible_catalogs,
            record_rest_identity=lambda: None, default_catalogs=default_catalogs,
            workspace_filter=None if activity["mode"] == "all" else activity,
            catalog_scope=tuple(catalogs) if catalogs else None, identity=attribution,
        )))

    return StandaloneServices(workspace, sql, models, assessment_factory)
