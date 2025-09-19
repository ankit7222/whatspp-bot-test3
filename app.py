import os
import json
from google.oauth2.service_account import Credentials
import gspread
from flask import Flask, request

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# -----------------------------
# Setup Google Sheets Credentials
# -----------------------------
if os.path.exists("service_account.json"):
    # Local development: use JSON file
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
else:
    # Production: use environment variable
    google_sheets_env = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
    if not google_sheets_env:
        raise Exception("GOOGLE_SHEETS_CREDENTIALS environment variable not found!")
    try:
        service_account_info = json.loads(google_sheets_env)
    except json.JSONDecodeError as e:
        raise Exception(f"Invalid JSON in GOOGLE_SHEETS_CREDENTIALS: {e}")
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)

# Authorize gspread client
gc = gspread.authorize(creds)

# -----------------------------
# Setup your spreadsheet
# -----------------------------
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "<YOUR_SPREADSHEET_ID>")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

try:
    sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
except Exception as e:
    raise Exception(f"Could not open spreadsheet: {e}")

# -----------------------------
# Flask routes (example)
# -----------------------------
@app.route("/")
def home():
    return "Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    # Your bot logic here
    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
