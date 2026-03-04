"""UI automation for payment flows (Playwright).

Covers Automation-tagged scenarios:
- TC_01 Card happy path
- TC_02 Wallet happy path
- TC_04 Invalid card number
- TC_05 Expired card
- TC_06 CVV length boundary
- TC_08 Gateway delay/timeout behaviour (pending -> resolved)
- TC_11 Multiple rapid Pay taps do not create duplicates
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    base_url: str
    headless: bool = True
    slow_mo_ms: int = 0
    default_timeout_ms: int = 30_000


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing test data file: {data_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in test data file: {data_path}") from exc


def configure_logging() -> None:
    if logging.getLogger().handlers:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def safe_screenshot(page: Page, name: str) -> Optional[str]:
    """Capture screenshot to repo-local folder. Returns path or None."""
    out_dir = Path(os.getenv("ARTIFACTS_DIR", ".")) / "playwright_artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("_")
    path = out_dir / f"{safe_name}.png"

    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except PlaywrightError:
        return None


def _required_order_id() -> str:
    order_id = os.getenv("ORDER_ID")
    if not order_id:
        pytest.skip("ORDER_ID env var is required for UI payment tests")
    return order_id


class PaymentApp:
    def __init__(self, page: Page, data: Dict[str, Any], settings: Settings):
        self.page = page
        self.data = data
        self.settings = settings

    @property
    def selectors(self) -> Dict[str, str]:
        return self.data["ui"]["selectors"]

    def goto(self, path: str) -> None:
        url = self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")
        self.page.goto(url, wait_until="domcontentloaded")

    def login(self, username: str, password: str) -> None:
        self.goto(self.data["ui"].get("login_path", "/login"))

        self.page.locator(self.selectors["username"]).fill(username)
        self.page.locator(self.selectors["password"]).fill(password)
        self.page.locator(self.selectors["login_submit"]).click()

        self.page.wait_for_load_state("networkidle")

    def open_payment_page(self, order_id: str) -> None:
        template = self.data["ui"].get("payment_path_template", "/orders/{order_id}/payment")
        self.goto(template.format(order_id=order_id))
        self.page.wait_for_load_state("domcontentloaded")

    def select_card(self) -> None:
        self.page.locator(self.selectors["payment_method_card"]).click()

    def select_wallet(self) -> None:
        self.page.locator(self.selectors["payment_method_wallet"]).click()

    def fill_card(self, number: str, expiry: str, cvv: str) -> None:
        self.page.locator(self.selectors["card_number"]).fill(number)
        self.page.locator(self.selectors["card_expiry"]).fill(expiry)
        self.page.locator(self.selectors["card_cvv"]).fill(cvv)

    def submit_payment(self) -> None:
        self.page.locator(self.selectors["pay_button"]).click()

    def status_text(self) -> str:
        return self.page.locator(self.selectors["status_badge"]).inner_text().strip()

    def wait_for_final_status(
        self,
        allowed_final: Tuple[str, ...] = ("PAID", "SUCCESS", "FAILED", "CANCELLED"),
        timeout_s: int = 90,
    ) -> str:
        end = time.time() + timeout_s
        last = ""
        while time.time() < end:
            try:
                last = self.status_text().upper()
            except PlaywrightError:
                time.sleep(0.5)
                continue

            if any(token in last for token in allowed_final):
                return last
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for final payment status. Last='{last}'")


@pytest.fixture(scope="session", autouse=True)
def _logging() -> None:
    configure_logging()


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return load_test_data()


@pytest.fixture(scope="session")
def settings(test_data: Dict[str, Any]) -> Settings:
    base_url = os.getenv("BASE_URL", test_data["ui"]["base_url"])
    return Settings(
        base_url=base_url,
        headless=_bool_env("HEADLESS", True),
        slow_mo_ms=int(os.getenv("SLOW_MO_MS", "0")),
        default_timeout_ms=int(os.getenv("DEFAULT_TIMEOUT_MS", "30000")),
    )


@pytest.fixture(scope="session")
def pw():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="function")
def page(pw, settings: Settings):
    browser = pw.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo_ms)
    context = browser.new_context()
    context.set_default_timeout(settings.default_timeout_ms)
    page = context.new_page()

    try:
        yield page
    finally:
        try:
            context.close()
        finally:
            browser.close()


@pytest.fixture(scope="function")
def app(page: Page, test_data: Dict[str, Any], settings: Settings) -> PaymentApp:
    user = test_data["users"]["default"]
    payment_app = PaymentApp(page=page, data=test_data, settings=settings)
    payment_app.login(user["username"], user["password"])
    return payment_app

# <AUTOGEN:TESTS>
