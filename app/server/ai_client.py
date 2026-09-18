"""Model discovery and streaming with connection state owned by each client."""
import time
import json
import logging
from collections.abc import Callable
from typing import Optional
import aiohttp
from server.security import safe_error

logger = logging.getLogger(__name__)


CACHE_TTL = 600

DEFAULT_LLM_MODEL = "databricks-claude-sonnet-4-6"

AI_MODELS = {
    "databricks-claude-sonnet-5": {"label": "Claude Sonnet 5", "provider": "Anthropic"},
    "databricks-claude-opus-5": {"label": "Claude Opus 5", "provider": "Anthropic"},
    "databricks-claude-sonnet-4-6": {"label": "Claude Sonnet 4.6", "provider": "Anthropic"},
    "databricks-claude-opus-4-6": {"label": "Claude Opus 4.6", "provider": "Anthropic"},
    "databricks-gpt-5-6": {"label": "GPT-5.6", "provider": "OpenAI"},
    "databricks-gpt-5-4": {"label": "GPT-5.4", "provider": "OpenAI"},
    "databricks-gpt-5-4-mini": {"label": "GPT-5.4 Mini", "provider": "OpenAI"},
    "databricks-gemini-2-5-pro": {"label": "Gemini 2.5 Pro", "provider": "Google"},
    "databricks-gemini-2-5-flash": {"label": "Gemini 2.5 Flash", "provider": "Google"},
}

_NO_TEMPERATURE_TOKENS = ("opus-5", "opus-4-8", "opus-4-7", "sonnet-5", "fable-5")

def _normalize_model(model: str) -> str:
    return (model or "").lower().replace(".", "-")

_LLM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=120)

def _derive_label_and_provider(model_id: str) -> tuple[str, str]:
    """Derive a reasonable label and provider from a model endpoint name.

    If the model_id is in AI_MODELS, use the curated values.
    Otherwise, infer from the model name:
      - Label: strip leading 'databricks-', replace '-' with spaces, title-case
      - Provider: infer from model name (claude→Anthropic, gpt→OpenAI, etc.)
    """
    if model_id in AI_MODELS:
        info = AI_MODELS[model_id]
        return info["label"], info["provider"]

    # Infer provider from model name substring
    provider = "Databricks"
    if "claude" in model_id:
        provider = "Anthropic"
    elif "gpt" in model_id:
        provider = "OpenAI"
    elif "gemini" in model_id or "gemma" in model_id:
        provider = "Google"
    elif "llama" in model_id:
        provider = "Meta"
    elif "qwen" in model_id:
        provider = "Alibaba"

    # Derive label: strip leading 'databricks-', replace '-' with spaces, title-case
    label = model_id
    if label.startswith("databricks-"):
        label = label[11:]
    label = label.replace("-", " ").title()

    return label, provider

_FAMILY_RULES = (
    ("claude", ("Claude", False)),
    ("gpt-oss", ("GPT", True)),   # OpenAI open-weight — must precede the "gpt" rule
    ("gpt", ("GPT", False)),
    ("gemma", ("Gemma", True)),
    ("gemini", ("Gemini", False)),
    ("llama", ("Llama", True)),
    ("qwen", ("Qwen", True)),
    ("mixtral", ("Mistral", True)),
    ("mistral", ("Mistral", True)),
    ("deepseek", ("DeepSeek", True)),
    ("phi", ("Phi", True)),
    ("dbrx", ("DBRX", True)),
)

def _classify_family(model_id: str, provider: str) -> tuple[str, bool]:
    """Return (family, open_source) for a model endpoint id. Unknown families
    fall back to the provider name and are treated as proprietary (conservative)."""
    m = (model_id or "").lower()
    for token, (family, open_source) in _FAMILY_RULES:
        if token in m:
            return family, open_source
    return (provider or "Other"), False

def _model_record(model_id: str) -> dict:
    """One model's full record for the picker: {id, label, provider, family, open_source}."""
    label, provider = _derive_label_and_provider(model_id)
    family, open_source = _classify_family(model_id, provider)
    return {"id": model_id, "label": label, "provider": provider,
            "family": family, "open_source": open_source}

async def resolve_default_model(models: list[dict]) -> str:
    """Resolve the default model from the available list.

    Preference order:
      1. DEFAULT_LLM_MODEL if it's in the available list
      2. First available model whose id contains "claude-sonnet"
      3. First available model id
      4. DEFAULT_LLM_MODEL (ultimate fallback)
    """
    if not models:
        return DEFAULT_LLM_MODEL

    # Check if DEFAULT_LLM_MODEL is available
    available_ids = {m["id"] for m in models}
    if DEFAULT_LLM_MODEL in available_ids:
        return DEFAULT_LLM_MODEL

    # Try to find a claude-sonnet model
    for model in models:
        if "claude-sonnet" in model["id"]:
            return model["id"]

    # Fall back to first available model
    if models:
        return models[0]["id"]

    # Ultimate fallback
    return DEFAULT_LLM_MODEL

class ModelClient:
    """Owns its HTTP session, discovery cache, and endpoint compatibility state."""

    def __init__(self, host_provider: Callable[[], str], auth_provider: Callable[[], dict]):
        self.host_provider = host_provider
        self.auth_provider = auth_provider
        self._session = None
        self._temperature_unsupported = set()
        self._models_cache = None

    def _cache_get(self, key):
        entry = self._models_cache
        return entry[1] if entry and time.time() - entry[0] < CACHE_TTL else None

    def _cache_set(self, key, value):
        self._models_cache = (time.time(), value)


    def _supports_temperature(self, model: str) -> bool:
        """Whether the given serving-endpoint model accepts a `temperature` param.

        Consults the static version denylist AND the runtime-learned set, matching on
        the dot-normalized, lowercased name so both the endpoint name
        (``databricks-claude-opus-5``) and the underlying model id
        (``us.anthropic.claude-opus-5``) are covered.
        """
        m = _normalize_model(model)
        if m in self._temperature_unsupported:
            return False
        return not any(tok in m for tok in _NO_TEMPERATURE_TOKENS)


    async def _get_llm_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_LLM_TIMEOUT)
        return self._session


    async def _stream_from_fmapi(self,
        messages: list,
        model: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ):
        """Core SSE streaming generator for the Databricks Foundation Model API.

        Uses the default model if no model is explicitly passed.
        Emits Server-Sent Events: `data: {"content": "..."}` chunks then `data: [DONE]`.
        """

        if model is None:
            model = DEFAULT_LLM_MODEL

        host = self.host_provider()
        auth_headers = self.auth_provider()
        url = f"{host}/serving-endpoints/{model}/invocations"

        headers = {**auth_headers, "Content-Type": "application/json"}
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        # Only send temperature to models that accept it (newer Anthropic models 400 on it).
        if self._supports_temperature(model):
            payload["temperature"] = temperature

        normalized_model = _normalize_model(model)
        retried_without_temp = False

        try:
            session = await self._get_llm_session()
            # At most two attempts: if a model returns a 400 while temperature is set,
            # drop temperature and retry once. This self-heals for any model that
            # rejects the param but isn't in the static denylist (finding-driven:
            # rejections can be worded differently, so we don't gate on the error text).
            for attempt in range(2):
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        if attempt == 0 and response.status == 400 and "temperature" in payload:
                            # If the error explicitly blames temperature we're certain,
                            # so learn it now; otherwise learn only if the retry succeeds
                            # (below), to avoid mislabeling a model over an unrelated 400.
                            if "temperature" in error_text.lower():
                                self._temperature_unsupported.add(normalized_model)
                            logger.warning(
                                f"Model {model} returned 400 with temperature set; "
                                f"retrying without it"
                            )
                            payload.pop("temperature", None)
                            retried_without_temp = True
                            continue
                        logger.error(f"LLM API error ({response.status}): {error_text[:200]}")
                        yield f"data: {json.dumps({'error': f'LLM API error: {response.status}'})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    # Dropping temperature fixed a prior 400 → this model rejects it;
                    # remember so future calls skip temperature proactively.
                    if retried_without_temp:
                        self._temperature_unsupported.add(normalized_model)

                    async for line in response.content:
                        decoded = line.decode("utf-8").strip()
                        if not decoded or not decoded.startswith("data: "):
                            continue
                        data_str = decoded[6:]
                        if data_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            content = choices[0].get("delta", {}).get("content", "")
                            if content:
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue
                    yield "data: [DONE]\n\n"
                    return
        except Exception as e:
            # The exception text can carry the request payload or upstream response
            # body; send the client only a reference to the (redacted) server log.
            reference, message = safe_error(e, "LLM streaming", logger)
            yield f"data: {json.dumps({'error': message, 'reference': reference})}\n\n"
            yield "data: [DONE]\n\n"


    async def stream_llm_chat(self,
        messages: list,
        model: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ):
        """Stream a response from the FM API given a full message list (multi-turn)."""
        async for chunk in self._stream_from_fmapi(messages, model, max_tokens, temperature):
            yield chunk


    async def stream_llm_response(self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ):
        """Stream a response from the FM API given a system + single user prompt."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        async for chunk in self._stream_from_fmapi(messages, model, max_tokens, temperature):
            yield chunk


    async def list_available_models(self) -> list[dict]:
        """Fetch the list of available LLM chat models from the workspace serving endpoints.

        Queries the serving-endpoints API, filters to task == "llm/v1/chat", and
        returns a list of {id, label, provider, family, open_source} dicts (see
        _model_record) so the picker can group by licensing and family.

        Results are cached for CACHE_TTL (10 min). On ANY error, falls back to
        static AI_MODELS entries so the dropdown is never empty.
        """

        # Check cache first
        cached = self._cache_get("serving_models")
        if cached is not None:
            return cached

        try:
            host = self.host_provider()
            auth_headers = self.auth_provider()

            if not host or not auth_headers:
                logger.warning("Missing host or auth headers; falling back to static AI_MODELS")
                result = [_model_record(k) for k in AI_MODELS]
                self._cache_set("serving_models", result)
                return result

            url = f"{host}/api/2.0/serving-endpoints"

            session = await self._get_llm_session()
            async with session.get(url, headers=auth_headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.warning(f"serving-endpoints API error ({response.status}): {error_text[:200]}")
                    result = [_model_record(k) for k in AI_MODELS]
                    self._cache_set("serving_models", result)
                    return result

                data = await response.json()
                endpoints = data.get("endpoints", [])

                # Filter to chat endpoints, enrich with labels and providers
                models = []
                for ep in endpoints:
                    if ep.get("task") == "llm/v1/chat":
                        model_id = ep.get("name")
                        if model_id:
                            models.append(_model_record(model_id))

                if not models:
                    logger.warning("No chat endpoints found in serving-endpoints API")
                    result = [_model_record(k) for k in AI_MODELS]
                    self._cache_set("serving_models", result)
                    return result

                # Sort: known/curated families first (Anthropic, OpenAI, Google), then alpha
                provider_order = {"Anthropic": 0, "OpenAI": 1, "Google": 2}
                models.sort(key=lambda m: (provider_order.get(m["provider"], 999), m["id"]))

                self._cache_set("serving_models", models)
                return models

        except Exception as e:
            logger.error(f"Error fetching serving endpoints: {e}")
            result = [_model_record(k) for k in AI_MODELS]
            self._cache_set("serving_models", result)
            return result


    async def is_available_model(self, model_id: str) -> bool:
        """Whether a model id is one the workspace actually serves.

        Validates against the DYNAMIC serving-endpoints list (cached), NOT the
        static AI_MODELS label map — the picker lists every live chat endpoint, so
        the static map is not the source of truth for what's selectable. Degrades
        open: if the list can't be resolved, accept the id rather than silently
        forcing the default (the FM API call itself will surface a real error if
        the endpoint truly doesn't exist).
        """
        if not model_id:
            return False
        try:
            models = await self.list_available_models()
            ids = {m["id"] for m in models}
            return model_id in ids if ids else True
        except Exception:
            return True


    async def close_llm_session(self) -> None:
        """Close pooled HTTP connections at the end of an unattended run."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

