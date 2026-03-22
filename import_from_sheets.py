#!/usr/bin/env python3
"""
Helper script to import data from Google Sheets CSV export into Time Tracker Pro.

Usage:
1. Export your Google Sheet as CSV (File > Download > Comma-separated values)
2. Run this script: python import_from_sheets.py <path_to_csv_file>
3. Or set the API token and endpoint as environment variables and use the API directly
"""

import sys
import os
import requests
import csv
from pathlib import Path

# Configuration
API_ENDPOINT = os.getenv("TIME_TRACKER_URL", "http://localhost:5001")
API_TOKEN = os.getenv("TIME_TRACKER_API_TOKEN", "")

def read_csv_file(file_path):
    """Read CSV file and return as string"""
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()

def import_via_api(csv_content, api_url, api_token):
    """Import CSV data via API"""
    url = f"{api_url}/api/import-csv"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}" if api_token else "",
        "X-API-Token": api_token if api_token else ""
    }
    
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
        return False
    else:
        print(f"❌ Error: {response.status_code}")
        try:
            error_data = response.json()
            print(f"   Message: {error_data.get('message', 'Unknown error')}")
        except:
            print(f"   Response: {response.text}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python import_from_sheets.py <path_to_csv_file>")
        print("\nExample:")
        print("  python import_from_sheets.py ~/Downloads/time_tracker_export.csv")
        print("\nEnvironment variables:")
        print("  TIME_TRACKER_URL - API endpoint (default: http://localhost:5001)")
        print("  TIME_TRACKER_API_TOKEN - API authentication token")
        sys.exit(1)
    
    csv_file = Path(sys.argv[1])
    
    if not csv_file.exists():
        print(f"❌ Error: File not found: {csv_file}")
        sys.exit(1)
    
    print(f"Reading CSV file: {csv_file}")
    try:
        csv_content = read_csv_file(csv_file)
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        sys.exit(1)
    
    print(f"Importing to: {API_ENDPOINT}")
    if not API_TOKEN:
        print("⚠️  Warning: No API token set. Import may fail if authentication is required.")
        print("   Set TIME_TRACKER_API_TOKEN environment variable if needed.")
    
    success = import_via_api(csv_content, API_ENDPOINT, API_TOKEN)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
