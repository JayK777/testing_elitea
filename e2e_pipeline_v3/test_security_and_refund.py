"""Automation scenarios (tagged A) derived from pipeline_testcase.xlsx.

Coverage in this module:
- TC_12 Secure transmission: HTTPS & no sensitive data in URL/query params
- TC_13 Refund requested for an already refunded/cancelled order (negative)

Notes:
- TC_12 uses Playwright request event capture and pattern matching.
- TC_13 is implemented as an API-level negative test using `requests`.
  If API endpoints or a refunded order id are not configured, it will be skipped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests
from playwright.sync_api import Page, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    endpoints: Dict[str, str]


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


@pytest.fixture(scope="session")
def api_config(test_data: Dict[str, Any]) -> ApiConfig:
    api = _require(test_data, "api")
    return ApiConfig(base_url=_require(api, "base_url"), endpoints=_require(api, "endpoints"))
