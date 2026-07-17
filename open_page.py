import asyncio
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()
        await page.goto(URL)
        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
