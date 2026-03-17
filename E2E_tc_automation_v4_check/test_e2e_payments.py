"""E2E payment automation (Playwright + Requests + optional PostgreSQL checks).

Covers Automation-tagged scenarios from test_v2.xlsx:
- TC_01: Card payment success
- TC_02: Card payment failure due to invalid details with retry / change method
- TC_04: Gateway timeout with retry and no double-charge
- TC_06: Wallet insufficient balance with top-up / alternate method

Execution:
  pytest -q E2E_tc_automation_v4_check/test_e2e_payments.py

Configuration:
  Update E2E_tc_automation_v4_check/test_data.json and/or env vars.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import requests
from playwright.sync_api import Page, Playwright, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebConfig:
    base_url: str
    login_path: str
    checkout_path: str
    selectors: Dict[str, str]


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    token_env: str
    endpoints: Dict[str, str]


@dataclass(frozen=True)
class DbConfig:
    enabled: bool
    dsn_env: str
    queries: Dict[str, str]


@dataclass(frozen=True)
class TestConfig:
    web: WebConfig
    user: Dict[str, str]
    payment: Dict[str, Any]
    api: ApiConfig
    db: DbConfig


def _load_test_config() -> TestConfig:
    data_path = Path(__file__).with_name("test_data.json")
    payload = json.loads(data_path.read_text(encoding="utf-8"))

    web = payload["web"]
    api = payload.get("api", {})
    db = payload.get("db", {})

    return TestConfig(
        web=WebConfig(
            base_url=web["base_url"].rstrip("/"),
            login_path=web.get("login_path", "/login"),
            checkout_path=web.get("checkout_path", "/checkout"),
            selectors=web.get("selectors", {}),
        ),
        user=payload.get("user", {}),
        payment=payload.get("payment", {}),
        api=ApiConfig(
            base_url=str(api.get("base_url", "")).rstrip("/"),
            token_env=str(api.get("auth", {}).get("token_env", "API_TOKEN")),
            endpoints=api.get("endpoints", {}),
        ),
        db=DbConfig(
            enabled=bool(db.get("enabled", False)),
            dsn_env=str(db.get("dsn_env", "POSTGRES_DSN")),
            queries=db.get("queries", {}),
        ),
    )


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@pytest.fixture(scope="session")
def cfg() -> TestConfig:
    _configure_logging()
    return _load_test_config()


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture()
def page(playwright_instance: Playwright) -> Page:
    browser = playwright_instance.chromium.launch(
        headless=os.getenv("HEADLESS", "true").lower() == "true"
    )
    context = browser.new_context()
    page_obj = context.new_page()

    yield page_obj

    context.close()
    browser.close()


def _safe_screenshot(page: Page, name: str) -> Optional[str]:
    try:
        out_dir = Path(os.getenv("ARTIFACTS_DIR", "."))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        LOGGER.exception("Failed to take screenshot")
        return None


def _require_selectors(selectors: Dict[str, str], keys: List[str]) -> None:
    missing = [key for key in keys if key not in selectors]
    if missing:
        raise KeyError(
            "Missing required selectors in test_data.json: "
            f"{', '.join(missing)}"
        )


def _click_if_visible(page: Page, selector: str, timeout_ms: int = 1_000) -> bool:
    locator = page.locator(selector)
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return False

    locator.click()
    return True


def _wait_for_status_text(
    page: Page,
    selector: str,
    expected_substrings: List[str],
    timeout_s: int = 60,
    poll_s: float = 1.0,
) -> str:
    end_time = time.time() + timeout_s
    last_text = ""

    while time.time() < end_time:
        try:
            last_text = (page.locator(selector).inner_text() or "").strip()
        except Exception:
            last_text = ""

        if any(s.lower() in last_text.lower() for s in expected_substrings):
            return last_text

        time.sleep(poll_s)

    raise TimeoutError(
        f"Timed out waiting for status. Last status text: '{last_text}'"
    )


def _build_api_session(cfg: TestConfig) -> Optional[requests.Session]:
    if not cfg.api.base_url:
        return None

    token = os.getenv(cfg.api.token_env, "").strip()
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})

    return session


def _api_get_json(
    session: requests.Session, base_url: str, path: str
) -> Optional[Dict[str, Any]]:
    url = f"{base_url}{path}"
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        LOGGER.exception("API request failed: %s", url)
        return None


def _db_fetch_one(
    dsn: str, query: str, params: Dict[str, Any]
) -> Optional[Tuple[Any, ...]]:
    with contextlib.suppress(ImportError):
        import psycopg2  # type: ignore

        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()
        finally:
            conn.close()

    LOGGER.warning("psycopg2 is not installed; skipping DB verification")
    return None


class PaymentFlow:
    """UI actions for payment flow. Keep selectors configurable via test_data.json."""

    def __init__(self, page: Page, cfg: TestConfig) -> None:
        self._page = page
        self._cfg = cfg
        self._s = cfg.web.selectors

        _require_selectors(
            self._s,
            keys=[
                "username",
                "password",
                "login_button",
                "payment_method_card",
                "payment_method_wallet",
                "card_number",
                "card_expiry",
                "card_cvv",
                "pay_button",
                "status_text",
                "order_confirmation",
                "error_message",
            ],
        )

    def goto_login(self) -> None:
        self._page.goto(f"{self._cfg.web.base_url}{self._cfg.web.login_path}")

    def login(self) -> None:
        self.goto_login()
        self._page.fill(self._s["username"], self._cfg.user["username"])
        self._page.fill(self._s["password"], self._cfg.user["password"])
        self._page.click(self._s["login_button"])

    def goto_checkout(self) -> None:
        self._page.goto(f"{self._cfg.web.base_url}{self._cfg.web.checkout_path}")

    def open_checkout_authenticated(self) -> None:
        self.login()
        self.goto_checkout()

    def choose_card_payment(self) -> None:
        self._page.click(self._s["payment_method_card"])

    def choose_wallet_payment(self) -> None:
        self._page.click(self._s["payment_method_wallet"])

    def fill_card_details(self, card: Dict[str, str]) -> None:
        self._page.fill(self._s["card_number"], card["number"])
        self._page.fill(self._s["card_expiry"], card["expiry"])
        self._page.fill(self._s["card_cvv"], card["cvv"])

    def submit_payment(self) -> None:
        self._page.click(self._s["pay_button"])

    def wait_for_final_status(self, expected: List[str], timeout_s: int = 90) -> str:
        return _wait_for_status_text(
            page=self._page,
            selector=self._s["status_text"],
            expected_substrings=expected,
            timeout_s=timeout_s,
        )

    def get_error_message(self) -> str:
        try:
            return (self._page.locator(self._s["error_message"]).inner_text() or "").strip()
        except Exception:
            return ""

    def is_order_confirmed(self) -> bool:
        try:
            return self._page.locator(self._s["order_confirmation"]).is_visible()
        except Exception:
            return False


class TestPaymentsE2E:
    """Automation-tagged E2E payment scenarios from the spreadsheet."""

    def test_tc01_card_payment_success(self, page: Page, cfg: TestConfig) -> None:
        raise NotImplementedError

    def test_tc02_card_payment_invalid_details_retry_or_change_method(
        self, page: Page, cfg: TestConfig
    ) -> None:
        raise NotImplementedError

    def test_tc04_gateway_timeout_retry_no_double_charge(
        self, page: Page, cfg: TestConfig
    ) -> None:
        raise NotImplementedError

    def test_tc06_wallet_insufficient_balance_topup_or_alternate_method(
        self, page: Page, cfg: TestConfig
    ) -> None:
        raise NotImplementedError
