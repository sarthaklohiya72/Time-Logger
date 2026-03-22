# How to Import Your Google Sheets Data

Since Sheety API is returning 402 Payment Required errors, you need to import your data directly from Google Sheets.

## Option 1: Import via Google Sheets CSV URL (Recommended)

1. **Get your Google Sheet's CSV export URL:**
   - Open your Google Sheet in a browser
   - Click **File** → **Share** → **Publish to web**
   - In the dialog, select:
     - **Link**: "Entire document" or "Sheet1" (your sheet name)
     - **Format**: "Comma-separated values (.csv)"
   - Click **Publish**
   - Copy the URL that appears (it will look like: `https://docs.google.com/spreadsheets/d/YOUR_ID/export?format=csv&gid=0`)

2. **Set your Railway app URL and API token:**
   ```bash
   export TIME_TRACKER_URL="https://your-railway-app.railway.app"
   export TIME_TRACKER_API_TOKEN="your-api-token-here"
   ```

3. **Run the import script:**
   ```bash
   python import_from_google_sheets.py "YOUR_CSV_URL_HERE"
   ```

## Option 2: Manual CSV Export and Import

1. **Export your Google Sheet:**
   - Open your Google Sheet
   - Click **File** → **Download** → **Comma-separated values (.csv)**
   - Save the file to your computer

2. **Set your Railway app URL and API token:**
   ```bash
   export TIME_TRACKER_URL="https://your-railway-app.railway.app"
   export TIME_TRACKER_API_TOKEN="your-api-token-here"
   ```

3. **Run the import script:**
   ```bash
   python import_from_sheets.py ~/Downloads/your_sheet.csv
   ```

## Option 3: Using curl (if you have the CSV content)

```bash
curl -X POST https://your-railway-app.railway.app/api/import-csv \
  -H "Content-Type: application/json" \
  -H "X-API-Token: your-api-token" \
  -d '{"csv_data": "your-csv-content-here"}'
```

## Finding Your API Token

Your API token should be set in Railway's environment variables as `TIME_TRACKER_API_TOKEN`. If you haven't set it yet:

1. Go to your Railway project
2. Click on your service
3. Go to **Variables** tab
4. Add `TIME_TRACKER_API_TOKEN` with a secure random value (you can generate one with: `openssl rand -hex 32`)

## Troubleshooting

- **401 Unauthorized**: Make sure your `TIME_TRACKER_API_TOKEN` is set correctly in both Railway and your local environment
- **No data imported**: Check that your CSV has the correct columns (Logged Time, Raw Start, Raw Task)
- **Import fails**: Check the error message - it will tell you what went wrong

## After Import

Once imported, refresh your dashboard and your data should appear! The app will continue to work with the local database even if Sheety sync fails.
