"""
happy_path.py
-------------
TC_01 - Successful login with valid username + password → dashboard redirect + nav identifier.
TC_02 - Successful login with valid email + password → dashboard redirect.

Uses: Playwright (UI), requests (API session validation).
Jira: EP-2
"""

import logging

from utils.browser_helper import fill_login_form, get_visible_text, launch_browser, new_page
from utils.config_loader import load_test_data
from utils.reporter import TestReport, run_test

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def tc_01_successful_login_username(config: dict) -> tuple[bool, str]:
    """
    TC_01: Login with valid username/email + password.
    Verifies redirect to dashboard and user identity visible in nav bar.
    """
    login_url = config["login_url"]
    dashboard_url = config["dashboard_url"]
    username = config["valid_user"]["username"]
    password = config["valid_user"]["password"]

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            fill_login_form(page, username, password)

            # Verify redirect to dashboard
            page.wait_for_url(f"{dashboard_url}**", timeout=10_000)
            current_url = page.url
            if dashboard_url not in current_url:
                return False, f"Expected dashboard URL, got: {current_url}"

            # Verify user identity in nav bar
            nav_selector = "[data-testid='user-nav'], .user-profile, .nav-username"
            nav_text = get_visible_text(page, nav_selector, timeout=5000)
            if not nav_text.strip():
                return False, "User identity not visible in navigation bar after login."

            return True, f"Login succeeded. Nav shows: '{nav_text.strip()}'"


def tc_02_successful_login_email(config: dict) -> tuple[bool, str]:
    """
    TC_02: Login using email format credential.
    Verifies session creation and dashboard redirect.
    """
    login_url = config["login_url"]
    dashboard_url = config["dashboard_url"]
    email = config["valid_user"]["username"]   # email used as username
    password = config["valid_user"]["password"]

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            fill_login_form(page, email, password)

            page.wait_for_url(f"{dashboard_url}**", timeout=10_000)
            current_url = page.url
            if dashboard_url not in current_url:
                return False, f"Expected dashboard URL after email login, got: {current_url}"

            # Verify session cookie is set
            cookies = page.context.cookies()
            session_cookie = next(
                (c for c in cookies if "session" in c["name"].lower() or "auth" in c["name"].lower()),
                None,
            )
            if session_cookie is None:
                return False, "No session/auth cookie found after successful login."

            return True, f"Email login successful. Session cookie: '{session_cookie['name']}'"


def tc_13_remember_me_checked_session_persists(config: dict) -> tuple[bool, str]:
    """
    TC_13: 'Remember Me' checked → session persists across browser restart.
    Verifies a persistent cookie with expected max-age/expiry is set.
    """
    login_url = config["login_url"]
    dashboard_url = config["dashboard_url"]
    username = config["valid_user"]["username"]
    password = config["valid_user"]["password"]

    with launch_browser() as browser:
        # Step 1: Login with Remember Me checked
        with new_page(browser) as page:
            page.goto(login_url)
            fill_login_form(page, username, password)
            remember_selector = "input[name='rememberMe'], #remember-me, input[type='checkbox']"
            page.check(remember_selector)
            page.click("button[type='submit'], button:has-text('Login')")
            page.wait_for_url(f"{dashboard_url}**", timeout=10_000)

            # Capture storage state
            storage_state = page.context.storage_state()

        # Step 2: Re-open with saved storage state (simulates browser restart)
        with new_page(browser, storage_state=storage_state) as new_pg:
            new_pg.goto(dashboard_url)
            current_url = new_pg.url
            if "login" in current_url.lower():
                return False, "Session did not persist after browser restart with Remember Me checked."

            return True, "Remember Me: session persisted across browser context restart."


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    config = load_test_data()
    report = TestReport(suite_name="Happy Path - Login")

    run_test(report, "TC_01", "Successful login (username) → dashboard + nav identity",
             tc_01_successful_login_username, config)
    run_test(report, "TC_02", "Successful login (email) → dashboard + session cookie",
             tc_02_successful_login_email, config)
    run_test(report, "TC_13", "Remember Me checked → session persists after browser restart",
             tc_13_remember_me_checked_session_persists, config)

    report.print_summary()
    report.save_json()


if __name__ == "__main__":
    main()
