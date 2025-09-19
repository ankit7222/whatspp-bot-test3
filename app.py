import os
import json
import requests
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)

# --- Google Sheets setup ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_SHEETS_CREDENTIALS"))
creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)

gc = gspread.authorize(creds)
service = build("sheets", "v4", credentials=creds)

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

# --- WhatsApp setup ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"


# --- Helper: send WhatsApp message ---
def send_message(to, message, buttons=None):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    if buttons:
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": message},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": str(i), "title": btn}}
                        for i, btn in enumerate(buttons, 1)
                    ]
                },
            },
        }
    else:
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": {"body": message}},
        }

    requests.post(WHATSAPP_API_URL, headers=headers, json=data)


# --- Conditional Formatting ---
def get_column_index(column_name):
    """Find column index (0-based) by header name."""
    header = sheet.row_values(1)  # first row is header
    if column_name in header:
        return header.index(column_name)
    else:
        raise ValueError(f"Column '{column_name}' not found in sheet")


def apply_conditional_formatting():
    """Color full row green/red based on Monthly Profit."""
    try:
        profit_col_index = get_column_index("Monthly Profit")  # dynamic lookup
        end_col_index = len(sheet.row_values(1))  # auto-adjust to header count

        body = {
            "requests": [
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": 0,
                                    "startRowIndex": 1,  # skip header
                                    "startColumnIndex": 0,
                                    "endColumnIndex": end_col_index,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [
                                        {
                                            "userEnteredValue": f"=${chr(65+profit_col_index)}2>=7000"
                                        }
                                    ],
                                },
                                "format": {
                                    "backgroundColor": {
                                        "red": 0.8,
                                        "green": 1,
                                        "blue": 0.8,
                                    }
                                },
                            },
                        },
                        "index": 0,
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": 0,
                                    "startRowIndex": 1,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": end_col_index,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [
                                        {
                                            "userEnteredValue": f"=${chr(65+profit_col_index)}2<7000"
                                        }
                                    ],
                                },
                                "format": {
                                    "backgroundColor": {
                                        "red": 1,
                                        "green": 0.8,
                                        "blue": 0.8,
                                    }
                                },
                            },
                        },
                        "index": 1,
                    }
                },
            ]
        }

        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body=body
        ).execute()
        print("✅ Conditional formatting applied successfully")

    except Exception as e:
        print(f"⚠️ Error applying conditional formatting: {e}")


# --- Webhook ---
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Invalid verification token"
    return "OK", 200


# --- Main ---
if __name__ == "__main__":
    apply_conditional_formatting()  # Run once at startup
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
