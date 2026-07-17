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
SHEET_COLUMNS = ["case_id", "case_url", "auction_date"] + list(CASE_FIELDS.keys())


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
    xpaths = [
        "xpath=//input[@value='OK']",
        "xpath=//input[@value='Ok']",
    ]
    for xp in xpaths:
        ok_button = page.locator(xp).first
        try:
            await ok_button.wait_for(state="visible", timeout=timeout)
            await human_wait(0.3, 0.8)
            await ok_button.click()
            return True
        except Exception:
            continue
    return False


async def safe_text(page, xpath, timeout=3000):
    """Return the inner text of the first match, or '' if it isn't found in time."""
    locator = page.locator(f"xpath={xpath}").first
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        return (await locator.inner_text()).strip()
    except Exception:
        return ""


async def locator_text(locator, timeout=2000):
    """Return the inner text of an already-scoped locator, or '' if it isn't there in time."""
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


def get_existing_rows(worksheet):
    """
    Return (case_rows, next_row):
      - case_rows: {case_id: {"row": sheet_row_number, "auction_sold": value}}
        for every existing row (skipping the header)
      - next_row: the first empty row number, for appending new cases

    Used to resume a run: a case is only treated as "done" once it has an
    auction_sold value recorded. If a case is already in the sheet but
    auction_sold is still blank (the sale hadn't happened yet last time we
    scraped it), it gets re-scraped and that row is updated in place instead
    of skipping it or adding a duplicate row.
    """
    all_values = worksheet.get_all_values()
    if not all_values:
        return {}, 2

    header = all_values[0]
    case_id_idx = header.index("case_id") if "case_id" in header else 0
    auction_sold_idx = header.index("auction_sold") if "auction_sold" in header else None

    case_rows = {}
    for row_num, row in enumerate(all_values[1:], start=2):
        case_id = row[case_id_idx] if case_id_idx < len(row) else ""
        if not case_id:
            continue
        auction_sold = ""
        if auction_sold_idx is not None and auction_sold_idx < len(row):
            auction_sold = row[auction_sold_idx]
        case_rows[case_id] = {"row": row_num, "auction_sold": auction_sold}

    return case_rows, len(all_values) + 1


async def collect_case_links(page):
    """
    Read every case row on the current calendar page into plain Python values
    (case_id + href + the auction_sold status shown right on the calendar)
    BEFORE opening any tabs. Some rows disappear/reshuffle once you start
    interacting with the page (opening tabs, session refresh, etc.), so
    indexing into a live locator mid-loop is unreliable. Collecting
    everything up front avoids that: once it's a Python list, the DOM
    changing underneath us can't affect it anymore.
    """
    cases = page.locator("xpath=//div[@class='AUCTION_ITEM']")
    count = await cases.count()

    collected = []
    for i in range(count):
        try:
            item = cases.nth(i)
            case_ele = item.locator("xpath=.//td[@class='AD_DTA']/a[1]")
            auction_sold_ele = item.locator("xpath=.//div[@class='ASTAT_MSGB Astat_DATA']")

            case_id = (await case_ele.inner_text(timeout=5000)).strip()
            href = await case_ele.get_attribute("href", timeout=5000)
            if not href:
                logger.warning(f"Case at index {i} has no href, skipping")
                continue

            # the calendar row itself already shows a sold/canceled status
            # once the auction has happened -- grab it so we can skip
            # already-resolved cases without even opening a tab
            auction_sold = await locator_text(auction_sold_ele, timeout=1500)

            collected.append({"case_id": case_id, "href": href, "auction_sold": auction_sold})
        except Exception as e:
            logger.warning(f"Could not read case at index {i}, skipping: {e}")

    logger.info(f"Collected {len(collected)} of {count} cases on this page")
    return collected


def match_cases_with_sheet(case_list, case_rows):
    """
    Compare the freshly collected calendar cases (case_id + auction_sold_ele)
    against what's already recorded in the sheet (case_id + auction_sold),
    and return only the cases that still need to be scraped.

    A case is skipped ONLY when both values are available (non-empty) AND
    they match exactly -- meaning nothing has changed since the last scrape.
    If the case isn't in the sheet yet, or either value is blank, or the
    text differs (e.g. a reschedule adds a second date), it still needs to
    be scraped so the row gets created/updated with the new information.
    """
    to_scrape = []
    for case in case_list:
        case_id = case["case_id"]
        calendar_auction_sold = case.get("auction_sold", "")
        existing = case_rows.get(case_id)
        sheet_auction_sold = existing["auction_sold"] if existing else ""

        if existing and calendar_auction_sold and sheet_auction_sold and calendar_auction_sold == sheet_auction_sold:
            logger.info(f"Skipping case {case_id} (auction_sold unchanged: {calendar_auction_sold})")
            continue

        to_scrape.append(case)

    return to_scrape


async def scrape_case(page, idx, total, case_id, case_url, auction_date, worksheet, case_rows, sheet_state):
    """
    Open a single case in a new tab, extract its details, and write the row.
    If this case_id already has a row (previously scraped with auction_sold
    still blank), update that row in place; otherwise append a new one.
    """
    case_page = None
    try:
        case_page = await page.context.new_page()
        await case_page.goto(case_url)
        await human_wait(1.5, 3)

        details = await extract_case_details(case_page)
        row = {"case_id": case_id, "case_url": case_url, "auction_date": auction_date, **details}
        row_values = [row.get(col, "") for col in SHEET_COLUMNS]

        existing = case_rows.get(case_id)
        if existing:
            target_row = existing["row"]
            worksheet.update(
                range_name=f"A{target_row}",
                values=[row_values],
                value_input_option="USER_ENTERED",
            )
        else:
            target_row = sheet_state["next_row"]
            worksheet.append_row(row_values, value_input_option="USER_ENTERED")
            sheet_state["next_row"] += 1

        case_rows[case_id] = {"row": target_row, "auction_sold": row.get("auction_sold", "")}
        logger.info(f"[{idx}/{total}] OK")
    except Exception as e:
        logger.warning(f"[{idx}/{total}] FAILED - case_id={case_id} url={case_url} error={e}")
    finally:
        if case_page is not None and not case_page.is_closed():
            await case_page.close()
        await human_wait(1, 2.5)


async def main():
    worksheet = connect_google_sheet()
    case_rows, next_row = get_existing_rows(worksheet)
    sheet_state = {"next_row": next_row}

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


        while True:
            date_element = page.locator("xpath=//div[@class='BLHeaderDateDisplay']")
            auctions_date = (await date_element.inner_text()).strip()

            logger.info(f"Processing calendar page {auctions_date}")
            await human_wait(1, 2)

            # Phase 1: collect every case link (case_id + auction_sold_ele) on this page first.
            case_list = await collect_case_links(page)

            # Phase 2: match those against the sheet to find what's left to do.
            cases_to_scrape = match_cases_with_sheet(case_list, case_rows)

            # Phase 3: scrape only the remaining cases, one by one.
            total = len(cases_to_scrape)
            for idx, case in enumerate(cases_to_scrape, start=1):
                case_id = case["case_id"]
                case_url = urljoin(page.url, case["href"])
                await scrape_case(
                    page, idx, total, case_id, case_url, auctions_date,
                    worksheet, case_rows, sheet_state,
                )

            next_button = page.locator("xpath=//div[@class='BLHeaderNext BLArrow']//a").first
            if await next_button.is_visible():
                await human_wait(0.8, 1.8)
                await next_button.click()
                await human_wait(1.5, 3)
            else:
                logger.info("No more pages.")
                break

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
