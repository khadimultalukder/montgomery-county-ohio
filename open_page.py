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


async def click_next_page(page, timeout=3000):
    """Click the calendar's 'next' pagination arrow if it exists and is visible."""
    next_arrow = page.locator("xpath=//div[@class='BLHeaderNext BLArrow']//a").first
    try:
        await next_arrow.wait_for(state="visible", timeout=timeout)
        await next_arrow.click()
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
        await asyncio.sleep(1)
        open_case = page.locator("xpath=//div[@class='CALDAYBOX']//div[@role='link']").first
        await open_case.wait_for(state="visible", timeout=10000)
        await open_case.click()
        await asyncio.sleep(1)

        page_num = 1
        max_pages = 50  # safety cap so it can't loop forever
        while page_num <= max_pages:
            print(f"Processing calendar page {page_num}")

            # TODO: handle the opened case here (extract data, close/back, etc.)

            moved_to_next = await click_next_page(page)
            if not moved_to_next:
                print("No more pages.")
                break

            await asyncio.sleep(1)
            page_num += 1

        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
