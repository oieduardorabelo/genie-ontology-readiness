"""Unattended plan generation shares App prompts and rejects unsuccessful streams."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from server import action_plan


@pytest.mark.parametrize("events", [[{"error": "Model denied"}], [], [{"content": ""}]])
def test_failed_or_empty_plan_stream_does_not_produce_a_plan(monkeypatch, events):
    async def stream(*args, **kwargs):
        for event in events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    with pytest.raises(RuntimeError):
        asyncio.run(action_plan.generate_action_plan({"overall": {}, "pillars": []}, "test-model", SimpleNamespace(stream_llm_chat=stream)))


def test_plan_has_grounded_summary_and_uses_app_prompts(monkeypatch):
    scorecard = {"overall": {"score": 42, "level": 2, "level_label": "Developing", "readiness_stage": "Forming"}, "pillars": []}

    async def stream(messages, **kwargs):
        assert messages[0]["content"] == action_plan._generate_system(scorecard)
        assert kwargs == {"model": "test-model", "max_tokens": 2400, "temperature": 0.4}
        yield 'data: {"content": "## Where you are\\n\\n"}\n\n'
        yield 'data: {"content": "Improve metadata."}\n\n'
        yield "data: [DONE]\n\n"

    plan = asyncio.run(action_plan.generate_action_plan(scorecard, "test-model", SimpleNamespace(stream_llm_chat=stream)))
    assert plan.startswith(action_plan._scorecard_markdown(scorecard))
    assert "42/100" in plan
    assert "## Where you are\n\nImprove metadata." in plan
