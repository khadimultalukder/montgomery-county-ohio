import asyncio
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

# --- Config ---------------------------------------------------------------
URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
CALENDAR_URL = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?ZACTION=USER&ZMETHOD=CALENDAR"
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USERNAME = os.getenv("LOGIN_USERNAME")
PASSWORD = os.getenv("LOGIN_PASSWORD")
MAX_PAGES = 50  # safety cap so the pagination loop can't run forever

# --- Selectors --------------------------------------------------------------
SEL_USERNAME = "//input[@id='LogName']"
SEL_PASSWORD = "#LogPass"
SEL_LOGIN_BUTTON = "//div[@id='LogButton']"
SEL_OK_BUTTON = "xpath=//input[@value='OK']"
SEL_OPEN_CASE = "xpath=//div[@class='CALDAYBOX']//div[@role='link']"
SEL_NEXT_PAGE = "xpath=//div[@class='BLHeaderNext BLArrow']//a"


# --- Helpers ----------------------------------------------------------------
async def click_if_present(page, selector, timeout=3000):
    """Click the first matching element if it becomes visible in time."""
    element = page.locator(selector).first
    try:
        await element.wait_for(state="visible", timeout=timeout)
        await element.click()
        return True
    except Exception:
        return False


async def login(page):
    print("Logging in...")
    await page.goto(URL)
    await page.locator(SEL_USERNAME).fill(USERNAME)
    await page.locator(SEL_PASSWORD).fill(PASSWORD)
    await page.locator(SEL_LOGIN_BUTTON).click()

    # dismiss up to two confirmation popups that may appear after submit
    await click_if_present(page, SEL_OK_BUTTON)
    await click_if_present(page, SEL_OK_BUTTON)


async def open_first_case(page):
    open_case = page.locator(SEL_OPEN_CASE).first
    await open_case.wait_for(state="visible", timeout=10000)
    await open_case.click()
    await asyncio.sleep(1)


async def process_calendar(page):
    print("Opening calendar...")
    await page.goto(CALENDAR_URL)
    await open_first_case(page)



    page_num = 1
    while page_num <= MAX_PAGES:
        print(f"Processing calendar page {page_num}")


        cases = page.locator("xpath=//td[@class='AD_DTA']/a[1]")
        count = await cases.count()
        print(f"Found {count} cases")


        if not await click_if_present(page, SEL_NEXT_PAGE):
            print("No more pages.")
            break

        page_num += 1


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        try:
            await login(page)
            await process_calendar(page)
            input("Press Enter to close the browser...")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
