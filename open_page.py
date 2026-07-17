import asyncio
import os
import random
from urllib.parse import urljoin
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
CALENDAR_URL = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?ZACTION=USER&ZMETHOD=CALENDAR"
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USERNAME = os.getenv("LOGIN_USERNAME")
PASSWORD = os.getenv("LOGIN_PASSWORD")


async def human_wait(min_sec=1.0, max_sec=2.5):
    """Random pause so actions don't fire at robotic, fixed intervals."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def human_type(locator, text):
    """Type like a person, one key at a time with a random delay per key."""
    await locator.click()
    for ch in text:
        await locator.type(ch, delay=random.randint(80, 220))
    await asyncio.sleep(random.uniform(0.2, 0.6))


async def click_ok_if_present(page, timeout=3000):
    ok_button = page.locator("xpath=//input[@value='OK']").first
    try:
        await ok_button.wait_for(state="visible", timeout=timeout)
        await human_wait(0.3, 0.8)
        await ok_button.click()
        return True
    except Exception:
        return False


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()

        await page.goto(URL)
        await human_wait(1.5, 3)

        await human_type(page.locator("//input[@id='LogName']"), USERNAME)
        await human_wait(0.5, 1.2)
        await human_type(page.locator("#LogPass"), PASSWORD)
        await human_wait(0.5, 1.2)
        await page.locator("//div[@id='LogButton']").click()
        await human_wait(1.5, 3)

        await click_ok_if_present(page)
        await human_wait(0.5, 1)
        await click_ok_if_present(page)
        await human_wait(1, 2)

        await page.goto(CALENDAR_URL)
        await human_wait(1.5, 3)

        open_case = page.locator("xpath=//div[@class='CALDAYBOX']//div[@role='link']").first
        await open_case.wait_for(state="visible", timeout=10000)
        await human_wait(0.5, 1.2)
        await open_case.click()
        await human_wait(1, 2)

        page_num = 1
        while page_num <= 50:
            print(f"Processing calendar page {page_num}")
            await human_wait(1, 2)

            cases = page.locator("xpath=//td[@class='AD_DTA']/a[1]")
            count = await cases.count()
            print(f"Found {count} cases")

            for i in range(count):
                case_link = cases.nth(i)
                case_id = (await case_link.inner_text()).strip()
                case_href = await case_link.get_attribute("href")
                case_url = urljoin(page.url, case_href)
                print(f"Opening case: {case_id}")

                await human_wait(0.5, 1.5)
                case_page = await page.context.new_page()
                await case_page.goto(case_url)
                await human_wait(1.5, 3)

                # TODO: extract case details from case_page here

                await case_page.close()
                await human_wait(1, 2.5)

            next_button = page.locator("xpath=//div[@class='BLHeaderNext BLArrow']//a").first
            if await next_button.is_visible():
                await human_wait(0.8, 1.8)
                await next_button.click()
                await human_wait(1.5, 3)
            else:
                print("No more pages.")
                break

            page_num += 1

        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
