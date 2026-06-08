from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import random
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from scrapling import DynamicFetcher, Fetcher, StealthyFetcher

from job_bot.config import Settings
from job_bot.site_profiles import get_site_profile

if TYPE_CHECKING:
    from job_bot.storage import StateStore


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int
    html: str
    page: Any
    strategy: str


class _TextSelection:
    def __init__(self, values: list[str]) -> None:
        self._values = [value for value in values if value is not None]

    def get(self) -> str | None:
        return self._values[0] if self._values else None

    def getall(self) -> list[str]:
        return list(self._values)


@dataclass(slots=True)
class _AnchorSelection:
    attrib: dict[str, str]
    text: str

    def css(self, selector: str) -> _TextSelection:
        if selector == "::text":
            return _TextSelection([self.text] if self.text else [])
        return _TextSelection([])


class _FallbackSelectorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_AnchorSelection] = []
        self.body_texts: list[str] = []
        self.ld_json_blocks: list[str] = []
        self._inside_body = False
        self._anchor_stack: list[dict[str, Any]] = []
        self._ld_json_stack: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "body":
            self._inside_body = True
        if tag == "a":
            self._anchor_stack.append({"attrib": attr_map, "texts": []})
        if tag == "script" and attr_map.get("type", "").lower() == "application/ld+json":
            self._ld_json_stack.append([])

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self._inside_body = False
        if tag == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            text = " ".join(
                " ".join(str(part).split())
                for part in anchor["texts"]
                if str(part).strip()
            )
            self.anchors.append(_AnchorSelection(attrib=dict(anchor["attrib"]), text=" ".join(text.split())))
        if tag == "script" and self._ld_json_stack:
            block = "".join(self._ld_json_stack.pop()).strip()
            if block:
                self.ld_json_blocks.append(block)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._inside_body:
            self.body_texts.append(data)
        if self._anchor_stack:
            self._anchor_stack[-1]["texts"].append(data)
        if self._ld_json_stack:
            self._ld_json_stack[-1].append(data)


class _FallbackSelectorPage:
    def __init__(self, *, content: str, url: str) -> None:
        del url
        parser = _FallbackSelectorParser()
        parser.feed(content)
        parser.close()
        self._anchors = parser.anchors
        self._body_texts = parser.body_texts
        self._ld_json_blocks = parser.ld_json_blocks

    def css(self, selector: str) -> list[_AnchorSelection] | _TextSelection:
        if selector == "a":
            return list(self._anchors)
        if selector == "body ::text":
            return _TextSelection(self._body_texts)
        if selector == 'script[type="application/ld+json"]::text':
            return _TextSelection(self._ld_json_blocks)
        return _TextSelection([])


class ResilientFetcher:
    def __init__(self, settings: Settings, logger: logging.Logger, state_store: "StateStore | None" = None) -> None:
        self.settings = settings
        self.logger = logger
        self.state_store = state_store
        self._browser_fetch_disabled_reason: str | None = None
        self._browser_fetch_disabled_until = 0.0
        self._browser_fetch_failure_count = 0
        self._last_failure_reason_by_domain: dict[str, str] = {}

    @staticmethod
    def _looks_like_cloudflare_challenge(response_url: str, html: str) -> bool:
        lower_url = response_url.lower()
        lower_html = html.lower()
        if "/cdn-cgi/challenge-platform/" in lower_url:
            return True
        if "<title>just a moment...</title>" in lower_html:
            return True
        if "<title>challenge -" in lower_html and "enable javascript and cookies to continue" in lower_html:
            return True
        if "challenges.cloudflare.com" in lower_html or "cf-turnstile" in lower_html:
            return True
        if "__cf_chl_rt_tk=" in lower_url:
            return True
        if "window._cf_chl_opt" in lower_html or "cf_chl_opt" in lower_html:
            return True
        if "enable javascript and cookies to continue" in lower_html:
            return True
        if "verify you are human" in lower_html and "cloudflare" in lower_html:
            return True
        if "id=\"sec-if-cpt-container\"" in lower_html or "id='sec-if-cpt-container'" in lower_html:
            return True
        if "behavioral-content" in lower_html and "location.reload(true)" in lower_html:
            return True
        if "powered and protected by" in lower_html and "window.xmlhttprequest.prototype.send" in lower_html:
            return True
        return False

    def _fetch_with_curl_cffi(self, url: str, timeout_ms: int) -> FetchResult | None:
        try:
            from curl_cffi import requests as curl_requests
        except Exception:  # noqa: BLE001
            return None

        response = curl_requests.get(
            url,
            impersonate="chrome124",
            timeout=max(1.0, float(timeout_ms) / 1000.0),
            allow_redirects=True,
            verify=self.settings.verify_ssl,
        )
        html = str(getattr(response, "text", ""))
        response_url = str(getattr(response, "url", url))
        return FetchResult(
            url=response_url,
            status=int(getattr(response, "status_code", 0)),
            html=html,
            page=self._build_selector_page(html=html, url=response_url),
            strategy="curl_cffi",
        )

    def _build_selector_page(self, *, html: str, url: str) -> Any:
        try:
            from scrapling import Selector

            return Selector(content=html, url=url)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[fetch] using lightweight selector fallback for %s because Scrapling Selector was unavailable: %s",
                url,
                str(exc),
            )
            return _FallbackSelectorPage(content=html, url=url)

    async def fetch(self, url: str) -> FetchResult | None:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> FetchResult | None:
        self._restore_browser_fetch_after_cooldown()
        timeout = self._strategy_timeout_ms()
        domain = self._domain_for_url(url)
        self._last_failure_reason_by_domain.pop(domain, None)
        tries = self._strategy_plan(url, timeout)
        last_error = None
        for strategy_name, strategy_call in tries:
            if strategy_name in {"stealthy", "dynamic"} and self._browser_fetch_disabled_reason is not None:
                self._record_fetch_strategy_result(
                    domain,
                    strategy_name,
                    success=False,
                    latency_ms=0.0,
                    skipped=True,
                    error=self._browser_fetch_disabled_reason or "browser_disabled",
                )
                continue
            strategy_result = self._run_strategy_with_budget(
                url=url,
                domain=domain,
                strategy_name=strategy_name,
                strategy_call=strategy_call,
            )
            if strategy_result is None:
                continue
            if isinstance(strategy_result, Exception):
                last_error = strategy_result
                continue
            self._last_failure_reason_by_domain.pop(domain, None)
            return strategy_result

        if last_error is not None and domain not in self._last_failure_reason_by_domain:
            if self._looks_like_timeout_error(last_error):
                self._remember_failure_reason(domain, "timeout")
            elif self._looks_like_rate_limit_error(last_error):
                self._remember_failure_reason(domain, "rate_limited")
            elif self._looks_like_transient_fetch_error(last_error):
                self._remember_failure_reason(domain, "transient")
            else:
                self._remember_failure_reason(domain, "error")
        self.logger.error("[fetch] failed for %s after all strategies: %s", url, str(last_error))
        return None

    def _strategy_timeout_ms(self) -> int:
        configured = int(getattr(self.settings, "fetch_strategy_timeout_ms", self.settings.request_timeout_ms))
        return max(1000, min(int(self.settings.request_timeout_ms), configured))

    @staticmethod
    def _domain_for_url(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        return (parsed.netloc or str(url or "").strip()).lower()

    def _strategy_plan(self, url: str, timeout_ms: int) -> list[tuple[str, Any]]:
        tries: list[tuple[str, Any]] = []
        browser_enabled = bool(getattr(self.settings, "enable_browser_tabs", True))
        profile = get_site_profile(url) if bool(getattr(self.settings, "enable_site_profiles", True)) else None
        if browser_enabled and self._browser_fetch_disabled_reason is None:
            tries.extend(
                [
                    (
                        "stealthy",
                        lambda: StealthyFetcher.fetch(
                            url,
                            headless=self.settings.headless_browser,
                            timeout=timeout_ms,
                            network_idle=True,
                            solve_cloudflare=self.settings.use_scrapling_cloudflare_solver,
                            google_search=False,
                        ),
                    ),
                    (
                        "dynamic",
                        lambda: DynamicFetcher.fetch(
                            url,
                            headless=self.settings.headless_browser,
                            timeout=timeout_ms,
                            network_idle=True,
                            google_search=False,
                        ),
                    ),
                ]
            )
        tries.extend(
            [
                ("curl_cffi", lambda: self._fetch_with_curl_cffi(url, timeout_ms)),
                (
                    "static_verified",
                    lambda: Fetcher.get(
                        url,
                        timeout=timeout_ms,
                        verify=self.settings.verify_ssl,
                        stealthy_headers=True,
                        google_search=False,
                    ),
                ),
                (
                    "static_unverified",
                    lambda: Fetcher.get(
                        url,
                        timeout=timeout_ms,
                        verify=False,
                        stealthy_headers=True,
                        google_search=False,
                    ),
                ),
            ]
        )
        strategy_names = [name for name, _ in tries]
        if profile is not None and profile.fetch_order:
            preferred_order = list(profile.fetch_order)
            strategy_names = preferred_order + [name for name in strategy_names if name not in preferred_order]
            lookup = {name: call for name, call in tries}
            tries = [(name, lookup[name]) for name in strategy_names if name in lookup]
        if self.state_store is not None:
            ordered_names = self.state_store.get_fetch_strategy_plan(
                self._domain_for_url(url),
                strategy_names,
                max_count=max(
                    1,
                    int(
                        profile.max_fetch_strategies
                        if profile is not None and profile.max_fetch_strategies is not None
                        else getattr(self.settings, "max_fetch_strategies_per_url", len(strategy_names))
                    ),
                ),
            )
            lookup = {name: call for name, call in tries}
            ordered_tries = [(name, lookup[name]) for name in ordered_names if name in lookup]
            if ordered_tries:
                return ordered_tries
        max_strategies = max(
            1,
            int(
                profile.max_fetch_strategies
                if profile is not None and profile.max_fetch_strategies is not None
                else getattr(self.settings, "max_fetch_strategies_per_url", len(tries))
            ),
        )
        return tries[:max_strategies]

    def _run_strategy_with_budget(
        self,
        *,
        url: str,
        domain: str,
        strategy_name: str,
        strategy_call: Any,
    ) -> FetchResult | Exception | None:
        retry_budget = max(0, int(getattr(self.settings, "network_retry_budget", 0)))
        base_backoff = max(0.1, float(getattr(self.settings, "network_backoff_base_seconds", 1.5)))
        for attempt in range(retry_budget + 1):
            started = time.monotonic()
            try:
                response = self._call_strategy_with_timeout(
                    strategy_call,
                    timeout_seconds=max(1.0, self._strategy_timeout_ms() / 1000.0),
                )
                latency_ms = (time.monotonic() - started) * 1000.0
                if response is None:
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        error="empty_response",
                    )
                    return None
                result = self._normalize_fetch_result(url=url, strategy_name=strategy_name, response=response)
                if result is None or not result.html:
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        error="empty_html",
                    )
                    return None
                if result.status in self._blocked_http_statuses():
                    self._remember_failure_reason(domain, "blocked_or_forbidden")
                    self.logger.warning(
                        "[fetch] strategy=%s returned blocked/denied status=%s for %s; stopping fetch attempts",
                        strategy_name,
                        result.status,
                        url,
                    )
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        status=result.status,
                        error="blocked_or_forbidden",
                    )
                    return result
                if result.status in {404, 410}:
                    self.logger.info(
                        "[fetch] strategy=%s found terminal status=%s url=%s; stopping further strategies",
                        strategy_name,
                        result.status,
                        url,
                    )
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        status=result.status,
                        error="terminal_not_found",
                    )
                    return None
                if self._looks_like_cloudflare_challenge(result.url, result.html):
                    self._remember_failure_reason(domain, "challenge_or_interstitial")
                    self.logger.warning(
                        "[fetch] strategy=%s returned a blocking challenge/interstitial for %s; falling back",
                        strategy_name,
                        url,
                    )
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        status=result.status,
                        error="challenge_or_interstitial",
                    )
                    return None
                if self._is_rate_limited_status(result.status) or self._looks_rate_limited_body(result.html):
                    self._remember_failure_reason(domain, "rate_limited")
                    self._record_fetch_metric("fetch.rate_limits")
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        rate_limited=True,
                        retries=attempt,
                        status=result.status,
                        error="rate_limited",
                    )
                    if attempt < retry_budget:
                        delay_seconds = self._compute_backoff(attempt + 1, base_backoff, rate_limited=True)
                        self.logger.warning(
                            "[fetch] strategy=%s rate-limited for %s; retry %s/%s in %.2fs",
                            strategy_name,
                            url,
                            attempt + 1,
                            retry_budget,
                            delay_seconds,
                        )
                        self._record_fetch_metric("fetch.retries")
                        time.sleep(delay_seconds)
                        continue
                    return None
                if result.status >= 500:
                    self._remember_failure_reason(domain, "transient")
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        status=result.status,
                        error=f"server_status_{result.status}",
                    )
                    if attempt < retry_budget:
                        delay_seconds = self._compute_backoff(attempt + 1, base_backoff)
                        self.logger.warning(
                            "[fetch] strategy=%s transient server status=%s for %s; retry %s/%s in %.2fs",
                            strategy_name,
                            result.status,
                            url,
                            attempt + 1,
                            retry_budget,
                            delay_seconds,
                        )
                        self._record_fetch_metric("fetch.retries")
                        time.sleep(delay_seconds)
                        continue
                    return None
                self.logger.info(
                    "[fetch] strategy=%s status=%s url=%s latency=%.0fms",
                    strategy_name,
                    result.status,
                    url,
                    latency_ms,
                )
                if strategy_name in {"stealthy", "dynamic"}:
                    self._reset_browser_fetch_health()
                self._record_fetch_strategy_result(
                    domain,
                    strategy_name,
                    success=True,
                    latency_ms=latency_ms,
                    retries=attempt,
                    status=result.status,
                )
                return result
            except Exception as exc:  # noqa: BLE001
                latency_ms = (time.monotonic() - started) * 1000.0
                if strategy_name in {"stealthy", "dynamic"} and self._looks_like_browser_driver_failure(exc):
                    self._browser_fetch_failure_count += 1
                    cooldown_base = max(30, int(getattr(self.settings, "browser_reprobe_cooldown_seconds", 600)))
                    cooldown_seconds = min(cooldown_base * max(1, 2 ** (self._browser_fetch_failure_count - 1)), 3600)
                    self._browser_fetch_disabled_reason = str(exc)
                    self._browser_fetch_disabled_until = time.monotonic() + cooldown_seconds
                    self._remember_failure_reason(domain, "browser_driver_failure")
                    self.logger.warning(
                        "[fetch] disabling browser-backed strategies after %s failed for %s: %s | cooldown=%ss before re-probe",
                        strategy_name,
                        url,
                        str(exc),
                        cooldown_seconds,
                    )
                    self._record_fetch_strategy_result(
                        domain,
                        strategy_name,
                        success=False,
                        latency_ms=latency_ms,
                        retries=attempt,
                        error=str(exc),
                    )
                    return exc
                timed_out = self._looks_like_timeout_error(exc)
                rate_limited = self._looks_like_rate_limit_error(exc)
                transient = timed_out or rate_limited or self._looks_like_transient_fetch_error(exc)
                if timed_out:
                    self._remember_failure_reason(domain, "timeout")
                elif rate_limited:
                    self._remember_failure_reason(domain, "rate_limited")
                elif transient:
                    self._remember_failure_reason(domain, "transient")
                else:
                    self._remember_failure_reason(domain, "error")
                if timed_out:
                    self._record_fetch_metric("fetch.timeouts")
                if rate_limited:
                    self._record_fetch_metric("fetch.rate_limits")
                self._record_fetch_strategy_result(
                    domain,
                    strategy_name,
                    success=False,
                    latency_ms=latency_ms,
                    timed_out=timed_out,
                    rate_limited=rate_limited,
                    retries=attempt,
                    error=str(exc),
                )
                if transient and attempt < retry_budget:
                    delay_seconds = self._compute_backoff(attempt + 1, base_backoff, rate_limited=rate_limited)
                    self.logger.warning(
                        "[fetch] strategy=%s transient failure for %s; retry %s/%s in %.2fs: %s",
                        strategy_name,
                        url,
                        attempt + 1,
                        retry_budget,
                        delay_seconds,
                        str(exc),
                    )
                    self._record_fetch_metric("fetch.retries")
                    time.sleep(delay_seconds)
                    continue
                self.logger.warning(
                    "[fetch] strategy=%s failed for %s: %s",
                    strategy_name,
                    url,
                    str(exc),
                )
                return exc
        return None

    @staticmethod
    def _call_strategy_with_timeout(strategy_call: Any, *, timeout_seconds: float) -> Any:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fetch-budget")
        future = executor.submit(strategy_call)
        try:
            return future.result(timeout=max(0.1, float(timeout_seconds)))
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"fetch strategy exceeded {timeout_seconds:.1f}s budget") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _normalize_fetch_result(url: str, strategy_name: str, response: Any) -> FetchResult | None:
        if isinstance(response, FetchResult):
            return response
        status = int(getattr(response, "status", 0))
        html = str(getattr(response, "html_content", "")) or str(getattr(response, "text", ""))
        response_url = str(getattr(response, "url", url))
        if not html:
            return None
        return FetchResult(
            url=response_url,
            status=status,
            html=html,
            page=response,
            strategy=strategy_name,
        )

    @staticmethod
    def _looks_like_timeout_error(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        return any(marker in text for marker in ("timeout", "timed out", "deadline exceeded", "read timed out"))

    @staticmethod
    def _looks_like_rate_limit_error(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        return any(marker in text for marker in ("429", "rate limit", "too many requests"))

    @staticmethod
    def _looks_like_transient_fetch_error(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        markers = (
            "temporar",
            "service unavailable",
            "connection reset",
            "connection aborted",
            "connection closed",
            "connection refused",
            "bad gateway",
            "gateway timeout",
            "internal server error",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_rate_limited_status(status: int) -> bool:
        return int(status or 0) == 429
    def _blocked_http_statuses(self) -> set[int]:
        raw_statuses = getattr(self.settings, "blocked_http_statuses", ()) or ()
        if isinstance(raw_statuses, str):
            raw_statuses = raw_statuses.replace(";", ",").split(",")
        statuses: set[int] = set()
        for raw_status in raw_statuses:
            try:
                statuses.add(int(raw_status))
            except (TypeError, ValueError):
                continue
        return statuses

    @staticmethod
    def _looks_rate_limited_body(html: str) -> bool:
        lowered = str(html or "").lower()
        return "too many requests" in lowered or "rate limit" in lowered

    @staticmethod
    def _compute_backoff(retry_attempt: int, base_seconds: float, *, rate_limited: bool = False) -> float:
        multiplier = 2 ** max(0, int(retry_attempt) - 1)
        if rate_limited:
            multiplier *= 2
        jitter = random.uniform(0.0, max(0.2, float(base_seconds)))
        return min(max(0.1, float(base_seconds)) * multiplier + jitter, 60.0)

    def _record_fetch_metric(self, metric_key: str, amount: float = 1.0) -> None:
        if self.state_store is None:
            return
        self.state_store.increment_runtime_metric(metric_key, amount)

    def _record_fetch_strategy_result(
        self,
        domain: str,
        strategy_name: str,
        *,
        success: bool,
        latency_ms: float,
        timed_out: bool = False,
        rate_limited: bool = False,
        retries: int = 0,
        skipped: bool = False,
        status: int | None = None,
        error: str = "",
    ) -> None:
        if self.state_store is None:
            return
        self.state_store.record_fetch_strategy_result(
            domain,
            strategy_name,
            success=success,
            latency_ms=latency_ms,
            timed_out=timed_out,
            rate_limited=rate_limited,
            retries=retries,
            skipped=skipped,
            status=status,
            error=error,
        )

    @staticmethod
    def _looks_like_browser_driver_failure(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        markers = (
            "broken pipe",
            "epipe",
            "pipe closed",
            "channel closed",
            "connection closed",
            "connection disposed",
            "target page, context or browser has been closed",
            "target browser, context or page has been closed",
            "browser has been closed",
            "browser closed",
            "playwright connection closed",
        )
        return any(marker in text for marker in markers)

    def _restore_browser_fetch_after_cooldown(self) -> None:
        if self._browser_fetch_disabled_reason is None:
            return
        if time.monotonic() < self._browser_fetch_disabled_until:
            return
        self.logger.info("[fetch] browser strategy cooldown elapsed; re-probing browser-backed fetches")
        self._browser_fetch_disabled_reason = None
        self._browser_fetch_disabled_until = 0.0

    def _reset_browser_fetch_health(self) -> None:
        self._browser_fetch_failure_count = 0
        self._browser_fetch_disabled_reason = None
        self._browser_fetch_disabled_until = 0.0

    @staticmethod
    def _failure_reason_priority(reason: str) -> int:
        priorities = {
            "challenge_or_interstitial": 50,
            "rate_limited": 40,
            "blocked_or_forbidden": 60,
            "browser_driver_failure": 30,
            "timeout": 20,
            "transient": 10,
            "error": 1,
        }
        return priorities.get(str(reason or "").strip().lower(), 0)

    def _remember_failure_reason(self, domain: str, reason: str) -> None:
        normalized_domain = self._domain_for_url(domain)
        normalized_reason = str(reason or "").strip().lower()
        if not normalized_domain or not normalized_reason:
            return
        previous = self._last_failure_reason_by_domain.get(normalized_domain, "")
        if self._failure_reason_priority(normalized_reason) >= self._failure_reason_priority(previous):
            self._last_failure_reason_by_domain[normalized_domain] = normalized_reason

    def last_failure_reason(self, url: str) -> str:
        return str(self._last_failure_reason_by_domain.get(self._domain_for_url(url), "")).strip().lower()
