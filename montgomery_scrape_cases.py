import asyncio
import logging
import os
import random
from datetime import date
from urllib.parse import urljoin
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import gspread
import re

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [montgomery] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("montgomery-scrape_cases")

URL = os.getenv("TARGET_URL", "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm")
CALENDAR_URL = "https://montgomery.sheriffsaleauction.ohio.gov/index.cfm?ZACTION=USER&ZMETHOD=CALENDAR"
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USERNAME = os.getenv("LOGIN_USERNAME")
PASSWORD = os.getenv("LOGIN_PASSWORD")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Montgomery Auction")
GOOGLE_SHEET_TOTAL_TAB = os.getenv("GOOGLE_SHEET_TOTAL_TAB", "Total Auctions")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "config/service_account.json")

# field name -> xpath on the case detail page
CASE_FIELDS = {
    "sale_type": "//th[contains(.,'Sale Type')]/following-sibling::td[1]",
    "parcel_id": "//th[contains(.,'Parcel ID')]/following-sibling::td[1]",
    "street_address": [
        "//th[contains(.,'Property Address')]/following-sibling::td[1]",
    ],
    "appraised_value": "//th[contains(.,'Appraised Value')]/following-sibling::td[1]",
    "opening_bid": "//th[contains(.,'Opening Bid')]/following-sibling::td[1]",
    "case_status": "//th[contains(.,'Case Status')]/following-sibling::td[1]",
    "defendant": "//div[@class='bDiv']//td[contains(.,'DEFENDANT')]/following-sibling::td[1]",
    "plaintiff": "//div[@class='bDiv']//td[contains(.,'PLAINTIFF')]/following-sibling::td[1]",
    "auction_sold": "//div[@class='ASTAT_MSGB Astat_DATA']",
    "amount": "//div[@class='ASTAT_MSGD Astat_DATA']",
}
ROW_ADDRESS_XPATH = "//th[contains(.,'Property Address')]/parent::tr/following-sibling::tr[1]/td[@class='bDat']"
SHEET_COLUMNS = ["case_id", "case_url", "auction_date"] + list(CASE_FIELDS.keys()) + ["city", "state", "zip", "scraped_date"]


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


async def locator_text(locator, timeout=2000):
    """Return the inner text of an already-scoped locator, or '' if it isn't there in time."""
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        return (await locator.inner_text()).strip()
    except Exception:
        return ""


async def safe_text(page, xpath, timeout=3000):
    """Return the inner text of the first match for an xpath on `page`, or '' if not found in time."""
    return await locator_text(page.locator(f"xpath={xpath}").first, timeout=timeout)

def split_city_state_zip(addr):
    if not addr:
        return "", "", ""
    cleaned = re.sub(r"\s+", " ", addr.strip())
    m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d+)$", cleaned)
    if not m:
        logger.warning(f"Could not parse city/state/zip from address: {addr!r}")
        return "", "", ""
    city, state, zip_code = m.groups()
    return city, state.upper(), zip_code[:5]


async def extract_case_details(case_page):
    details = {}
    for field, xpaths in CASE_FIELDS.items():
        xpaths = xpaths if isinstance(xpaths, list) else [xpaths]
        parts = [await safe_text(case_page, xp) for xp in xpaths]
        value = ", ".join(p for p in parts if p)
        if field == "defendant":
            value = value.replace(" , et al.", "")
        details[field] = value

    row_address = await safe_text(case_page, ROW_ADDRESS_XPATH)
    details["city"], details["state"], details["zip"] = split_city_state_zip(row_address)
    return details

def connect_google_sheet(tab_name):
    """
    Open the given worksheet tab using a service account, creating the tab
    if it doesn't exist yet and adding the header row if missing. Used for
    both the per-county tab (GOOGLE_SHEET_TAB) and the combined
    GOOGLE_SHEET_TOTAL_TAB tab -- every scraped row gets written to both.
    """
    gc = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        worksheet = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=tab_name, rows=1000, cols=len(SHEET_COLUMNS))

    existing_rows = worksheet.get_all_values()
    if not existing_rows or existing_rows[0] != SHEET_COLUMNS:
        worksheet.insert_row(SHEET_COLUMNS, index=1, value_input_option="USER_ENTERED")

    return worksheet


def get_existing_rows(worksheet):
    """
    Return (recorded_keys, latest_auction_sold, next_row):
      - recorded_keys: a set of (case_id, auction_sold) pairs already present
        anywhere in the sheet (skipping the header) -- the authoritative
        post-scrape dedup check (see scrape_case)
      - latest_auction_sold: {case_id: most recently recorded auction_sold}
        -- used for the cheap pre-scrape filter (see should_skip_without_scraping)
      - next_row: the first empty row number, for appending new cases

    Used to resume a run and avoid duplicates: a row only gets written when
    its exact (case_id, auction_sold) pair -- both read from the case's own
    detail page -- isn't already recorded. If auction_sold changes (e.g. it
    finally shows a sold result), that's a new pair and gets appended as a
    fresh row, keeping a history instead of overwriting.
    """
    all_values = worksheet.get_all_values()
    if not all_values:
        return set(), {}, 2

    header = all_values[0]
    case_id_idx = header.index("case_id") if "case_id" in header else 0
    auction_sold_idx = header.index("auction_sold") if "auction_sold" in header else None

    recorded_keys = set()
    latest_auction_sold = {}
    for row in all_values[1:]:
        case_id = row[case_id_idx] if case_id_idx < len(row) else ""
        if not case_id:
            continue
        auction_sold = ""
        if auction_sold_idx is not None and auction_sold_idx < len(row):
            auction_sold = row[auction_sold_idx]
        recorded_keys.add((case_id, auction_sold))
        # rows are appended in scrape order, so the last occurrence wins
        latest_auction_sold[case_id] = auction_sold

    return recorded_keys, latest_auction_sold, len(all_values) + 1


async def collect_case_links(page):
    """
    Read every case row on the current calendar page into plain Python values
    (case_id + href + the calendar's own status widget text) BEFORE opening
    any tabs. Some rows disappear/reshuffle once you start interacting with
    the page (opening tabs, session refresh, etc.), so indexing into a live
    locator mid-loop is unreliable. Collecting everything up front avoids
    that: once it's a Python list, the DOM changing underneath us can't
    affect it anymore.

    calendar_status matches the case detail page's auction_sold field
    exactly (same text), so it's used as a cheap pre-scrape comparison in
    should_skip_without_scraping to avoid opening a tab when nothing's
    changed.
    """
    cases = page.locator("xpath=//div[@class='AUCTION_ITEM']")
    count = await cases.count()

    collected = []
    for i in range(count):
        try:
            item = cases.nth(i)
            case_ele = item.locator("xpath=.//td[@class='AD_DTA']/a[1]")
            status_ele = item.locator("xpath=.//div[@class='ASTAT_MSGB Astat_DATA']")

            case_id = (await case_ele.inner_text(timeout=5000)).strip()
            href = await case_ele.get_attribute("href", timeout=5000)
            if not href:
                logger.warning(f"Case at index {i} has no href, skipping")
                continue

            calendar_status = await locator_text(status_ele, timeout=1500)

            collected.append({"case_id": case_id, "href": href, "calendar_status": calendar_status})
        except Exception as e:
            logger.warning(f"Could not read case at index {i}, skipping: {e}")

    logger.info(f"Collected {len(collected)} of {count} cases on this page")
    return collected


async def get_case_list_max_pages(page):
    """Read maxCA -- the total number of case-list sub-pages for the current calendar day."""
    text = await safe_text(page, "//span[@id='maxCA']", timeout=2000)
    try:
        return int(text)
    except (TypeError, ValueError):
        return 1


async def click_case_list_next_page(page, timeout=3000):
    """Click the case-list's own pagination arrow (separate from the day-to-day calendar arrow)."""
    next_arrow = page.locator(
        "xpath=//div[@class='Head_C']//div[@class='PageFrame'][1]//span[@class='PageRight']"
    ).first
    try:
        await next_arrow.wait_for(state="visible", timeout=timeout)
        await next_arrow.click()
        await asyncio.sleep(6)
        return True
    except Exception:
        return False


async def collect_all_cases_for_day(page):
    """
    The case list for a single calendar day can itself be paginated
    (maxCA > 1), separate from the day-to-day calendar pagination. Walk
    every sub-page here and return the combined list of cases, so callers
    always get every case for the day in one call.
    """
    all_cases = []
    page_num = 1

    while True:
        all_cases.extend(await collect_case_links(page))

        max_pages = await get_case_list_max_pages(page)
        if max_pages <= 1 or page_num >= max_pages:
            # maxCA == 1 means there's only one page of cases -- nothing to paginate
            break

        if not await click_case_list_next_page(page):
            logger.warning("Case list pagination arrow not found, stopping early")
            break

        await human_wait(1, 2)
        page_num += 1

    return all_cases


async def append_row_with_retry(worksheet, row_values, attempts=3, base_delay=2):
    """
    Append a row to the sheet, retrying with backoff on transient failures
    (e.g. sheets.googleapis.com read timeouts). Re-raises the last error if
    every attempt fails, so the caller's existing error handling still logs
    it as a genuine failure.
    """
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            worksheet.append_row(row_values, value_input_option="USER_ENTERED")
            return
        except Exception as e:
            if attempt == attempts:
                raise
            logger.warning(f"Sheet write failed (attempt {attempt}/{attempts}): {e} -- retrying in {delay}s")
            await asyncio.sleep(delay)
            delay *= 2


def should_skip_without_scraping(case, latest_auction_sold):
    """
    Cheap pre-filter to avoid opening a tab for a case we already know about
    and that hasn't changed. calendar_status (read off the calendar row) has
    been confirmed to match the detail page's auction_sold value exactly, so
    a direct equality check here is reliable: if this case_id is already
    recorded and the calendar's current text matches what we last recorded
    for it, there's nothing new -- skip without opening a tab.

    Anything new (unseen case_id) or anything that's changed on the
    calendar still gets a real scrape, and scrape_case's own
    (case_id, auction_sold) check is the authoritative dedup before writing.
    """
    case_id = case["case_id"]
    calendar_status = case.get("calendar_status", "")
    return case_id in latest_auction_sold and calendar_status == latest_auction_sold[case_id]


async def scrape_case(page, idx, total, case_id, case_url, auction_date, worksheet, total_worksheet, recorded_keys, latest_auction_sold, sheet_state):
    """
    Open a single case in a new tab and extract its details. The row is only
    written if this exact (case_id, auction_sold) pair isn't already in the
    sheet -- the authoritative dedup check, done after scraping using the
    detail page's own value. If auction_sold has genuinely changed since
    the last time this case was recorded, it's appended as a new row,
    keeping a history instead of overwriting.
    """
    case_page = None
    try:
        case_page = await page.context.new_page()
        await case_page.goto(case_url)
        await human_wait(1.5, 3)

        details = await extract_case_details(case_page)
        row = {"case_id": case_id, "case_url": case_url, "auction_date": auction_date, **details,
               "scraped_date": date.today().isoformat()}
        auction_sold = row.get("auction_sold", "")
        key = (case_id, auction_sold)

        if key in recorded_keys:
            logger.info(f"[{idx}/{total}] Skipping case {case_id} (auction_sold unchanged: {auction_sold!r})")
            return

        row_values = [row.get(col, "") for col in SHEET_COLUMNS]
        await append_row_with_retry(worksheet, row_values)
        await append_row_with_retry(total_worksheet, row_values)
        sheet_state["next_row"] += 1

        # remember this pair (and the latest value for this case_id) so a
        # duplicate encounter later in this same run -- or the cheap
        # pre-filter above -- can recognize it
        recorded_keys.add(key)
        latest_auction_sold[case_id] = auction_sold
        logger.info(f"[{idx}/{total}] OK")
    except Exception as e:
        logger.warning(f"[{idx}/{total}] FAILED - case_id={case_id} url={case_url} error={e}")
    finally:
        if case_page is not None and not case_page.is_closed():
            await case_page.close()
        await human_wait(1, 2.5)


async def main():
    worksheet = connect_google_sheet(GOOGLE_SHEET_TAB)
    total_worksheet = connect_google_sheet(GOOGLE_SHEET_TOTAL_TAB)
    recorded_keys, latest_auction_sold, next_row = get_existing_rows(worksheet)
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

            # Phase 1: collect every case link (case_id + href + calendar_status)
            # for this calendar day, walking the case list's own pagination.
            case_list = await collect_all_cases_for_day(page)

            # Phase 2: cheaply skip cases we already know about that have
            # nothing new on the calendar -- no tab needed for those.
            cases_to_scrape = []
            for case in case_list:
                if should_skip_without_scraping(case, latest_auction_sold):
                    logger.info(f"Skipping case {case['case_id']} (auction_sold unchanged: {case.get('calendar_status', '')!r})")
                    continue
                cases_to_scrape.append(case)

            # Phase 3: scrape the rest; scrape_case itself decides whether to
            # write based on (case_id, auction_sold) already being recorded.
            total = len(cases_to_scrape)
            for idx, case in enumerate(cases_to_scrape, start=1):
                case_id = case["case_id"]
                case_url = urljoin(page.url, case["href"])
                await scrape_case(
                    page, idx, total, case_id, case_url, auctions_date,
                    worksheet, total_worksheet, recorded_keys, latest_auction_sold, sheet_state,
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
