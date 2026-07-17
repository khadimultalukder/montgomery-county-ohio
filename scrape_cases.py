import asyncio
import logging
import os
import random
from urllib.parse import urljoin
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import gspread

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [montgomery] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("montgomery")

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
CALENDAR_URL = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?ZACTION=USER&ZMETHOD=CALENDAR"
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USERNAME = os.getenv("LOGIN_USERNAME")
PASSWORD = os.getenv("LOGIN_PASSWORD")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "MONTGOMERY")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "config/service_account.json")

# field name -> xpath on the case detail page
CASE_FIELDS = {
    "sale_type": "//th[contains(.,'Sale Type')]/following-sibling::td[1]",
    "parcel_id": "//th[contains(.,'Parcel ID')]/following-sibling::td[1]",
    # property address is split across two rows: the street on the
    # "Property Address:" row, and city/state/zip on the very next row --
    # both parts get joined together into one field
    "property_address": [
        "//th[contains(.,'Property Address')]/following-sibling::td[1]",
        "//th[contains(.,'Property Address')]/parent::tr/following-sibling::tr[1]/td[@class='bDat']",
    ],
    "appraised_value": "//th[contains(.,'Appraised Value')]/following-sibling::td[1]",
    "opening_bid": "//th[contains(.,'Opening Bid')]/following-sibling::td[1]",
    "case_status": "//th[contains(.,'Case Status')]/following-sibling::td[1]",
    "defendant": "//div[@class='bDiv']//td[contains(.,'DEFENDANT')]/following-sibling::td[1]",
    "plaintiff": "//div[@class='bDiv']//td[contains(.,'PLAINTIFF')]/following-sibling::td[1]",
    "auction_sold": "//div[@class='ASTAT_MSGB Astat_DATA']",
    "amount": "//div[@class='ASTAT_MSGD Astat_DATA']",
}
SHEET_COLUMNS = ["case_id", "case_url"] + list(CASE_FIELDS.keys())


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


async def safe_text(page, xpath, timeout=3000):
    """Return the inner text of the first match, or '' if it isn't found in time."""
    locator = page.locator(f"xpath={xpath}").first
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        return (await locator.inner_text()).strip()
    except Exception:
        return ""


async def extract_case_details(case_page):
    details = {}
    for field, xpaths in CASE_FIELDS.items():
        xpaths = xpaths if isinstance(xpaths, list) else [xpaths]

        # for a single xpath this is just that one value; for a list, every
        # non-empty part gets joined together (e.g. street + city/state/zip)
        parts = [await safe_text(case_page, xp) for xp in xpaths]
        details[field] = ", ".join(p for p in parts if p)
    return details


def connect_google_sheet():
    """Open the target worksheet using a service account, adding the header row if missing."""
    gc = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sh.worksheet(GOOGLE_SHEET_TAB)

    existing_rows = worksheet.get_all_values()
    if not existing_rows or existing_rows[0] != SHEET_COLUMNS:
        worksheet.insert_row(SHEET_COLUMNS, index=1, value_input_option="USER_ENTERED")

    return worksheet


def get_existing_case_ids(worksheet):
    """Return the set of case_id values already recorded (column A), skipping the header row."""
    values = worksheet.col_values(1)
    return set(values[1:]) if values else set()


async def collect_case_links(page):
    """
    Read every case row on the current calendar page into plain Python values
    (case_id + href) BEFORE opening any tabs. Some rows disappear/reshuffle
    once you start interacting with the page (opening tabs, session refresh,
    etc.), so indexing into a live locator mid-loop is unreliable. Collecting
    everything up front avoids that: once it's a Python list, the DOM
    changing underneath us can't affect it anymore.
    """
    cases = page.locator("xpath=//td[@class='AD_DTA']/a[1]")
    count = await cases.count()

    collected = []
    for i in range(count):
        try:
            link = cases.nth(i)
            case_id = (await link.inner_text(timeout=5000)).strip()
            href = await link.get_attribute("href", timeout=5000)
            if not href:
                logger.warning(f"Case at index {i} has no href, skipping")
                continue
            collected.append({"case_id": case_id, "href": href})
        except Exception as e:
            logger.warning(f"Could not read case at index {i}, skipping: {e}")

    logger.info(f"Collected {len(collected)} of {count} cases on this page")
    return collected


async def scrape_case(page, idx, total, case_id, case_url, worksheet, existing_case_ids):
    """Open a single case in a new tab, extract its details, and write the row."""
    case_page = None
    try:
        case_page = await page.context.new_page()
        await case_page.goto(case_url)
        await human_wait(1.5, 3)

        details = await extract_case_details(case_page)
        row = {"case_id": case_id, "case_url": case_url, **details}

        worksheet.append_row(
            [row.get(col, "") for col in SHEET_COLUMNS],
            value_input_option="USER_ENTERED",
        )
        existing_case_ids.add(case_id)
        logger.info(f"[{idx}/{total}] OK")
    except Exception as e:
        logger.warning(f"[{idx}/{total}] FAILED - case_id={case_id} url={case_url} error={e}")
    finally:
        if case_page is not None and not case_page.is_closed():
            await case_page.close()
        await human_wait(1, 2.5)


async def main():
    worksheet = connect_google_sheet()
    existing_case_ids = get_existing_case_ids(worksheet)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()

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
            logger.info(f"Processing calendar page {page_num}")
            await human_wait(1, 2)

            # Phase 1: collect every case link on this page first.
            case_list = await collect_case_links(page)

            # Phase 2: now scrape them one by one from the static list.
            total = len(case_list)
            for idx, case in enumerate(case_list, start=1):
                case_id = case["case_id"]

                if case_id in existing_case_ids:
                    logger.info(f"[{idx}/{total}] Skipping case {case_id} (already in sheet)")
                    continue

                case_url = urljoin(page.url, case["href"])
                await scrape_case(page, idx, total, case_id, case_url, worksheet, existing_case_ids)

            next_button = page.locator("xpath=//div[@class='BLHeaderNext BLArrow']//a").first
            if await next_button.is_visible():
                await human_wait(0.8, 1.8)
                await next_button.click()
                await human_wait(1.5, 3)
            else:
                logger.info("No more pages.")
                break

            page_num += 1

        input("Press Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
