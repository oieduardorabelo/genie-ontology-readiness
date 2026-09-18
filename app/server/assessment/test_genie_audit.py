import unittest
from unittest.mock import AsyncMock

from server.assessment.probes import AssessmentProbes, ProbeDependencies
from server.workspace_filter import get_workspace_filter, get_catalog_scope


def suite(execute=None, accessible=None, defaults=()):
    return AssessmentProbes(ProbeDependencies(
        execute_sql=execute or AsyncMock(return_value=[]),
        get_workspace_host=lambda: "", get_auth_headers=lambda **kw: {},
        get_user_token=lambda: None, accessible_catalogs=accessible or AsyncMock(return_value=None),
        record_rest_identity=lambda: None, workspace_filter=get_workspace_filter(),
        catalog_scope=tuple(get_catalog_scope() or ()) or None, default_catalogs=defaults,
    ))

from server.workspace_filter import set_workspace_filter


class GenieAuditCountsTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        set_workspace_filter(None)  # don't leak filter state across tests

    async def test_scopes_audit_scan_to_selected_workspaces_and_lookback(self):
        execute = AsyncMock(return_value=[{"total": "3", "active_30d": "2"}])
        set_workspace_filter({"mode": "include", "workspace_ids": ["1444828305810485"]})

        result = await suite(execute)._genie_audit_counts()

        self.assertEqual(result, {"total": 3, "active_30d": 2})
        query = execute.await_args.args[0]
        self.assertIn("CAST(workspace_id AS STRING) IN (:wsf_0)", query)
        self.assertIn("event_date >= current_date() - INTERVAL 30 DAYS", query)
        self.assertEqual(execute.await_args.kwargs["parameters"], {"wsf_0": "1444828305810485"})

    async def test_exclude_mode_uses_not_in(self):
        execute = AsyncMock(return_value=[{"total": "1", "active_30d": "0"}])
        set_workspace_filter({"mode": "exclude", "workspace_ids": ["111", "222"]})

        await suite(execute)._genie_audit_counts()

        query = execute.await_args.args[0]
        self.assertIn("CAST(workspace_id AS STRING) NOT IN (:wsf_0, :wsf_1)", query)
        self.assertEqual(execute.await_args.kwargs["parameters"], {"wsf_0": "111", "wsf_1": "222"})

    async def test_omits_workspace_predicate_when_no_filter(self):
        execute = AsyncMock(return_value=[{"total": 0, "active_30d": 0}])
        set_workspace_filter(None)

        await suite(execute)._genie_audit_counts()

        self.assertNotIn("wsf_", execute.await_args.args[0])
        self.assertIsNone(execute.await_args.kwargs["parameters"])

    async def test_degrades_gracefully_when_audit_query_fails(self):
        execute = AsyncMock(side_effect=TimeoutError("audit query timed out"))

        result = await suite(execute)._genie_audit_counts()

        self.assertEqual(result, {"total": None, "active_30d": None})


class GenieAuditRowsTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        set_workspace_filter(None)

    async def test_resolves_display_name_and_keeps_space_id(self):
        # The query COALESCEs the resolved name over the id; a space with no name in
        # the window falls back to its id (the row's `agent` == `space_id`).
        set_workspace_filter({"mode": "include", "workspace_ids": ["7474644235756678"]})  # single ws
        execute = AsyncMock(return_value=[
            {"agent": "TD US Portfolio Assistant", "space_id": "01f1a", "events": "1158", "active_30d": 1},
            {"agent": "01f1b", "space_id": "01f1b", "events": "5", "active_30d": 0},
        ])
        rows = await suite(execute)._genie_audit_rows()

        query = execute.await_args.args[0]
        self.assertIn("max_by(request_params.display_name, event_time)", query)
        self.assertIn("COALESCE(nm.space_name, a.space_id) AS agent", query)
        self.assertIn("request_params.space_id <> 'new'", query)  # drop the placeholder
        self.assertEqual(rows[0], {"agent": "TD US Portfolio Assistant", "space_id": "01f1a",
                                   "events": 1158, "active_30d": "Yes"})
        self.assertEqual(rows[1]["agent"], rows[1]["space_id"])  # fallback to id

    async def test_scopes_name_window_to_selected_workspaces(self):
        set_workspace_filter({"mode": "include", "workspace_ids": ["7474644235756678"]})
        execute = AsyncMock(return_value=[])
        await suite(execute)._genie_audit_rows()

        query = execute.await_args.args[0]
        # The workspace predicate is applied to BOTH the names CTE and the activity scan.
        self.assertEqual(query.count("CAST(workspace_id AS STRING) IN (:wsf_0)"), 2)
        self.assertEqual(execute.await_args.kwargs["parameters"], {"wsf_0": "7474644235756678"})


if __name__ == "__main__":
    unittest.main()
