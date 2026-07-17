import asyncio
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USERNAME = os.getenv("LOGIN_USERNAME")
PASSWORD = os.getenv("LOGIN_PASSWORD")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        await page.goto(URL)

        await page.locator("//input[@id='LogName']").fill(USERNAME)
        # //label[@for='LogPass'] identifies the password field; the label's
        # "for" attribute points to the actual input id, so fill that input.
        await page.locator("#LogPass").fill(PASSWORD)
        await page.locator("//div[@id='LogButton']").click()

        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
