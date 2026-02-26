// Playwright test: EPAM example scenario
import { chromium } from 'playwright';

async function runTest() {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext();
    const page = await context.newPage();

    // Navigate to https://www.epam.com/
    await page.goto('https://www.epam.com/');

    // Select "Services" from the header menu
    // Using a text selector to click the Services menu item
    await page.click('text=Services');

    // Click the "Explore Our Client Work" link.
    await page.click('text=Explore Our Client Work');

    // Verify that the "Client Work" text is visible on the page
    await page.waitForSelector('text=Client Work', { timeout: 5000 });
    const visible = await page.isVisible('text=Client Work');
    if (!visible) {
      throw new Error('\"Client Work\" text not visible');
    }
    console.log('Test passed: "Client Work" text is visible');
  } finally {
    if (browser) await browser.close();
  }
}

runTest().catch(e => { console.error(e); process.exit(1); });
