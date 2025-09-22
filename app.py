import os
import json
import re
from flask import Flask, request, jsonify
from google.oauth2.service_account import Credentials
import gspread

app = Flask(__name__)

# Load Google Sheets credentials from environment variable
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_SHEETS_CREDENTIALS"))

# Define the scope for Google Sheets
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(creds)

# Google Sheet details
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Orders")
sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

def is_valid_link(value):
    return value.startswith("http://") or value.startswith("https://")

def is_numeric(value):
    return re.match(r'^\d+(\.\d+)?$', str(value)) is not None

@app.route("/")
def home():
    return "✅ Google Sheets Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()

        # Extract values
        playstore_link = data.get("playstore_link", "")
        appstore_link = data.get("appstore_link", "")
        iap_revenue = data.get("iap_revenue", "No")
        subscription_revenue = data.get("subscription_revenue", "No")
        ad_revenue = data.get("ad_revenue", "No")
        last_12m_revenue = data.get("last_12m_revenue", "")
        last_12m_spend = data.get("last_12m_spend", "")
        last_12m_profit = data.get("last_12m_profit", "")
        monthly_profit_avg = data.get("monthly_profit_avg", "")

        # ✅ Validations
        if playstore_link and not is_valid_link(playstore_link):
            return jsonify({"error": "❌ Invalid Playstore link"}), 400

        if appstore_link and not is_valid_link(appstore_link):
            return jsonify({"error": "❌ Invalid Appstore link"}), 400

        if last_12m_revenue and not is_numeric(last_12m_revenue):
            return jsonify({"error": "❌ Last 12M Revenue must be a number"}), 400

        if last_12m_spend and not is_numeric(last_12m_spend):
            return jsonify({"error": "❌ Last 12M Spend must be a number"}), 400

        if last_12m_profit and not is_numeric(last_12m_profit):
            return jsonify({"error": "❌ Last 12M Profit must be a number"}), 400

        if monthly_profit_avg and not is_numeric(monthly_profit_avg):
            return jsonify({"error": "❌ Monthly Profit Average must be a number"}), 400

        # Append row in Google Sheet
        sheet.append_row([
            playstore_link,
            appstore_link,
            iap_revenue,
            subscription_revenue,
            ad_revenue,
            last_12m_revenue,
            last_12m_spend,
            last_12m_profit,
            monthly_profit_avg
        ])

        return jsonify({"message": "✅ Data saved successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(port=5000, debug=True)
