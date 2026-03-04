"""API/DB automation for gateway error and refund flows.

Covers Automation-tagged scenarios:
- TC_10 Gateway API error handled gracefully
- TC_13 Refund on cancellation updates status
- TC_14 Refund idempotency (no double refund)
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import requests

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    timeout: Tuple[int, int]


def configure_logging() -> None:
    if logging.getLogger().handlers:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing test data file: {data_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in test data file: {data_path}") from exc


class ApiClient:
    def __init__(self, settings: ApiSettings, session: requests.Session):
        self.settings = settings
        self.session = session

    def url(self, path: str) -> str:
        return self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")

    def request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 2,
    ) -> requests.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(
                    method=method,
                    url=self.url(path),
                    json=json_body,
                    headers=headers,
                    timeout=self.settings.timeout,
                )
                return resp
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                LOG.warning("API request failed (attempt %s/%s): %s", attempt + 1, retries + 1, exc)
                time.sleep(1 + attempt)

        raise RuntimeError(f"API request failed after retries: {last_exc}")


class DbClient:
    def __init__(self, dsn: str):
        if psycopg2 is None:  # pragma: no cover
            raise RuntimeError("psycopg2 is not installed")
        self.dsn = dsn
        self.conn = None

    def __enter__(self):
        self.conn = psycopg2.connect(self.dsn)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
            self.conn.close()

    def fetch_one(self, query: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert self.conn is not None
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else {}


@pytest.fixture(scope="session", autouse=True)
def _logging() -> None:
    configure_logging()


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return load_test_data()


@pytest.fixture(scope="session")
def api_settings(test_data: Dict[str, Any]) -> ApiSettings:
    base_url = os.getenv("API_BASE_URL", test_data["api"]["base_url"])
    connect = int(test_data["api"]["timeouts"].get("connect", 5))
    read = int(test_data["api"]["timeouts"].get("read", 30))
    return ApiSettings(base_url=base_url, timeout=(connect, read))


@pytest.fixture(scope="session")
def api(api_settings: ApiSettings) -> ApiClient:
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return ApiClient(settings=api_settings, session=session)


def _poll_order_status(
    api: ApiClient,
    test_data: Dict[str, Any],
    order_id: str,
    timeout_s: int = 120,
) -> Dict[str, Any]:
    path = test_data["api"]["endpoints"]["order_status"].format(order_id=order_id)
    end = time.time() + timeout_s
    last: Dict[str, Any] = {}

    while time.time() < end:
        resp = api.request("GET", path)
        if resp.ok:
            last = resp.json()
            status = str(last.get("status", "")).upper()
            payment_status = str(last.get("payment_status", "")).upper()
            refund_status = str(last.get("refund_status", "")).upper()

            if any(x in refund_status for x in ("REFUND", "INITIAT")):
                return last
            if any(x in payment_status for x in ("PAID", "REFUND", "INITIAT")):
                return last

        time.sleep(2)
    return last


def _get_paid_order_id(api: ApiClient, test_data: Dict[str, Any]) -> str:
    """Return a paid order id.

    Priority:
    1) Use env PAID_ORDER_ID if provided.
    2) Try to create & pay via API endpoints.
    """
    paid = os.getenv("PAID_ORDER_ID")
    if paid:
        return paid

    create_path = test_data["api"]["endpoints"].get("create_order")
    pay_path = test_data["api"]["endpoints"].get("pay_order")
    if not create_path or not pay_path:
        pytest.skip("API endpoints for creating/paying orders not configured")

    create_resp = api.request("POST", create_path, json_body={"source": "qa_automation"})
    if not create_resp.ok:
        pytest.skip(f"Cannot create order via API (HTTP {create_resp.status_code})")

    order_id = create_resp.json().get("order_id")
    if not order_id:
        pytest.skip("Create order response missing order_id")

    card = test_data["payments"]["card_valid"]
    pay_payload = {
        "order_id": order_id,
        "method": "card",
        "card": card,
    }
    pay_resp = api.request("POST", pay_path, json_body=pay_payload)
    if not pay_resp.ok:
        pytest.skip(f"Cannot pay order via API (HTTP {pay_resp.status_code})")

    return str(order_id)


def _db_dsn() -> str:
    dsn = os.getenv("DB_DSN")
    if not dsn:
        pytest.skip("DB_DSN env var not set; skipping DB assertions")
    if psycopg2 is None:
        pytest.skip("psycopg2 not installed; skipping DB assertions")
    return dsn

def test_tc10_gateway_api_error_handled_gracefully(api: ApiClient, test_data: Dict[str, Any]) -> None:
    """TC_10: Gateway API error (HTTP 5xx/invalid payload) handled gracefully."""
    pay_path = test_data["api"]["endpoints"].get("pay_order")
    if not pay_path:
        pytest.skip("pay_order endpoint not configured")

    resp = api.request(
        "POST",
        pay_path,
        json_body={"malformed": True},
        headers={"X-Force-Gateway-Error": "500"},
        retries=1,
    )

    assert resp.status_code >= 400, "Expected a handled error response for forced gateway failure"

    content_type = resp.headers.get("Content-Type", "")
    if "application/json" in content_type:
        body = resp.json()
        assert any(k in body for k in ("error", "message", "details")), body
    else:
        # Still acceptable as long as service returns promptly with a readable payload.
        assert resp.text.strip(), "Expected non-empty error response body"


def test_tc13_order_cancellation_triggers_refund(api: ApiClient, test_data: Dict[str, Any]) -> None:
    """TC_13: Order cancellation after successful payment triggers refund and status update."""
    order_id = _get_paid_order_id(api, test_data)
    cancel_path = test_data["api"]["endpoints"]["cancel_order"].format(order_id=order_id)

    cancel_resp = api.request("POST", cancel_path, json_body={"reason": "qa_automation"})
    assert cancel_resp.status_code in {200, 202}, cancel_resp.text

    status = _poll_order_status(api, test_data, order_id=order_id, timeout_s=180)
    assert status, "Expected order status response"

    refund_status = str(status.get("refund_status", "")).upper()
    payment_status = str(status.get("payment_status", "")).upper()
    assert any(x in refund_status for x in ("REFUND", "INITIAT")) or any(
        x in payment_status for x in ("REFUND", "INITIAT")
    ), status

    # DB verification (optional)
    dsn = _db_dsn()
    with DbClient(dsn) as db:
        count_row = db.fetch_one(
            test_data["db"]["refunds_count_query"],
            params={"order_id": order_id},
        )
        assert int(count_row.get("cnt", 0)) >= 1, count_row

        latest = db.fetch_one(
            test_data["db"]["latest_refund_query"],
            params={"order_id": order_id},
        )
        assert latest.get("refund_id"), latest
        assert str(latest.get("status", "")).upper() in {
            "INITIATED",
            "IN_PROGRESS",
            "REFUNDED",
            "SUCCESS",
        }, latest


def test_tc14_refund_idempotency_no_double_refund(api: ApiClient, test_data: Dict[str, Any]) -> None:
    """TC_14: Repeated cancellation/refund requests should not double-refund."""
    order_id = _get_paid_order_id(api, test_data)
    cancel_path = test_data["api"]["endpoints"]["cancel_order"].format(order_id=order_id)

    dsn = _db_dsn()
    with DbClient(dsn) as db:
        before = db.fetch_one(
            test_data["db"]["refunds_count_query"],
            params={"order_id": order_id},
        )
        before_cnt = int(before.get("cnt", 0))

        first = api.request("POST", cancel_path, json_body={"reason": "qa_automation"})
        second = api.request("POST", cancel_path, json_body={"reason": "qa_automation"})

        assert first.status_code in {200, 202, 409}, first.text
        assert second.status_code in {200, 202, 409}, second.text

        # Give asynchronous refund creation time (if any)
        time.sleep(3)

        after = db.fetch_one(
            test_data["db"]["refunds_count_query"],
            params={"order_id": order_id},
        )
        after_cnt = int(after.get("cnt", 0))

        assert after_cnt <= max(before_cnt + 1, 1), {
            "before": before_cnt,
            "after": after_cnt,
        }
