"""Resolve standalone connection settings without mixing named-profile credentials."""

import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config


class ProfileConfig(Config):
    """SDK configuration with isolated environment loading for a named profile.

    Override the SDK's environment-loading hook rather than mutating os.environ.
    Credentials and host come from the profile; operational defaults remain usable.
    """

    def __init__(self, *, environment: dict[str, str], **kwargs):
        self._environment = dict(environment)
        super().__init__(**kwargs)

    @classmethod
    def attributes(cls):
        # SDK metadata is declared on Config, not inherited through __dict__.
        return Config.attributes()

    def _load_from_env(self):
        retained = {"config_file", "databricks_cli_path", "warehouse_id", "debug_truncate_bytes", "debug_headers", "rate_limit"}
        for attribute in self.attributes():
            if self.profile and attribute.name not in retained:
                continue
            if attribute.name in self._inner:
                continue
            value = next((self._environment[name] for name in [attribute.env, *getattr(attribute, "env_aliases", [])]
                          if name and self._environment.get(name)), None)
            if value:
                setattr(self, attribute.name, value)


def create_workspace_client(profile: str | None, host: str | None, warehouse_id: str | None) -> WorkspaceClient:
    settings = ProfileConfig(
        environment=dict(os.environ), profile=profile, host=host,
        warehouse_id=warehouse_id, http_timeout_seconds=30,
    )
    return WorkspaceClient(config=settings)


def identity_from_response(response: dict) -> dict:
    """Keep only principal metadata; never copy the whole authentication response."""
    schemas = [str(schema).lower() for schema in response.get("schemas", [])]
    if response.get("applicationId") or any("serviceprincipal" in schema for schema in schemas):
        kind = "service_principal"
        name = response.get("displayName") or response.get("applicationId")
    elif response.get("userName") or any(schema.endswith(":user") for schema in schemas):
        kind = "user"
        name = response.get("userName") or response.get("displayName")
    else:
        kind, name = "unknown", None
    return {"type": kind, "id": response.get("id"), "name": name}
