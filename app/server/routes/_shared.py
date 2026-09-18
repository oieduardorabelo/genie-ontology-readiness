"""Shared infrastructure for all route modules: App response cache and request identity."""

import time
import logging
from contextvars import ContextVar
from typing import Optional

from fastapi import Header

from server.security import resolve_principal
from server.ai_client import (
    DEFAULT_LLM_MODEL as DEFAULT_LLM_MODEL,
    _model_record as _model_record,
    _classify_family as _classify_family,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authenticated principal
# ---------------------------------------------------------------------------
async def current_principal(
    x_forwarded_email: Optional[str] = Header(default=None),
    x_forwarded_access_token: Optional[str] = Header(default=None),
) -> str:
    """The ownership key for this request's assessment history and saved plans.

    Every per-user route depends on this instead of reading the forwarded email
    header directly, so ownership is bound to an identity the app established —
    from the forwarded token where one exists — rather than to a header value a
    caller could set (CWE-290). This always resolves to a usable key, so no
    request is ever refused for lack of an identity.
    """
    return await resolve_principal(forwarded_email=x_forwarded_email,
                                   forwarded_token=x_forwarded_access_token)

# ---------------------------------------------------------------------------
# Lightweight server-side response cache. The assessment is relatively
# expensive (many system-table queries), so cache results briefly.
# ---------------------------------------------------------------------------
_response_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 600  # seconds (10 min — environment metadata changes slowly)
MAX_CACHE_SIZE = 100


def _cache_get(key: str):
    entry = _response_cache.get(key)
    if entry is not None and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: object):
    if len(_response_cache) >= MAX_CACHE_SIZE:
        oldest_key = min(_response_cache, key=lambda k: _response_cache[k][0])
        del _response_cache[oldest_key]
    _response_cache[key] = (time.time(), value)


def _cache_clear():
    _response_cache.clear()




_ai_model: ContextVar[str] = ContextVar("ai_model", default=DEFAULT_LLM_MODEL)
