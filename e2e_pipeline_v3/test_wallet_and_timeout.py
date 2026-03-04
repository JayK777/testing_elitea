"""Automation scenarios (tagged A) derived from pipeline_testcase.xlsx.

Coverage in this module:
- TC_06 Digital wallet payment fails due to insufficient wallet balance
- TC_08 Payment gateway timeout while processing payment

Notes:
- Timeout simulation is environment-dependent. If `web.network_simulation.timeout_url_pattern`
  is not configured, the timeout test will be skipped.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from playwright.sync_api import Page, Route, sync_playwright


LOGGER = logging.getLogger(__name__)


def _load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    return json.loads(data_path.read_text(encoding="utf-8"))


def _require(data: Dict[str, Any], dotted_key: str) -> Any:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"Missing required config key: {dotted_key}")
        cur = cur[part]
    return cur


def _maybe_get(data: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    try:
        return _require(data, dotted_key)
    except KeyError:
        return default


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return _load_test_data()


@pytest.fixture()
def page(test_data: Dict[str, Any]) -> Page:
    web = _require(test_data, "web")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(int(_maybe_get(web, "timeouts_ms.action", 10000)))
        yield page
        context.close()
        browser.close()


def _safe_screenshot(page: Page, name: str) -> Optional[str]:
    try:
        path = Path(__file__).with_name(f"{name}.png")
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        LOGGER.exception("Failed to take screenshot")
        return None
