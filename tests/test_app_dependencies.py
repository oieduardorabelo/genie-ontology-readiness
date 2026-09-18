"""App requests and shutdown use their injected model client."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from server.routes.dependencies import get_model_client
from server.routes.plan import PlanGenerateRequest, plan_generate


def test_app_config_plan_and_shutdown_use_app_owned_client(monkeypatch):
    import app as entrypoint
    from server.routes import config as config_route
    from server import sql_client

    calls = []

    async def stream(messages, **kwargs):
        calls.append((messages, kwargs))
        yield 'data: {"content": "Improve metadata."}\n\n'
        yield 'data: [DONE]\n\n'

    models = SimpleNamespace(
        list_available_models=AsyncMock(return_value=[{"id": "app-chat-model"}]),
        is_available_model=AsyncMock(return_value=True),
        close_llm_session=AsyncMock(), stream_llm_chat=stream,
    )
    factory = Mock(return_value=models)
    monkeypatch.setattr(entrypoint, "ModelClient", factory)
    monkeypatch.setattr(entrypoint, "USE_LAKEBASE", False)
    warmup = AsyncMock()
    monkeypatch.setattr(sql_client, "execute_sql", warmup)
    monkeypatch.setattr(config_route, "get_cloud_provider", lambda: "aws")

    async def run():
        async with entrypoint.lifespan(entrypoint.app):
            request = SimpleNamespace(app=entrypoint.app, headers={"X-AI-Model": "app-chat-model"},
                                      url=SimpleNamespace(path="/api/plan/generate"))
            assert get_model_client(request) is models
            config = await config_route.get_config(client=models)
            assert config["default_model"] == "app-chat-model"

            async def next_request(req):
                response = await plan_generate(PlanGenerateRequest(scorecard={
                    "overall": {"score": 42}, "pillars": [{"name": "Metadata", "score": 42}],
                }), principal="viewer", client=get_model_client(req))
                body = "".join([frame async for frame in response.body_iterator])
                assert "Improve metadata." in body
                assert calls[0][1]["model"] == "app-chat-model"
                return response

            middleware = entrypoint.RequestContextMiddleware(entrypoint.app)
            response = await middleware.dispatch(request, next_request)
            assert response.headers["Cache-Control"] == "no-store"

    asyncio.run(run())
    warmup.assert_awaited_once_with("SELECT 1 AS warmup")
    models.close_llm_session.assert_awaited_once()
    factory.assert_called_once()
