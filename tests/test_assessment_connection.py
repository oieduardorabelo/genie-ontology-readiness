"""Named profiles are isolated without changing another entry point's state."""

import os
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

from server.assessment import connection
from server.sql_client import StatementExecutionClient


def test_named_profile_filters_environment_without_mutating_it(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "ambient-credential")
    monkeypatch.setenv("DATABRICKS_HOST", "https://ambient.example.com")
    environment = dict(os.environ, DATABRICKS_WAREHOUSE_ID="environment-warehouse")
    settings = object.__new__(connection.ProfileConfig)
    settings._inner = {"profile": "selected", "host": "https://override.example.com"}
    settings._environment = environment
    settings._load_from_env()
    assert settings.host == "https://override.example.com"
    assert settings.token is None
    assert settings.warehouse_id == "environment-warehouse"
    assert os.environ["DATABRICKS_TOKEN"] == "ambient-credential"
    assert os.environ["DATABRICKS_HOST"] == "https://ambient.example.com"


def test_unnamed_connection_uses_standard_environment_values():
    settings = object.__new__(connection.ProfileConfig)
    settings._inner = {}
    settings._environment = {"DATABRICKS_HOST": "https://ambient.example.com", "DATABRICKS_TOKEN": "ambient"}
    settings._load_from_env()
    assert settings.host == "https://ambient.example.com"
    assert settings.token == "ambient"


def test_connection_flags_are_passed_to_sdk(monkeypatch):
    settings = Mock(return_value=SimpleNamespace())
    client = Mock()
    monkeypatch.setattr(connection, "ProfileConfig", settings)
    monkeypatch.setattr(connection, "WorkspaceClient", client)
    connection.create_workspace_client("selected", "https://override.example.com", "flag-warehouse")
    assert settings.call_args.kwargs["profile"] == "selected"
    assert settings.call_args.kwargs["host"] == "https://override.example.com"
    assert settings.call_args.kwargs["warehouse_id"] == "flag-warehouse"
    client.assert_called_once_with(config=settings.return_value)


def test_concurrent_sql_reads_use_injected_connections_and_auth(monkeypatch):
    from server import sql_client
    execute = AsyncMock(return_value=[{"value": 1}])
    monkeypatch.setattr(sql_client, "_execute_statement", execute)
    first = StatementExecutionClient("https://first.example.com", "first-warehouse", lambda: {"Authorization": "first"})
    second = StatementExecutionClient("https://second.example.com", "second-warehouse", lambda: {"Authorization": "second"})

    async def run():
        return await asyncio.gather(first.execute("SELECT 1"), second.execute("SELECT 2"))

    assert asyncio.run(run()) == [[{"value": 1}], [{"value": 1}]]
    assert execute.await_args_list[0].args[2:5] == (
        "https://first.example.com", "first-warehouse", {"Authorization": "first"},
    )
    assert execute.await_args_list[1].args[2:5] == (
        "https://second.example.com", "second-warehouse", {"Authorization": "second"},
    )


def test_profile_is_loaded_by_real_sdk_config_without_ambient_auth(tmp_path, monkeypatch):
    config_file = tmp_path / "databrickscfg"
    config_file.write_text("[selected]\nhost = https://selected.example.com\ntoken = profile-token\n")
    monkeypatch.setattr(connection.Config, "_resolve_host_metadata", lambda self: None)
    settings = connection.ProfileConfig(
        environment={"DATABRICKS_TOKEN": "ambient", "DATABRICKS_HOST": "https://ambient.example.com"},
        profile="selected", config_file=str(config_file), warehouse_id="selected-warehouse",
    )
    assert settings.profile == "selected"
    assert settings.host == "https://selected.example.com"
    assert settings.authenticate() == {"Authorization": "Bearer profile-token"}
