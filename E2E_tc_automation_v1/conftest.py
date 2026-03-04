import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None


LOGGER = logging.getLogger(__name__)


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")

        return _ENV_PATTERN.sub(repl, value)

    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_expand_env(v) for v in value]

    return value


def load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    return _expand_env(raw)


@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str


def _get_pg_config() -> Optional[PgConfig]:
    host = os.environ.get("PG_HOST")
    port = os.environ.get("PG_PORT")
    dbname = os.environ.get("PG_DB")
    user = os.environ.get("PG_USER")
    password = os.environ.get("PG_PASSWORD")

    if not all([host, port, dbname, user, password]):
        return None

    return PgConfig(
        host=host,
        port=int(port),
        dbname=dbname,
        user=user,
        password=password,
    )


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return load_test_data()


@pytest.fixture(scope="session")
def browser() -> Generator[Browser, None, None]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def context(browser: Browser) -> Generator[BrowserContext, None, None]:
    ctx = browser.new_context()
    yield ctx
    ctx.close()


@pytest.fixture()
def page(context: BrowserContext) -> Generator[Page, None, None]:
    pg = context.new_page()
    yield pg
    pg.close()


@pytest.fixture(scope="session")
def pg_conn() -> Generator[Any, None, None]:
    config = _get_pg_config()
    if not config:
        yield None
        return

    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required for PG validations, but is not installed")

    conn = psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )
    try:
        yield conn
    finally:
        conn.close()
