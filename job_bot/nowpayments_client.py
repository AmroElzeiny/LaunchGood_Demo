from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass


@dataclass(slots=True)
class NowPaymentsInvoice:
    provider_payment_id: str
    payment_url: str
    status: str


class NowPaymentsClient:
    def __init__(
        self,
        api_key: str,
        logger: logging.Logger,
        base_url: str = "https://api.nowpayments.io",
        email: str = "",
        password: str = "",
    ) -> None:
        self.api_key = api_key.strip()
        self.logger = logger
        self.base_url = self._normalize_base_url(base_url)
        self.email = email.strip()
        self.password = password.strip()
        self._auth_token = ""
        self._auth_refresh_disabled = False

    async def create_invoice(
        self,
        amount_usd: float,
        order_id: str,
        order_description: str,
    ) -> NowPaymentsInvoice:
        if not self.api_key:
            mock_id = f"mock-{uuid.uuid4().hex[:12]}"
            return NowPaymentsInvoice(
                provider_payment_id=mock_id,
                payment_url=f"https://nowpayments.io/payment/?iid={mock_id}",
                status="waiting",
            )

        payload = {
            "price_amount": float(amount_usd),
            "price_currency": "usd",
            "order_id": order_id,
            "order_description": order_description,
            "is_fixed_rate": True,
            "is_fee_paid_by_user": False,
        }
        response = await asyncio.to_thread(
            self._request_json,
            method="POST",
            path="/v1/invoice",
            payload=payload,
            allow_auth_retry=True,
        )

        invoice_id = str(response.get("id") or response.get("invoice_id") or response.get("payment_id") or "")
        if not invoice_id:
            raise RuntimeError("NOWPayments response missing invoice id.")

        payment_url = str(response.get("invoice_url") or response.get("pay_url") or response.get("payment_url") or "")
        if not payment_url:
            payment_url = f"https://nowpayments.io/payment/?iid={invoice_id}"

        status = str(response.get("status") or response.get("invoice_status") or "waiting").lower()
        return NowPaymentsInvoice(provider_payment_id=invoice_id, payment_url=payment_url, status=status)

    async def get_payment_status(self, provider_payment_id: str) -> str:
        if provider_payment_id.startswith("mock-") or not self.api_key:
            return "waiting"

        # Primary path for payment status
        primary_error: Exception | None = None
        try:
            response = await asyncio.to_thread(
                self._request_json,
                method="GET",
                path=f"/v1/payment/{provider_payment_id}",
                allow_auth_retry=True,
                log_http_errors=False,
            )
            status = self._extract_status_from_object(response)
            if status:
                return status
        except Exception as exc:  # noqa: BLE001
            primary_error = exc

        # Invoice-based checkout fallback:
        # query payments filtered by invoice id.
        # Note: /v1/invoice/{id} is not a valid status endpoint.
        can_use_auth_query = bool(self._auth_token) or self._try_refresh_auth_token()
        if can_use_auth_query:
            try:
                invoice_id = urllib.parse.quote(str(provider_payment_id), safe="")
                response = await asyncio.to_thread(
                    self._request_json,
                    method="GET",
                    path=f"/v1/payment/?invoiceid={invoice_id}&limit=1&page=0",
                    allow_auth_retry=False,
                    use_auth_token=True,
                    log_http_errors=False,
                )
                status = self._extract_status_from_list_response(response)
                if status:
                    return status
            except Exception as exc:  # noqa: BLE001
                if primary_error is None:
                    primary_error = exc

        # If payment is not yet materialized by provider, treat as waiting.
        if isinstance(primary_error, urllib.error.HTTPError) and primary_error.code == 404:
            return "waiting"
        if primary_error is not None:
            raise primary_error
        return "waiting"

    @staticmethod
    def _extract_status_from_object(response: object) -> str:
        if not isinstance(response, dict):
            return ""
        return str(
            response.get("payment_status")
            or response.get("invoice_status")
            or response.get("status")
            or "waiting"
        ).lower()

    def _extract_status_from_list_response(self, response: object) -> str:
        items: list[object] = []
        if isinstance(response, list):
            items = response
        elif isinstance(response, dict):
            for key in ("data", "result", "payments"):
                value = response.get(key)
                if isinstance(value, list):
                    items = value
                    break
            if not items:
                return self._extract_status_from_object(response)

        if not items:
            return ""
        first = items[0]
        return self._extract_status_from_object(first)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        allow_auth_retry: bool = False,
        use_auth_token: bool = False,
        log_http_errors: bool = True,
    ) -> dict:
        url = self._compose_url(path)
        body = None
        headers = self._build_headers(use_auth_token=use_auth_token)

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if log_http_errors:
                self.logger.warning("[nowpayments] http error %s on %s: %s", exc.code, path, error_body)
            if allow_auth_retry and exc.code in {401, 403}:
                if self._try_refresh_auth_token():
                    return self._request_json(
                        method=method,
                        path=path,
                        payload=payload,
                        allow_auth_retry=False,
                        use_auth_token=True,
                        log_http_errors=log_http_errors,
                    )
            raise

    def _build_headers(self, use_auth_token: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # NOWPayments sits behind Cloudflare; default urllib UA can trigger 1010 blocks.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if use_auth_token and self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _try_refresh_auth_token(self) -> bool:
        if self._auth_refresh_disabled:
            return False
        if not self.email or not self.password:
            return False
        try:
            auth_response = self._request_json_without_retry(
                method="POST",
                path="/v1/auth",
                payload={"email": self.email, "password": self.password},
            )
        except Exception as exc:  # noqa: BLE001
            self._auth_refresh_disabled = True
            self.logger.debug(
                "[nowpayments] auth fallback disabled after failure; invoice-query checks will be skipped: %s",
                str(exc),
            )
            return False

        token = str(auth_response.get("token") or auth_response.get("access_token") or "").strip()
        if not token:
            self._auth_refresh_disabled = True
            return False
        self._auth_token = token
        self._auth_refresh_disabled = False
        return True

    def _request_json_without_retry(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self._compose_url(path)
        body = None
        headers = self._build_headers(use_auth_token=True)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[: -len("/v1")]
        if not normalized:
            normalized = "https://api.nowpayments.io"
        return normalized

    def _compose_url(self, path: str) -> str:
        normalized_path = "/" + path.lstrip("/")
        if normalized_path.startswith("/v1/"):
            return f"{self.base_url}{normalized_path}"
        return f"{self.base_url}/v1{normalized_path}"
