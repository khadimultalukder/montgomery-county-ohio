# Montgomery County Sheriff Sale Auction Scraper

Logs into the Montgomery County (Ohio) sheriff sale auction site, walks the auction calendar, opens each case, and writes the extracted details to a Google Sheet.

## Files

- `montgomery_scrape_cases.py` — main script. Collects all case links on a calendar page first, then scrapes each one individually (avoids issues with rows shifting/hiding mid-scrape).
- `.env` — configuration (login, sheet target). Not committed with real secrets — fill in your own values.
- `requirements.txt` — Python dependencies.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Fill in `.env`:
   ```
   TARGET_URL=https://montgomery.sheriffsaleauction.ohio.gov/index.cfm
   HEADLESS=false
   LOGIN_USERNAME=your_username_here
   LOGIN_PASSWORD=your_password_here

   GOOGLE_SHEET_ID=your_sheet_id
   GOOGLE_SHEET_TAB=MONTGOMERY
   GOOGLE_SHEET_TOTAL_TAB=Total Auctions
   GOOGLE_SERVICE_ACCOUNT_FILE=config/service_account.json
   ```

   Every scraped row is written to both `GOOGLE_SHEET_TAB` (the per-county tab) and `GOOGLE_SHEET_TOTAL_TAB` (a combined tab across all counties). Both tabs are created automatically if they don't already exist.

3. Google Sheets access:
   - Create a Google Cloud service account with the Sheets API enabled.
   - Download its JSON key and place it at the path set in `GOOGLE_SERVICE_ACCOUNT_FILE` (e.g. `config/service_account.json`).
   - Open the target Google Sheet, click **Share**, and add the service account's `client_email` (found inside the JSON key) as an **Editor**. Without this step the script will fail with a permission error.

## Running

```
python montgomery_scrape_cases.py
```

The script will:
- Log in (typing credentials with human-like delays, dismissing confirmation popups).
- Go to the auction calendar and open the first day with listings.
- On each calendar page: collect every case's ID and link, then for each case:
  - if it's already in the sheet **and** `auction_sold` has a value, skip it (considered resolved/final),
  - if it's already in the sheet but `auction_sold` is still blank, re-scrape it and update that row in place,
  - otherwise scrape it and append a new row.
- Move to the next calendar page and repeat until there are no more pages.
- Pause with `Press Enter to close the browser...` at the end so you can review the final state before it closes.

## Sheet columns

`case_id`, `case_url`, `auction_date`, `sale_type`, `parcel_id`, `property_address`, `appraised_value`, `opening_bid`, `case_status`, `defendant`, `plaintiff`, `auction_sold`, `amount`

`property_address` combines the street line and the city/state/zip line from the case page into one field.

## Notes

- `HEADLESS=false` is recommended while testing so you can see what's happening; set to `true` for unattended runs.
- The script logs to the console in the format `TIMESTAMP [montgomery] LEVEL: message`. Per-case progress prints as `[n/total] OK` or `[n/total] FAILED - ...` with the reason.
- Re-running the script is safe and resumable — it uses `case_id` + `auction_sold` to decide whether a case is finished (skip), still pending (re-scrape and update in place), or new (append).
