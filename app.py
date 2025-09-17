import os
import json
import requests
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

# ==========================
# Google Sheets Setup
# ==========================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Load credentials JSON from Render env variable
creds_json = os.getenv("GOOGLE_CREDS_JSON")
creds_dict = json.loads(creds_json)

creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
client = gspread.authorize(creds)

# Open your Google Sheet
sheet = client.open("Whatsapp_bot_AK").worksheet("Sheet1")

# ==========================
# WhatsApp API Setup
# ==========================
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
TOKEN = os.getenv("META_ACCESS_TOKEN")
WHATSAPP_API_URL = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# ==========================
# Conversation Flow
# ==========================
questions = [
    "What is your app name?",
    "Please share your app link.",
    "What is your last 12 months revenue?",
    "What is your last 12 months profit?",
    "What is your last 12 months spends?",
    "What are your Daily Active Users (DAU)?",
    "What are your Monthly Active Users (MAU)?",
    "What is your Retention Day 1?",
    "What is your Retention Day 7?",
    "What is your Retention Day 30?"
]

user_sessions = {}

# ==========================
# Helper Functions
# ==========================
def send_message(to, text, buttons=None):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive" if buttons else "text",
    }
    if buttons:
        data["interactive"] = {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": str(i), "title": btn}}
                    for i, btn in enumerate(buttons)
                ]
            },
        }
    else:
        data["text"] = {"body": text}

    requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data)

def save_to_sheet(user_id, responses):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [now, user_id] + responses
    sheet.append_row(row)

# ==========================
# Webhook Routes
# ==========================
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == "kalagato123":  # must match your Verify Token in Meta
        return challenge
    return "Unauthorized", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if data.get("entry"):
        for entry in data["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])

                for message in messages:
                    user_id = message["from"]
                    text = None

                    if "text" in message:
                        text = message["text"]["body"].strip().lower()
                    elif message.get("interactive"):
                        text = message["interactive"]["button_reply"]["title"].lower()

                    # Start flow
                    if text in ["hi", "hello"]:
                        send_message(
                            user_id,
                            "Hi, I am Kalagato AI agent. Are you interested in selling your app?",
                            ["Yes", "No"]
                        )
                        user_sessions[user_id] = {"step": -1, "responses": []}

                    elif text == "yes":
                        user_sessions[user_id] = {"step": 0, "responses": []}
                        send_message(user_id, questions[0])

                    elif text == "no":
                        send_message(
                            user_id,
                            "Thanks! If you have any queries, contact us at aman@kalagato.co"
                        )
                        user_sessions.pop(user_id, None)

                    elif user_id in user_sessions:
                        session = user_sessions[user_id]
                        step = session["step"]

                        if 0 <= step < len(questions):
                            session["responses"].append(message["text"]["body"])
                            session["step"] += 1

                            if session["step"] < len(questions):
                                send_message(user_id, questions[session["step"]])
                            else:
                                # Save all responses to Google Sheet
                                save_to_sheet(user_id, session["responses"])
                                send_message(user_id, "Thank you! Your responses have been saved.")
                                user_sessions.pop(user_id, None)

    return "ok", 200

# ==========================
# Run App
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
