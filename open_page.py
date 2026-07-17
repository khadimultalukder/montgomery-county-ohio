import asyncio
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USERNAME = os.getenv("LOGIN_USERNAME")
PASSWORD = os.getenv("LOGIN_PASSWORD")

async def click_ok_if_present(page, timeout=3000):
    ok_button = page.locator("xpath=//input[@value='OK']").first
    try:
        await ok_button.wait_for(state="visible", timeout=timeout)
        await ok_button.click()
        return True
    except Exception:
        return False


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        await page.goto(URL)
        await asyncio.sleep(1)
        await page.locator("//input[@id='LogName']").fill(USERNAME)
        await asyncio.sleep(0.5)
        await page.locator("#LogPass").fill(PASSWORD)
        await asyncio.sleep(0.5)
        await page.locator("//div[@id='LogButton']").click()
        await asyncio.sleep(0.5)

        # first popup (e.g. right after submit)
        await click_ok_if_present(page)
        # second popup (only if a separate one appears after the first click)
        await click_ok_if_present(page)
        await asyncio.sleep(1)

        await page.goto("https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?ZACTION=USER&ZMETHOD=CALENDAR")



        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
