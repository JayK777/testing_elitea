"""Playwright (Python) UI test for EPAM website.

Scenario:
- Navigate to https://www.epam.com/
- Select "Services" from the header menu
- Click the "Explore Our Client Work" link
- Verify that the "Client Work" text is visible on the page

Run (example):
  pip install playwright
  playwright install
  python tests/test_epam_client_work.py
"""

from playwright.sync_api import sync_playwright, expect


def run() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # Steps will be added below.
            page.goto("https://www.epam.com/", wait_until="domcontentloaded")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    run()
