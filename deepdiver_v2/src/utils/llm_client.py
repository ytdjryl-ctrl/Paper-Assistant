import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)


_SESSION: Optional[requests.Session] = None


class LLMResponse:
    """Small response adapter matching the parts of requests.Response used by agents."""

    def __init__(self, status_code: int, data: Dict[str, Any], text: str = ""):
        self.status_code = status_code
        self._data = data
        self.text = text or json.dumps(data, ensure_ascii=False)

    def json(self) -> Dict[str, Any]:
        return self._data


def _provider_from_config(model_config: Dict[str, Any]) -> str:
    provider = (
        model_config.get("provider")
        or os.getenv("MODEL_PROVIDER")
        or ""
    ).strip().lower()
    if provider:
        return provider

    url = str(model_config.get("url") or os.getenv("MODEL_REQUEST_URL", "")).lower()
    if "dashscope.aliyuncs.com" in url:
        return "openai_compatible"
    if "127.0.0.1" in url or "localhost" in url:
        return "litellm"
    return "csb"


def _build_headers(token: str, provider: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not token:
        return headers

    if provider in {"csb", "pangu", "custom_csb"}:
        headers["csb-token"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _sanitize_payload(payload: Dict[str, Any], provider: str) -> Dict[str, Any]:
    clean_payload = dict(payload)

    # OpenAI-compatible providers reject local template-only fields.
    if provider in {"openai", "openai_compatible", "dashscope", "litellm", "deepseek", "siliconflow"}:
        clean_payload.pop("chat_template", None)
        clean_payload.pop("spaces_between_special_tokens", None)

    return clean_payload


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        retries = Retry(
            total=0,
            connect=0,
            read=0,
            status=0,
            allowed_methods=False,
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _SESSION = session
    return _SESSION


def _should_bypass_proxy(url: str, provider: str) -> bool:
    setting = os.getenv("MODEL_BYPASS_PROXY", "auto").strip().lower()
    if setting in {"1", "true", "yes", "on"}:
        return True
    if setting in {"0", "false", "no", "off"}:
        return False
    return provider in {"openai_compatible", "dashscope"} and "dashscope.aliyuncs.com" in url.lower()


def _request_proxies(url: str, provider: str) -> Optional[Dict[str, str]]:
    if _should_bypass_proxy(url, provider):
        return {"http": "", "https": ""}
    return None


def _retry_count() -> int:
    return max(1, int(os.getenv("MODEL_REQUEST_RETRIES", "4")))


def _retry_sleep_seconds(attempt: int) -> float:
    base = float(os.getenv("MODEL_RETRY_BACKOFF_SECONDS", "2"))
    return min(base * (2 ** max(0, attempt - 1)), 15.0)


def _usage_log_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "logs" / "llm_usage.jsonl"


def _log_usage(
    *,
    provider: str,
    url: str,
    agent_name: Optional[str],
    payload: Dict[str, Any],
    response_json: Dict[str, Any],
    status_code: int,
    elapsed_ms: int,
    error: Optional[str] = None,
) -> None:
    try:
        usage = response_json.get("usage") if isinstance(response_json, dict) else None
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "provider": provider,
            "url": url,
            "agent": agent_name,
            "model": payload.get("model"),
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
            "error": error,
        }
        path = _usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("Failed to write LLM usage log: %s", exc)


def chat_completion_response(
    payload: Dict[str, Any],
    *,
    model_config: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
    request_logger: Optional[logging.Logger] = None,
) -> LLMResponse:
    """Send a chat completion request through the configured provider.

    This is the single request path for Agent calls. It supports direct
    OpenAI-compatible APIs, LiteLLM, and legacy CSB-style services.
    """
    from config.config import get_config

    log = request_logger or logger
    if model_config is None:
        model_config = get_config().get_custom_llm_config()

    url = model_config.get("url") or os.getenv("MODEL_REQUEST_URL", "")
    token = model_config.get("token") or os.getenv("MODEL_REQUEST_TOKEN", "")
    provider = _provider_from_config(model_config)
    timeout = model_config.get("timeout", 180)

    request_payload = _sanitize_payload(payload, provider)
    headers = _build_headers(token, provider)

    session = _get_session()
    proxies = _request_proxies(url, provider)
    max_attempts = _retry_count()

    last_exc: Optional[Exception] = None
    response = None
    final_elapsed_ms = 0
    for attempt in range(1, max_attempts + 1):
        started = time.time()
        try:
            response = session.post(
                url=url,
                headers=headers,
                json=request_payload,
                timeout=timeout,
                proxies=proxies,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                elapsed_ms = int((time.time() - started) * 1000)
                try:
                    response_json = response.json()
                except Exception:
                    response_json = {"error": {"message": response.text, "code": response.status_code}}
                _log_usage(
                    provider=provider,
                    url=url,
                    agent_name=agent_name,
                    payload=request_payload,
                    response_json=response_json,
                    status_code=response.status_code,
                    elapsed_ms=elapsed_ms,
                    error=f"retryable_status_attempt_{attempt}",
                )
                wait_s = _retry_sleep_seconds(attempt)
                log.warning(
                    "LLM request retryable status via provider=%s status=%s attempt=%s/%s; sleep %.1fs",
                    provider,
                    response.status_code,
                    attempt,
                    max_attempts,
                    wait_s,
                )
                time.sleep(wait_s)
                continue
            final_elapsed_ms = int((time.time() - started) * 1000)
            break
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ProxyError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            last_exc = exc
            elapsed_ms = int((time.time() - started) * 1000)
            error_json = {"error": {"message": str(exc), "code": "connection_error"}}
            _log_usage(
                provider=provider,
                url=url,
                agent_name=agent_name,
                payload=request_payload,
                response_json=error_json,
                status_code=0,
                elapsed_ms=elapsed_ms,
                error=f"attempt_{attempt}: {exc}",
            )
            if attempt >= max_attempts:
                log.warning("LLM request failed via provider=%s url=%s: %s", provider, url, exc)
                raise
            wait_s = _retry_sleep_seconds(attempt)
            log.warning(
                "LLM request network failure via provider=%s attempt=%s/%s; sleep %.1fs: %s",
                provider,
                attempt,
                max_attempts,
                wait_s,
                exc,
            )
            time.sleep(wait_s)
            continue
        except Exception as exc:
            last_exc = exc
            break

    if response is None:
        if last_exc is not None:
            log.warning("LLM request failed via provider=%s url=%s: %s", provider, url, last_exc)
            raise last_exc
        raise RuntimeError("LLM request failed without response")

    if last_exc is not None and response is None:
        raise last_exc

    try:
        try:
            response_json = response.json()
        except Exception:
            response_json = {
                "error": {
                    "message": response.text,
                    "code": response.status_code,
                }
            }

        _log_usage(
            provider=provider,
            url=url,
            agent_name=agent_name,
            payload=request_payload,
            response_json=response_json,
            status_code=response.status_code,
            elapsed_ms=final_elapsed_ms,
        )
        return LLMResponse(response.status_code, response_json, response.text)
    except Exception as exc:
        error_json = {"error": {"message": str(exc), "code": "connection_error"}}
        _log_usage(
            provider=provider,
            url=url,
            agent_name=agent_name,
            payload=request_payload,
            response_json=error_json,
            status_code=0,
            elapsed_ms=final_elapsed_ms,
            error=str(exc),
        )
        log.warning("LLM request failed via provider=%s url=%s: %s", provider, url, exc)
        raise


def stream_chat_completion_response(
    payload: Dict[str, Any],
    *,
    model_config: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
    request_logger: Optional[logging.Logger] = None,
) -> LLMResponse:
    """Collect an OpenAI-compatible SSE stream into one response object.

    Thinking models can emit many reasoning tokens before final content. A
    streamed transport prevents the HTTP read from appearing idle while still
    exposing only the final content to downstream parsers.
    """
    from config.config import get_config

    log = request_logger or logger
    if model_config is None:
        model_config = get_config().get_custom_llm_config()
    url = model_config.get("url") or os.getenv("MODEL_REQUEST_URL", "")
    token = model_config.get("token") or os.getenv("MODEL_REQUEST_TOKEN", "")
    provider = _provider_from_config(model_config)
    timeout = float(model_config.get("timeout", 600))
    request_payload = _sanitize_payload(payload, provider)
    request_payload["stream"] = True
    headers = _build_headers(token, provider)
    started = time.time()
    response = _get_session().post(
        url=url,
        headers=headers,
        json=request_payload,
        timeout=(30, timeout),
        proxies=_request_proxies(url, provider),
        stream=True,
    )
    if response.status_code >= 400:
        try:
            data = response.json()
        except Exception:
            data = {"error": {"message": response.text, "code": response.status_code}}
        _log_usage(
            provider=provider, url=url, agent_name=agent_name, payload=request_payload,
            response_json=data, status_code=response.status_code,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return LLMResponse(response.status_code, data, response.text)

    content_parts = []
    reasoning_parts = []
    finish_reason = None
    usage: Dict[str, Any] = {}
    # Some SSE endpoints omit a charset, causing requests to default to
    # ISO-8859-1. Always decode the wire bytes as UTF-8 so Chinese content is
    # not permanently stored as mojibake (for example "è..." sequences).
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.startswith("data:"):
            continue
        data_text = line[5:].strip()
        if data_text == "[DONE]":
            break
        try:
            chunk = json.loads(data_text)
        except json.JSONDecodeError:
            log.debug("Ignored malformed SSE line from model %s", request_payload.get("model"))
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta") or {}
        if delta.get("reasoning_content"):
            reasoning_parts.append(str(delta["reasoning_content"]))
        if delta.get("content"):
            content_parts.append(str(delta["content"]))

    data = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "reasoning_content": "".join(reasoning_parts),
            },
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }
    _log_usage(
        provider=provider, url=url, agent_name=agent_name, payload=request_payload,
        response_json=data, status_code=response.status_code,
        elapsed_ms=int((time.time() - started) * 1000),
    )
    return LLMResponse(response.status_code, data)
