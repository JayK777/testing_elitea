"""
browser_helper.py
-----------------
Reusable Playwright browser/page utility functions.
Follows Open/Closed and Single Responsibility Principles.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

logger = logging.getLogger(__name__)


@contextmanager
def launch_browser(
    browser_type: str = "chromium",
    headless: bool = True,
    slow_mo: int = 0,
) -> Generator[Browser, None, None]:
    """
    Context manager that launches and tears down a Playwright browser.

    Args:
        browser_type (str): One of 'chromium', 'firefox', 'webkit'.
        headless (bool): Run browser in headless mode.
        slow_mo (int): Milliseconds to slow down operations (useful for debugging).

    Yields:
        Browser: Active Playwright browser instance.
    """
    with sync_playwright() as playwright:
        browser = _get_browser(playwright, browser_type, headless, slow_mo)
        logger.info("Browser '%s' launched (headless=%s).", browser_type, headless)
        try:
            yield browser
        finally:
            browser.close()
            logger.info("Browser closed.")


def _get_browser(
    playwright: Playwright,
    browser_type: str,
    headless: bool,
    slow_mo: int,
) -> Browser:
    """Select and launch the appropriate browser type."""
    launchers = {
        "chromium": playwright.chromium,
        "firefox": playwright.firefox,
        "webkit": playwright.webkit,
    }
    launcher = launchers.get(browser_type)
    if launcher is None:
        raise ValueError(
            f"Unsupported browser type '{browser_type}'. "
            f"Choose from: {list(launchers.keys())}"
        )
    return launcher.launch(headless=headless, slow_mo=slow_mo)


@contextmanager
def new_page(
    browser: Browser,
    storage_state: dict = None,
) -> Generator[Page, None, None]:
    """
    Context manager that creates and tears down a browser page/context.

    Args:
        browser (Browser): Active Playwright browser instance.
        storage_state (dict): Optional storage state for session persistence testing.

    Yields:
        Page: Active Playwright page instance.
    """
    context: BrowserContext = browser.new_context(storage_state=storage_state)
    page: Page = context.new_page()
    logger.info("New browser page created.")
    try:
        yield page
    finally:
        context.close()
        logger.info("Browser context closed.")


def fill_login_form(
    page: Page,
    username: str,
    password: str,
    username_selector: str = "input[name='username'], input[type='email'], #username",
    password_selector: str = "input[name='password'], input[type='password'], #password",
    submit_selector: str = "button[type='submit'], button:has-text('Login'), #login-btn",
) -> None:
    """
    Fill and submit the login form on a given page.

    Args:
        page (Page): Active Playwright page.
        username (str): Username or email value.
        password (str): Password value.
        username_selector (str): CSS selector for the username field.
        password_selector (str): CSS selector for the password field.
        submit_selector (str): CSS selector for the submit button.
    """
    page.fill(username_selector, username)
    page.fill(password_selector, password)
    page.click(submit_selector)
    logger.info("Login form submitted for user: %s", username)


def get_visible_text(page: Page, selector: str, timeout: int = 5000) -> str:
    """
    Wait for an element to be visible and return its inner text.

    Args:
        page (Page): Active Playwright page.
        selector (str): CSS selector.
        timeout (int): Timeout in milliseconds.

    Returns:
        str: Visible inner text of the element.
    """
    element = page.wait_for_selector(selector, state="visible", timeout=timeout)
    return element.inner_text()
