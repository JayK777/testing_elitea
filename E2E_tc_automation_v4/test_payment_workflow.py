"""E2E payment workflow automation (Playwright + Requests + optional PostgreSQL).

How to run (example):
  python -m pip install pytest playwright requests psycopg2-binary
  playwright install
  pytest -q E2E_tc_automation_v4/test_payment_workflow.py

Configuration:
  - Edit E2E_tc_automation_v4/test_data.json
  - Or override via environment variables described in `EnvConfig`.

Notes:
  - This file intentionally contains all reusable helpers (no extra utils) to
    comply with the 2-file constraint.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest
import requests
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

try:
    import psycopg2  # type: ignore
except ImportError:  # pragma: no cover
    psycopg2 = None


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvConfig:
    """Environment overrides (keep secrets out of source control)."""

    web_base_url: str = os.getenv("WEB_BASE_URL", "")
    api_base_url: str = os.getenv("API_BASE_URL", "")
    api_token: str = os.getenv("API_TOKEN", "")
    db_dsn: str = os.getenv("PG_DSN", "")


class TestDataError(RuntimeError):
    """Raised when test data is missing or invalid."""


def _load_test_data(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        raise TestDataError(f"Missing test data file: {file_path}")

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TestDataError(f"Invalid JSON in test data file: {file_path}") from exc


def _required(dct: Dict[str, Any], key: str) -> Any:
    if key not in dct or dct[key] in (None, ""):
        raise TestDataError(f"Missing required test data key: {key}")
    return dct[key]


def _now_ms() -> int:
    return int(time.time() * 1000)


class PaymentApiClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    def get_order_status(self, endpoint_template: str, order_id: str) -> Dict[str, Any]:
        url = f"{self._base_url}{endpoint_template.format(order_id=order_id)}"
        try:
            response = self._session.get(url, timeout=self._timeout_s)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise AssertionError(f"API request failed: GET {url}: {exc}") from exc


class PostgresClient:
    def __init__(self, dsn: str) -> None:
        if psycopg2 is None:
            raise RuntimeError(
                "psycopg2 is not installed. Install psycopg2-binary or disable DB checks."
            )
        self._dsn = dsn

    def fetch_value(self, query: str, params: Iterable[Any]) -> Any:
        try:
            with psycopg2.connect(self._dsn) as conn:  # type: ignore[misc]
                with conn.cursor() as cur:
                    cur.execute(query, tuple(params))
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"DB query failed: {exc}") from exc


class CheckoutPage:
    """Minimal page-object style wrapper using selectors from test_data.json."""

    def __init__(self, page: Page, selectors: Dict[str, str], timeouts: Dict[str, Any]):
        self._page = page
        self._sel = selectors
        self._timeouts = timeouts

    @property
    def page(self) -> Page:
        return self._page

    def goto_checkout(self, base_url: str) -> None:
        url = f"{base_url.rstrip('/')}{_required(self._sel, 'checkout_path')}"
        self._page.goto(url, wait_until="domcontentloaded")

    def login_if_needed(self, base_url: str, username: str, password: str) -> None:
        """Optional login flow. If already logged in, it will no-op."""

        login_path = self._sel.get("login_path")
        if not login_path:
            return

        self._page.goto(f"{base_url.rstrip('/')}{login_path}", wait_until="domcontentloaded")

        user_sel = self._sel.get("login_username")
        pass_sel = self._sel.get("login_password")
        submit_sel = self._sel.get("login_submit")

        if not (user_sel and pass_sel and submit_sel):
            raise TestDataError("Login selectors are incomplete in test_data.json")

        self._page.fill(user_sel, username)
        self._page.fill(pass_sel, password)
        self._page.click(submit_sel)

        logged_in_sel = self._sel.get("logged_in_marker")
        if logged_in_sel:
            self._page.wait_for_selector(logged_in_sel, timeout=self._timeouts.get("ui_ms", 20000))

    def select_payment_method(self, method_key: str) -> None:
        method_selector = _required(self._sel, f"payment_{method_key}")
        self._page.click(method_selector)

    def click_pay(self) -> None:
        self._page.click(_required(self._sel, "pay_button"))

    def wait_for_status_text(self, status_regex: str) -> None:
        status_selector = _required(self._sel, "payment_status")
        timeout_ms = int(self._timeouts.get("payment_ms", 60000))
        self._page.wait_for_function(
            "(sel, reStr) => {"
            "  const el = document.querySelector(sel);"
            "  if (!el) return false;"
            "  const re = new RegExp(reStr, 'i');"
            "  return re.test(el.textContent || '');"
            "}",
            arg=[status_selector, status_regex],
            timeout=timeout_ms,
        )

    def extract_order_id(self) -> str:
        order_selector = self._sel.get("order_id")
        if not order_selector:
            raise TestDataError("Missing selector 'order_id' in test_data.json")

        txt = self._page.text_content(order_selector) or ""
        match = re.search(r"(\d+)", txt)
        if not match:
            raise AssertionError(f"Unable to extract order id from text: {txt!r}")
        return match.group(1)


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    data_file = Path(__file__).with_name("test_data.json")
    return _load_test_data(data_file)


@pytest.fixture(scope="session")
def env_config() -> EnvConfig:
    return EnvConfig()


@pytest.fixture(scope="session")
def api_client(test_data: Dict[str, Any], env_config: EnvConfig) -> Optional[PaymentApiClient]:
    api_cfg = test_data.get("api", {})
    base_url = env_config.api_base_url or api_cfg.get("base_url", "")
    token = env_config.api_token or api_cfg.get("token", "")

    if not base_url:
        LOGGER.warning("API base_url not set; API assertions will be skipped.")
        return None

    return PaymentApiClient(base_url=base_url, token=token, timeout_s=float(api_cfg.get("timeout_s", 20)))


@pytest.fixture(scope="session")
def db_client(test_data: Dict[str, Any], env_config: EnvConfig) -> Optional[PostgresClient]:
    db_cfg = test_data.get("db", {})
    enabled = bool(db_cfg.get("enabled", False))
    dsn = env_config.db_dsn or db_cfg.get("dsn", "")

    if not enabled:
        return None

    if not dsn:
        raise TestDataError("DB enabled but DSN is missing (set PG_DSN or db.dsn)")

    return PostgresClient(dsn=dsn)


@pytest.fixture()
def browser_page(test_data: Dict[str, Any]) -> Page:
    web_cfg = test_data.get("web", {})
    headless = bool(web_cfg.get("headless", True))
    slow_mo_ms = int(web_cfg.get("slow_mo_ms", 0))

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright=playwright, headless=headless, slow_mo_ms=slow_mo_ms)
        context = browser.new_context(ignore_https_errors=bool(web_cfg.get("ignore_https_errors", False)))
        page = context.new_page()

        try:
            yield page
        except Exception:  # noqa: BLE001
            _capture_failure_artifacts(page)
            raise
        finally:
            context.close()
            browser.close()


def _launch_browser(playwright: Playwright, headless: bool, slow_mo_ms: int) -> Browser:
    browser_name = os.getenv("BROWSER", "chromium").lower().strip()
    browser_type = {
        "chromium": playwright.chromium,
        "firefox": playwright.firefox,
        "webkit": playwright.webkit,
    }.get(browser_name)

    if browser_type is None:
        raise TestDataError(f"Unsupported BROWSER={browser_name!r}. Use chromium|firefox|webkit")

    return browser_type.launch(headless=headless, slow_mo=slow_mo_ms)


def _capture_failure_artifacts(page: Page) -> None:
    try:
        ts = _now_ms()
        page.screenshot(path=f"playwright_failure_{ts}.png", full_page=True)
        html = page.content()
        Path(f"playwright_failure_{ts}.html").write_text(html, encoding="utf-8")
        LOGGER.exception("Captured failure artifacts: screenshot + HTML")
    except Exception:  # noqa: BLE001
        LOGGER.exception("Failed to capture Playwright failure artifacts")
