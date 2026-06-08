from __future__ import annotations

import logging
from io import BytesIO
import urllib.error
import unittest

from job_bot.nowpayments_client import NowPaymentsClient


class NowPaymentsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_mode_creates_invoice_and_waiting_status(self) -> None:
        client = NowPaymentsClient(api_key="", logger=logging.getLogger("test"))
        invoice = await client.create_invoice(
            amount_usd=1.0,
            order_id="test-order-1",
            order_description="monthly subscription",
        )
        self.assertTrue(invoice.provider_payment_id.startswith("mock-"))
        self.assertIn("nowpayments.io", invoice.payment_url)
        self.assertEqual(invoice.status, "waiting")

        status = await client.get_payment_status(invoice.provider_payment_id)
        self.assertEqual(status, "waiting")

    async def test_base_url_normalization_avoids_double_v1(self) -> None:
        client = NowPaymentsClient(
            api_key="test-key",
            logger=logging.getLogger("test"),
            base_url="https://api.nowpayments.io/v1",
        )
        self.assertEqual(client.base_url, "https://api.nowpayments.io")
        self.assertEqual(client._compose_url("/v1/invoice"), "https://api.nowpayments.io/v1/invoice")
        self.assertEqual(client._compose_url("/invoice"), "https://api.nowpayments.io/v1/invoice")

    async def test_default_headers_include_user_agent_and_accept(self) -> None:
        client = NowPaymentsClient(api_key="test-key", logger=logging.getLogger("test"))
        headers = client._build_headers(use_auth_token=False)
        self.assertEqual(headers["Accept"], "application/json")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["x-api-key"], "test-key")

    async def test_get_payment_status_uses_invoice_query_fallback(self) -> None:
        client = NowPaymentsClient(api_key="test-key", logger=logging.getLogger("test"))
        client._auth_token = "test-token"  # type: ignore[attr-defined]

        def fake_request_json(method: str, path: str, **_: object) -> dict:
            if path == "/v1/payment/inv-123":
                raise urllib.error.HTTPError(
                    url="https://api.nowpayments.io/v1/payment/inv-123",
                    code=404,
                    msg="Not Found",
                    hdrs=None,
                    fp=BytesIO(b'{"status":false,"statusCode":404,"message":"Payment not found"}'),
                )
            if path.startswith("/v1/payment/?invoiceid=inv-123"):
                return {"data": [{"payment_status": "finished"}]}
            raise AssertionError(f"Unexpected path: {path}")

        client._request_json = fake_request_json  # type: ignore[method-assign]
        status = await client.get_payment_status("inv-123")
        self.assertEqual(status, "finished")

    async def test_get_payment_status_returns_waiting_on_404(self) -> None:
        client = NowPaymentsClient(api_key="test-key", logger=logging.getLogger("test"))

        def fake_request_json(method: str, path: str, **_: object) -> dict:
            if path == "/v1/payment/inv-404":
                raise urllib.error.HTTPError(
                    url="https://api.nowpayments.io/v1/payment/inv-404",
                    code=404,
                    msg="Not Found",
                    hdrs=None,
                    fp=BytesIO(b'{"status":false,"statusCode":404,"message":"Payment not found"}'),
                )
            if path.startswith("/v1/payment/?invoiceid=inv-404"):
                return {"data": []}
            raise AssertionError(f"Unexpected path: {path}")

        client._request_json = fake_request_json  # type: ignore[method-assign]
        status = await client.get_payment_status("inv-404")
        self.assertEqual(status, "waiting")


if __name__ == "__main__":
    unittest.main()
