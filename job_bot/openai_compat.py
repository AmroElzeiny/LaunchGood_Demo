from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import random
import re
import time
from typing import Any, Callable


_UNSUPPORTED_PARAMS_BY_MODEL: dict[str, set[str]] = {}


def create_chat_completion_with_fallback(
    create_fn: Callable[..., Any],
    *,
    model: str,
    messages: list[dict[str, str]],
    logger: logging.Logger,
    log_label: str,
    temperature: float | None = None,
    response_format: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    retry_budget: int = 0,
    backoff_base_seconds: float = 1.5,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    normalized_model = model.strip().lower()
    unsupported_params = set(_UNSUPPORTED_PARAMS_BY_MODEL.get(normalized_model, set()))
    unsupported_params.update(_default_unsupported_params_for_model(normalized_model))

    params: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None and "temperature" not in unsupported_params:
        params["temperature"] = temperature
    if response_format is not None and "response_format" not in unsupported_params:
        params["response_format"] = response_format
    if timeout_seconds is not None and timeout_seconds > 0:
        params["timeout"] = float(timeout_seconds)

    unsupported_retry_budget = 3
    retry_attempt = 0
    while True:
        try:
            return _call_with_strict_timeout(
                create_fn,
                timeout_seconds=timeout_seconds,
                **params,
            )
        except Exception as exc:
            removed = _remove_unsupported_params(params, str(exc))
            if not removed:
                transient = _is_transient_error(exc)
                rate_limited = _is_rate_limit_error(exc)
                timed_out = _is_timeout_error(exc)
                _emit_event(
                    event_callback,
                    "call_failed",
                    {
                        "model": model,
                        "timed_out": timed_out,
                        "rate_limited": rate_limited,
                        "transient": transient,
                        "attempt": retry_attempt + 1,
                        "error": str(exc),
                    },
                )
                if not transient or retry_attempt >= max(0, int(retry_budget)):
                    raise
                retry_attempt += 1
                delay_seconds = _compute_backoff_delay(
                    retry_attempt=retry_attempt,
                    base_seconds=backoff_base_seconds,
                    error_text=str(exc),
                    rate_limited=rate_limited,
                )
                logger.warning(
                    "[%s] retrying OpenAI chat completion model=%s attempt=%s/%s delay=%.2fs rate_limited=%s timeout=%s error=%s",
                    log_label,
                    model,
                    retry_attempt,
                    max(0, int(retry_budget)),
                    delay_seconds,
                    rate_limited,
                    timed_out,
                    str(exc),
                )
                _emit_event(
                    event_callback,
                    "retry_scheduled",
                    {
                        "model": model,
                        "attempt": retry_attempt,
                        "delay_seconds": delay_seconds,
                        "rate_limited": rate_limited,
                        "timed_out": timed_out,
                        "error": str(exc),
                    },
                )
                time.sleep(delay_seconds)
                continue
            _remember_unsupported_params(normalized_model, removed)
            logger.warning(
                "[%s] retrying OpenAI chat completion without unsupported params: %s",
                log_label,
                ", ".join(removed),
            )
            unsupported_retry_budget -= 1
            if unsupported_retry_budget <= 0:
                raise RuntimeError("OpenAI chat completion unsupported-param retry loop exhausted")


def _default_unsupported_params_for_model(normalized_model: str) -> set[str]:
    unsupported: set[str] = set()
    if normalized_model.startswith(("gpt-5", "o1", "o3", "o4")):
        unsupported.add("temperature")
    return unsupported


def _remember_unsupported_params(normalized_model: str, removed: list[str]) -> None:
    if not removed:
        return
    known = _UNSUPPORTED_PARAMS_BY_MODEL.setdefault(normalized_model, set())
    known.update(removed)


def _remove_unsupported_params(params: dict[str, Any], error_text: str) -> list[str]:
    lowered = error_text.lower()
    removed: list[str] = []

    if (
        "temperature" in params
        and "temperature" in lowered
        and any(marker in lowered for marker in ("unsupported", "does not support", "only the default"))
    ):
        params.pop("temperature", None)
        removed.append("temperature")

    if (
        "response_format" in params
        and "response_format" in lowered
        and any(marker in lowered for marker in ("unsupported", "not supported", "invalid", "unknown"))
    ):
        params.pop("response_format", None)
        removed.append("response_format")

    return removed


def _is_timeout_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__}: {str(exc)}".lower()
    markers = (
        "timeout",
        "timed out",
        "deadline exceeded",
        "request timed out",
        "read timed out",
        "apitimeouterror",
    )
    return any(marker in text for marker in markers)


def _is_rate_limit_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__}: {str(exc)}".lower()
    markers = (
        "rate limit",
        "too many requests",
        "429",
        "quota",
        "tokens per min",
        "requests per min",
    )
    return any(marker in text for marker in markers)


def _is_transient_error(exc: Exception) -> bool:
    if _is_timeout_error(exc) or _is_rate_limit_error(exc):
        return True
    text = f"{exc.__class__.__name__}: {str(exc)}".lower()
    markers = (
        "temporar",
        "temporarily unavailable",
        "unavailable",
        "connection reset",
        "connection aborted",
        "connection closed",
        "connection error",
        "server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "internal server error",
        "overloaded",
        "try again",
    )
    return any(marker in text for marker in markers)


def _compute_backoff_delay(
    *,
    retry_attempt: int,
    base_seconds: float,
    error_text: str,
    rate_limited: bool,
) -> float:
    capped_attempt = max(1, int(retry_attempt))
    multiplier = 2 ** (capped_attempt - 1)
    if rate_limited:
        multiplier *= 2
    computed = max(0.1, float(base_seconds)) * multiplier
    retry_after_seconds = _parse_retry_after_seconds(error_text)
    if retry_after_seconds is not None:
        computed = max(computed, retry_after_seconds)
    jitter = random.uniform(0.0, max(0.2, float(base_seconds)))
    return min(computed + jitter, 60.0)


def _parse_retry_after_seconds(error_text: str) -> float | None:
    lowered = str(error_text or "").lower()
    patterns = (
        r"retry after[: ]+([0-9]+(?:\.[0-9]+)?)",
        r"try again in[: ]+([0-9]+(?:\.[0-9]+)?)s",
        r"wait[: ]+([0-9]+(?:\.[0-9]+)?)s",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


def _emit_event(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    callback(event_type, payload)


def _call_with_strict_timeout(
    create_fn: Callable[..., Any],
    *,
    timeout_seconds: float | None,
    **params: Any,
) -> Any:
    if timeout_seconds is None or timeout_seconds <= 0:
        return create_fn(**params)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openai-budget")
    future = executor.submit(create_fn, **params)
    try:
        return future.result(timeout=float(timeout_seconds))
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"OpenAI call exceeded {timeout_seconds:.1f}s budget") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
