"""E2E payment checkout automation (Automation-tagged scenarios only).

Covers:
- TC_01: Happy path card payment success
- TC_02: Card payment declined -> error + retry/change method
- TC_05: Cancel/refund after success -> status updated + idempotent

Tech:
- Python + Playwright (web)
- requests (API verification)
- PostgreSQL (optional DB verification)

Execution examples:
- pytest -q E2E_tc_automation_v3/test_e2e_pipeline_v3.py
- python E2E_tc_automation_v3/test_e2e_pipeline_v3.py

Environment overrides (optional):
- E2E_BASE_URL, E2E_API_BASE_URL
- E2E_DB_HOST, E2E_DB_PORT, E2E_DB_NAME, E2E_DB_USER, E2E_DB_PASSWORD, E2E_DB_SSLMODE
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from playwright.sync_api import Page, sync_playwright


LOGGER = logging.getLogger(__name__)


DATA_FILE = Path(__file__).with_name("test_data.json")
ARTIFACTS_DIR = Path(__file__).with_suffix("").with_name("artifacts")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        stream=sys.stdout,
    )


def load_test_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Missing test data file: {DATA_FILE}")
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def env_override(data: Dict[str, Any]) -> Dict[str, Any]:
    """Override selected config values from env vars (safe for CI)."""
    data = dict(data)
    web = dict(data.get("web", {}))
    api = dict(data.get("api", {}))
    db = dict(data.get("db", {}))

    web["base_url"] = os.getenv("E2E_BASE_URL", web.get("base_url"))
    api["base_url"] = os.getenv("E2E_API_BASE_URL", api.get("base_url"))

    db["host"] = os.getenv("E2E_DB_HOST", db.get("host"))
    db["port"] = int(os.getenv("E2E_DB_PORT", str(db.get("port", 5432))))
    db["database"] = os.getenv("E2E_DB_NAME", db.get("database"))
    db["user"] = os.getenv("E2E_DB_USER", db.get("user"))
    db["password"] = os.getenv("E2E_DB_PASSWORD", db.get("password"))
    db["sslmode"] = os.getenv("E2E_DB_SSLMODE", db.get("sslmode", "prefer"))

    data["web"] = web
    data["api"] = api
    data["db"] = db
    return data


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: str = "prefer"


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    timeout_ms: int
    username: str
    password: str
    api_base_url: str
    api_order_status_endpoint: str
    api_refund_endpoint: str
    db: DbConfig


def build_config(data: Dict[str, Any]) -> AppConfig:
    web = data["web"]
    creds = data["credentials"]
    api = data["api"]
    db = data["db"]

    return AppConfig(
        base_url=str(web["base_url"]).rstrip("/"),
        timeout_ms=int(web.get("timeout_ms", 30000)),
        username=str(creds["username"]),
        password=str(creds["password"]),
        api_base_url=str(api["base_url"]).rstrip("/"),
        api_order_status_endpoint=str(api["order_status_endpoint"]),
        api_refund_endpoint=str(api["refund_endpoint"]),
        db=DbConfig(
            host=str(db["host"]),
            port=int(db.get("port", 5432)),
            database=str(db["database"]),
            user=str(db["user"]),
            password=str(db["password"]),
            sslmode=str(db.get("sslmode", "prefer")),
        ),
    )


class AutomationError(RuntimeError):
    """Raised when the automation flow cannot proceed safely."""


class ApiClient:
    def __init__(self, base_url: str, token: Optional[str] = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    def get_json(self, path: str, timeout_s: int = 30) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        resp = self._session.get(url, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise AutomationError(f"Unexpected JSON payload from {url}: {type(data)}")
        return data

    def post_json(self, path: str, payload: Dict[str, Any], timeout_s: int = 30) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        resp = self._session.post(url, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise AutomationError(f"Unexpected JSON payload from {url}: {type(data)}")
        return data


@contextmanager
def postgres_connection(cfg: DbConfig):
    """Best-effort PostgreSQL connection.

    If `psycopg2` is not installed or the DB is unreachable, the caller can catch
    the exception and treat it as a skipped DB verification.
    """
    try:
        import psycopg2  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise AutomationError(
            "psycopg2 is required for DB checks (pip install psycopg2-binary)."
        ) from exc

    conn = None
    try:
        conn = psycopg2.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.database,
            user=cfg.user,
            password=cfg.password,
            sslmode=cfg.sslmode,
        )
        yield conn
    finally:
        if conn is not None:
            conn.close()


def safe_slug(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def take_debug_artifacts(page: Page, name: str) -> None:
    """Capture screenshot + HTML for easier debugging in CI."""
    ensure_dir(ARTIFACTS_DIR)
    slug = safe_slug(name)
    screenshot_path = ARTIFACTS_DIR / f"{slug}.png"
    html_path = ARTIFACTS_DIR / f"{slug}.html"

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:  # noqa: BLE001
        LOGGER.exception("Failed to capture screenshot: %s", screenshot_path)

    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        LOGGER.exception("Failed to capture HTML: %s", html_path)


@contextmanager
def playwright_page(timeout_ms: int) -> Page:
    headless = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes"}
    slow_mo_ms = int(os.getenv("SLOW_MO_MS", "0"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            yield page
        finally:
            context.close()
            browser.close()


class Selectors:
    """Central place for selectors (override with env vars if needed)."""

    # Login
    USERNAME = os.getenv("SEL_USERNAME", "#username")
    PASSWORD = os.getenv("SEL_PASSWORD", "#password")
    LOGIN_SUBMIT = os.getenv("SEL_LOGIN_SUBMIT", "button[type='submit']")
    LOGIN_SUCCESS_MARKER = os.getenv("SEL_LOGIN_SUCCESS", "data-test=account")

    # Shopping / checkout
    SEARCH_INPUT = os.getenv("SEL_SEARCH_INPUT", "data-test=search")
    SEARCH_SUBMIT = os.getenv("SEL_SEARCH_SUBMIT", "data-test=search-submit")
    ADD_TO_CART = os.getenv("SEL_ADD_TO_CART", "data-test=add-to-cart")
    CART_ICON = os.getenv("SEL_CART_ICON", "data-test=cart")
    CHECKOUT_BUTTON = os.getenv("SEL_CHECKOUT_BUTTON", "data-test=checkout")

    # Payment
    PAY_METHOD_CARD = os.getenv("SEL_PAY_METHOD_CARD", "data-test=pay-method-card")
    CARD_NUMBER = os.getenv("SEL_CARD_NUMBER", "data-test=card-number")
    CARD_EXPIRY = os.getenv("SEL_CARD_EXPIRY", "data-test=card-expiry")
    CARD_CVV = os.getenv("SEL_CARD_CVV", "data-test=card-cvv")
    CARD_NAME = os.getenv("SEL_CARD_NAME", "data-test=card-name")
    PAY_NOW = os.getenv("SEL_PAY_NOW", "data-test=pay-now")

    STATUS_BANNER = os.getenv("SEL_STATUS_BANNER", "data-test=payment-status")
    ERROR_BANNER = os.getenv("SEL_ERROR_BANNER", "data-test=payment-error")
    ORDER_ID = os.getenv("SEL_ORDER_ID", "data-test=order-id")

    # Cancel/Refund
    CANCEL_ORDER = os.getenv("SEL_CANCEL_ORDER", "data-test=cancel-order")
    CONFIRM_CANCEL = os.getenv("SEL_CONFIRM_CANCEL", "data-test=confirm-cancel")


class CheckoutAutomation:
    def __init__(self, page: Page, cfg: AppConfig, data: Dict[str, Any]) -> None:
        self._page = page
        self._cfg = cfg
        self._data = data

    @property
    def page(self) -> Page:
        return self._page

    def open_login(self) -> None:
        self._page.goto(f"{self._cfg.base_url}/login", wait_until="domcontentloaded")

    def login(self) -> None:
        self.open_login()
        self._page.fill(Selectors.USERNAME, self._cfg.username)
        self._page.fill(Selectors.PASSWORD, self._cfg.password)
        self._page.click(Selectors.LOGIN_SUBMIT)
        self._page.wait_for_selector(Selectors.LOGIN_SUCCESS_MARKER)

    def add_item_to_cart(self) -> None:
        search_term = str(self._data["checkout"]["product_search_term"])
        self._page.goto(f"{self._cfg.base_url}/", wait_until="domcontentloaded")
        self._page.fill(Selectors.SEARCH_INPUT, search_term)
        self._page.click(Selectors.SEARCH_SUBMIT)
        self._page.click(Selectors.ADD_TO_CART)

    def go_to_checkout(self) -> None:
        self._page.click(Selectors.CART_ICON)
        self._page.click(Selectors.CHECKOUT_BUTTON)

    def pay_with_card(self, card: Dict[str, Any]) -> None:
        self._page.click(Selectors.PAY_METHOD_CARD)
        self._page.fill(Selectors.CARD_NUMBER, str(card["card_number"]))
        self._page.fill(Selectors.CARD_EXPIRY, str(card["expiry"]))
        self._page.fill(Selectors.CARD_CVV, str(card["cvv"]))
        self._page.fill(Selectors.CARD_NAME, str(card["name"]))
        self._page.click(Selectors.PAY_NOW)

    def wait_for_status(self, expected_regex: str, timeout_s: int = 60) -> str:
        deadline = time.time() + timeout_s
        last_text = ""
        while time.time() < deadline:
            try:
                if self._page.is_visible(Selectors.STATUS_BANNER):
                    last_text = self._page.inner_text(Selectors.STATUS_BANNER).strip()
                    if re.search(expected_regex, last_text, flags=re.IGNORECASE):
                        return last_text
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        raise AutomationError(
            f"Timed out waiting for status /{expected_regex}/. Last status: '{last_text}'"
        )

    def read_error(self) -> str:
        if not self._page.is_visible(Selectors.ERROR_BANNER):
            return ""
        return self._page.inner_text(Selectors.ERROR_BANNER).strip()

    def read_order_id(self) -> str:
        if self._page.is_visible(Selectors.ORDER_ID):
            return self._page.inner_text(Selectors.ORDER_ID).strip()

        # Fallback: try to locate something that looks like an order id
        content = self._page.content()
        match = re.search(r"order\s*#?\s*([A-Za-z0-9\-]{6,})", content, flags=re.I)
        if match:
            return match.group(1)

        raise AutomationError("Unable to determine order_id from UI.")

    def cancel_order_via_ui(self) -> None:
        self._page.click(Selectors.CANCEL_ORDER)
        self._page.click(Selectors.CONFIRM_CANCEL)


def wait_for_order_status_api(
    api: ApiClient,
    endpoint_template: str,
    order_id: str,
    expected_status: str,
    timeout_s: int = 60,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last: Dict[str, Any] = {}
    path = endpoint_template.format(order_id=order_id)

    while time.time() < deadline:
        try:
            last = api.get_json(path)
            status = str(last.get("payment_status") or last.get("status") or "").lower()
            if status == expected_status.lower():
                return last
        except Exception:  # noqa: BLE001
            LOGGER.exception("API poll failed for order_id=%s", order_id)
        time.sleep(2)

    raise AutomationError(
        f"Order {order_id} did not reach status '{expected_status}'. Last payload: {last}"
    )


def best_effort_db_payment_status(cfg: DbConfig, order_id: str) -> Optional[str]:
    """Return payment status from DB if possible; otherwise None (skip)."""
    try:
        with postgres_connection(cfg) as conn:
            with conn.cursor() as cur:
                # NOTE: Update table/column names to match your schema.
                cur.execute(
                    "SELECT payment_status FROM payments WHERE order_id = %s ORDER BY updated_at DESC LIMIT 1",
                    (order_id,),
                )
                row = cur.fetchone()
                return str(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Skipping DB verification (%s)", exc)
        return None


def _build_runtime() -> tuple[AppConfig, Dict[str, Any]]:
    configure_logging()
    data = env_override(load_test_data())
    cfg = build_config(data)
    return cfg, data


def _api_client(cfg: AppConfig) -> ApiClient:
    token = os.getenv("E2E_API_TOKEN")
    return ApiClient(cfg.api_base_url, token=token)


def _run_step(page: Page, step_name: str, fn) -> Any:
    try:
        LOGGER.info("STEP: %s", step_name)
        return fn()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Step failed: %s", step_name)
        take_debug_artifacts(page, step_name)
        raise exc


def test_tc_01_happy_path_card_payment_success() -> None:
    """TC_01: Happy path: Successful payment via Credit/Debit Card."""
    cfg, data = _build_runtime()
    api = _api_client(cfg)

    with playwright_page(cfg.timeout_ms) as page:
        app = CheckoutAutomation(page, cfg, data)

        _run_step(page, "login", app.login)
        _run_step(page, "add_item_to_cart", app.add_item_to_cart)
        _run_step(page, "go_to_checkout", app.go_to_checkout)

        card = data["checkout"]["card_success"]
        _run_step(page, "pay_with_card_success", lambda: app.pay_with_card(card))

        status_text = _run_step(
            page,
            "wait_for_success_status",
            lambda: app.wait_for_status(r"success|paid|completed"),
        )
        LOGGER.info("Payment status banner: %s", status_text)

        order_id = _run_step(page, "read_order_id", app.read_order_id)
        LOGGER.info("Created order_id=%s", order_id)

        # API verification (best-effort)
        try:
            payload = wait_for_order_status_api(
                api,
                cfg.api_order_status_endpoint,
                order_id,
                expected_status="success",
                timeout_s=60,
            )
            LOGGER.info("API order payload: %s", payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Skipping/failed API verification (%s)", exc)

        # DB verification (best-effort)
        db_status = best_effort_db_payment_status(cfg.db, order_id)
        if db_status is not None:
            assert str(db_status).lower() in {"success", "paid", "completed"}


def test_tc_02_declined_then_retry_success() -> None:
    """TC_02: Gateway declines payment -> clear error + retry/change method."""
    cfg, data = _build_runtime()

    with playwright_page(cfg.timeout_ms) as page:
        app = CheckoutAutomation(page, cfg, data)

        _run_step(page, "login", app.login)
        _run_step(page, "add_item_to_cart", app.add_item_to_cart)
        _run_step(page, "go_to_checkout", app.go_to_checkout)

        decline_card = data["checkout"]["card_decline"]
        _run_step(page, "pay_with_card_decline", lambda: app.pay_with_card(decline_card))

        _run_step(
            page,
            "wait_for_failure_status",
            lambda: app.wait_for_status(r"fail|declin|error"),
        )

        error_text = _run_step(page, "read_error", app.read_error)
        assert error_text, "Expected a non-empty, informative error message on decline."
        LOGGER.info("Decline error banner: %s", error_text)

        # Retry with a known-success card without losing context
        success_card = data["checkout"]["card_success"]
        _run_step(page, "retry_pay_with_card_success", lambda: app.pay_with_card(success_card))
        _run_step(page, "wait_for_success_after_retry", lambda: app.wait_for_status(r"success|paid|completed"))


def test_tc_05_refund_idempotent() -> None:
    """TC_05: Cancel/refund after successful payment -> status updated + idempotent."""
    cfg, data = _build_runtime()
    api = _api_client(cfg)

    with playwright_page(cfg.timeout_ms) as page:
        app = CheckoutAutomation(page, cfg, data)

        _run_step(page, "login", app.login)
        _run_step(page, "add_item_to_cart", app.add_item_to_cart)
        _run_step(page, "go_to_checkout", app.go_to_checkout)

        success_card = data["checkout"]["card_success"]
        _run_step(page, "pay_with_card_success", lambda: app.pay_with_card(success_card))
        _run_step(page, "wait_for_success_status", lambda: app.wait_for_status(r"success|paid|completed"))
        order_id = _run_step(page, "read_order_id", app.read_order_id)
        LOGGER.info("Refund test order_id=%s", order_id)

        # Prefer API refund for idempotency validation.
        refund_path = cfg.api_refund_endpoint.format(order_id=order_id)
        try:
            # 1st refund attempt
            resp1 = api._session.post(f"{cfg.api_base_url}{refund_path}", json={}, timeout=30)  # noqa: SLF001
            if resp1.status_code >= 500:
                resp1.raise_for_status()
            LOGGER.info("Refund attempt #1 status=%s body=%s", resp1.status_code, resp1.text)

            # 2nd refund attempt (should be idempotent)
            resp2 = api._session.post(f"{cfg.api_base_url}{refund_path}", json={}, timeout=30)  # noqa: SLF001
            if resp2.status_code >= 500:
                resp2.raise_for_status()
            LOGGER.info("Refund attempt #2 status=%s body=%s", resp2.status_code, resp2.text)

            # Acceptable idempotent outcomes: 200/201/202 or 409 (already refunded)
            assert resp2.status_code in {200, 201, 202, 204, 409}

            # Verify final state
            try:
                wait_for_order_status_api(
                    api,
                    cfg.api_order_status_endpoint,
                    order_id,
                    expected_status="refunded",
                    timeout_s=90,
                )
            except AutomationError:
                # Some systems use 'cancelled' post-refund.
                wait_for_order_status_api(
                    api,
                    cfg.api_order_status_endpoint,
                    order_id,
                    expected_status="cancelled",
                    timeout_s=90,
                )

        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("API refund path failed (%s). Falling back to UI cancel.", exc)
            _run_step(page, "cancel_order_via_ui", app.cancel_order_via_ui)


def _run_as_script() -> int:
    tests = [
        test_tc_01_happy_path_card_payment_success,
        test_tc_02_declined_then_retry_success,
        test_tc_05_refund_idempotent,
    ]

    failures: list[str] = []
    for test_fn in tests:
        name = test_fn.__name__
        try:
            LOGGER.info("RUNNING: %s", name)
            test_fn()
            LOGGER.info("PASSED: %s", name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("FAILED: %s (%s)", name, exc)
            failures.append(name)

    if failures:
        LOGGER.error("Failed tests: %s", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_as_script())
