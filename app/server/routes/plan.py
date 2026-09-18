"""Plan generation — one AI call that turns the workspace assessment into a
concise, tactical action plan, plus PDF export of the generated plan.

The Plan tab is a single button: it grounds in the workspace scorecard (overall
readiness, per-pillar scores, and top gaps), then generates a prioritized,
tactical plan to prepare for Genie Ontology, naming the public Databricks
accelerators that help close each gap. The plan can be exported to a branded PDF.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from server.action_plan import (
    _generate_system,
    _scorecard_markdown,
    _strip_document_h1 as _strip_document_h1,
    _build_plan_pdf_html,
)
from server.routes._shared import _ai_model, current_principal
from server.ai_client import ModelClient
from server.routes.dependencies import get_model_client
from server.pdf import (
    ExternalResourceBlocked,  # noqa: F401 - re-exported for tests / callers
    block_external_resources as _block_external_resources,  # noqa: F401
    pdf_export_unavailable_response,
    render_pdf_response,
)
from server import snapshots, plans

logger = logging.getLogger(__name__)
router = APIRouter()


class PlanGenerateRequest(BaseModel):
    # A plan is generated against EITHER a saved assessment (snapshot_id) or an
    # in-session assessment passed inline (scorecard). The inline path lets Plan
    # work when history isn't persisted (no Lakebase attached).
    snapshot_id: Optional[int] = None
    scorecard: Optional[dict] = None


# Field limits are the second half of the body-size cap in app.py: that one bounds
# the whole request, these bound what actually reaches the PDF renderer and the
# history table. A generated plan is capped at 2000 output tokens (~10 KB).
_MAX_TITLE = 200
_MAX_MARKDOWN = 256_000


class PlanSaveRequest(BaseModel):
    snapshot_id: Optional[int] = None
    title: str = Field(default="Genie Ontology Readiness — Action Plan", max_length=_MAX_TITLE)
    markdown: str = Field(max_length=_MAX_MARKDOWN)


class PlanPdfRequest(BaseModel):
    title: str = Field(default="Genie Ontology Readiness — Action Plan", max_length=_MAX_TITLE)
    markdown: str = Field(max_length=_MAX_MARKDOWN)


@router.post("/plan/generate")
async def plan_generate(req: PlanGenerateRequest, principal: str = Depends(current_principal), client: ModelClient = Depends(get_model_client)):
    """Generate the action plan against an assessment (no conversation).

    The assessment comes from EITHER a saved snapshot (``snapshot_id``, loaded
    server-side from the user's own history) or an inline ``scorecard`` sent by the
    client — the latter lets the Plan tab work from the in-session assessment even
    when history isn't persisted (no Lakebase attached).
    """
    if req.snapshot_id is not None:
        snap = await snapshots.get_snapshot(req.snapshot_id, created_by=principal)
        if snap is None:
            return JSONResponse(status_code=404, content={"error": "Assessment not found."})
        scorecard = snap.get("scorecard") or {}
    elif req.scorecard is not None:
        # Client-supplied (in-session assessment, when history isn't persisted).
        scorecard = req.scorecard
    else:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide a snapshot_id or an assessment scorecard."},
        )
    # Validate the resolved scorecard from EITHER source has real content — an empty
    # or malformed one (client dict, or a degraded/legacy snapshot) would otherwise
    # stream a generic "no assessment available" plan instead of a clear error.
    if not isinstance(scorecard, dict) or not scorecard.get("pillars"):
        return JSONResponse(
            status_code=400,
            content={"error": "The assessment is empty — run an assessment first."},
        )
    system = _generate_system(scorecard)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "Generate the action plan now."},
    ]
    logger.info("Plan generate for %s",
                f"snapshot {req.snapshot_id}" if req.snapshot_id is not None else "in-session assessment")

    async def _gen():
        # Emit the deterministic score summary first (as one SSE content frame) so
        # the top of the plan is always the real scores; then stream the LLM plan.
        header = _scorecard_markdown(scorecard)
        if header:
            yield f"data: {json.dumps({'content': header})}\n\n"
        async for chunk in client.stream_llm_chat(messages, model=_ai_model.get(), max_tokens=2400, temperature=0.4):
            yield chunk

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/plan/save")
async def plan_save(req: PlanSaveRequest, principal: str = Depends(current_principal)):
    """Persist a generated plan for the current user, linked to its assessment."""
    plan_id = await plans.save_plan(
        created_by=principal,
        snapshot_id=req.snapshot_id,
        title=req.title,
        model=_ai_model.get(),
        plan_markdown=req.markdown,
    )
    return {"id": plan_id, "saved": plan_id is not None}


@router.get("/plan/list")
async def plan_list(principal: str = Depends(current_principal)):
    """The current user's saved plans (metadata only)."""
    return {"plans": await plans.list_plans(created_by=principal)}


@router.get("/plan/{plan_id}")
async def plan_get(plan_id: int, principal: str = Depends(current_principal)):
    """Load one saved plan (including markdown), scoped to the current user."""
    plan = await plans.get_plan(plan_id, created_by=principal)
    if plan is None:
        return JSONResponse(status_code=404, content={"error": "Plan not found."})
    return plan


@router.post("/plan/pdf")
async def plan_pdf(req: PlanPdfRequest):
    """Render the plan Markdown to a branded PDF, returned inline for a new-tab viewer.

    The render engine, external-resource backstop, and Response mapping (503/400/500)
    all live in ``server.pdf`` and are shared with the assessment export. The optional
    PDF stack is gated up front so a missing dep degrades this endpoint (503) rather
    than raising mid-build.
    """
    unavailable = pdf_export_unavailable_response()
    if unavailable is not None:
        return unavailable
    html_doc = _build_plan_pdf_html(req.markdown or "", req.title)
    return render_pdf_response(html_doc, req.title or "action-plan")
