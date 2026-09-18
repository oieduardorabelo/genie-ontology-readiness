"""App composition adapters. Standalone entry points do not import this module."""

from fastapi import Request
from server.ai_client import ModelClient
from server import config
from server.assessment.probes import AssessmentProbes, ProbeDependencies
from server.bindings import accessible_catalogs
from server.sql_client import execute_sql, record_rest_identity, resolved_identity
from server.workspace_filter import get_workspace_filter, get_catalog_scope


def get_model_client(request: Request) -> ModelClient:
    return request.app.state.model_client


def create_app_assessment() -> AssessmentProbes:
    catalogs = get_catalog_scope()
    return AssessmentProbes(ProbeDependencies(
        execute_sql=execute_sql,
        get_workspace_host=config.get_workspace_host,
        get_auth_headers=config.get_auth_headers,
        get_user_token=config.get_user_token,
        accessible_catalogs=accessible_catalogs,
        record_rest_identity=record_rest_identity,
        identity_resolver=resolved_identity,
        default_catalogs=tuple(config.ASSESS_CATALOGS),
        workspace_filter=get_workspace_filter(),
        catalog_scope=tuple(catalogs) if catalogs else None,
    ))
