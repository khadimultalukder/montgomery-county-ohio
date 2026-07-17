import asyncio
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
CALENDAR_URL = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?ZACTION=USER&ZMETHOD=CALENDAR"
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
        await asyncio.sleep(1)

        await click_ok_if_present(page)
        await asyncio.sleep(0.5)
        await click_ok_if_present(page)
        await asyncio.sleep(1)

        await page.goto(CALENDAR_URL)
        await asyncio.sleep(1)

        open_case = page.locator("xpath=//div[@class='CALDAYBOX']//div[@role='link']").first
        await open_case.wait_for(state="visible", timeout=10000)
        await open_case.click()
        await asyncio.sleep(1)

        page_num = 1
        while page_num <= 50:
            print(f"Processing calendar page {page_num}")
            await asyncio.sleep(1)

            cases = page.locator("xpath=//td[@class='AD_DTA']/a[1]")
            count = await cases.count()
            print(f"Found {count} cases")

            for i in range(count):
                case_link = cases.nth(i)
                case_id = (await case_link.inner_text()).strip()
                print(f"Opening case: {case_id}")

                async with page.context.expect_page() as new_page_info:
                    await case_link.click()
                case_page = await new_page_info.value
                await case_page.wait_for_load_state()
                await asyncio.sleep(1)

                # TODO: extract case details from case_page here

                await case_page.close()
                await asyncio.sleep(1)

            next_button = page.locator("xpath=//div[@class='BLHeaderNext BLArrow']//a").first
            if await next_button.is_visible():
                await next_button.click()
                await asyncio.sleep(1)
            else:
                print("No more pages.")
                break

            page_num += 1

        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
