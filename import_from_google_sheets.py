#!/usr/bin/env python3
"""
Import data directly from Google Sheets CSV export URL.

This bypasses Sheety API and imports directly from Google Sheets.

Usage:
1. Get your Google Sheet's CSV export URL:
   - Open your Google Sheet
   - File > Share > Publish to web
   - Choose "Comma-separated values (.csv)" format
   - Copy the URL

2. Run this script:
   python import_from_google_sheets.py <google_sheets_csv_url>

Example:
   python import_from_google_sheets.py "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/export?format=csv&gid=0"
"""

import sys
import os
import requests
from pathlib import Path

# Configuration
API_ENDPOINT = os.getenv("TIME_TRACKER_URL", "http://localhost:5001")
API_TOKEN = os.getenv("TIME_TRACKER_API_TOKEN", "")

def fetch_csv_from_url(url):
    """Fetch CSV content from a URL"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        print(f"❌ Error fetching CSV from URL: {e}")
        return None

def import_via_api(csv_content, api_url, api_token):
    """Import CSV data via API"""
    url = f"{api_url}/api/import-csv"
    headers = {
        "Content-Type": "application/json",
    }
    
    # Try both authentication methods
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-API-Token"] = api_token
    
    response = requests.post(
        url,
        json={"csv_data": csv_content},
        headers=headers,
        timeout=30
    )
    
    if response.status_code == 200:
        result = response.json()
        print(f"✅ Success: {result.get('message', 'Import completed')}")
        print(f"   Imported {result.get('count', 0)} tasks")
        return True
    elif response.status_code == 401:
        print("❌ Error: Unauthorized. Please set TIME_TRACKER_API_TOKEN environment variable.")
        print("   Example: export TIME_TRACKER_API_TOKEN='your-token-here'")
        print(f"   Response: {response.text}")
        return False
    else:
        print(f"❌ Error: HTTP {response.status_code}")
        try:
            error_data = response.json()
            print(f"   Message: {error_data.get('message', 'Unknown error')}")
        except:
            print(f"   Response: {response.text}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python import_from_google_sheets.py <google_sheets_csv_url>")
        print("\nTo get your Google Sheet CSV URL:")
        print("1. Open your Google Sheet")
        print("2. File > Share > Publish to web")
        print("3. Choose 'Comma-separated values (.csv)'")
        print("4. Copy the URL and use it here")
        print("\nExample:")
        print('  python import_from_google_sheets.py "https://docs.google.com/spreadsheets/d/.../export?format=csv"')
        print("\nEnvironment variables:")
        print("  TIME_TRACKER_URL - API endpoint (default: http://localhost:5001)")
        print("  TIME_TRACKER_API_TOKEN - API authentication token")
        sys.exit(1)
    
    csv_url = sys.argv[1]
    
    print(f"Fetching CSV from: {csv_url}")
    csv_content = fetch_csv_from_url(csv_url)
    
    if not csv_content:
        sys.exit(1)
    
    print(f"✅ Fetched {len(csv_content)} characters of CSV data")
    print(f"Importing to: {API_ENDPOINT}")
    
    if not API_TOKEN:
        print("⚠️  Warning: No API token set. Import may fail if authentication is required.")
        print("   Set TIME_TRACKER_API_TOKEN environment variable if needed.")
    
    success = import_via_api(csv_content, API_ENDPOINT, API_TOKEN)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
