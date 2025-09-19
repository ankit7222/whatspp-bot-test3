import os
import json
from flask import Flask, request
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)

# ===================== GOOGLE SHEETS SETUP =====================
SHEET_NAME = os.getenv("SHEET_NAME")

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME).sheet1

# ===================== WHATSAPP API SETUP =====================
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

# ===================== CONVERSATION STATE =====================
user_states = {}

# ===================== HELPER FUNCTIONS =====================
def send_whatsapp_message(to, text, buttons=None):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive" if buttons else "text"
    }
    if buttons:
        data["interactive"] = {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.lower(), "title": b}}
                    for b in buttons
                ]
            }
        }
    else:
        data["text"] = {"body": text}

    requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data)

def get_questions_for_user(listing):
    q = []
    if listing in ["app store", "both"]:
        q.append("Please provide the App Store link (if applicable).")
    if listing in ["play store", "both"]:
        q.append("Please provide the Play Store link (if applicable).")
    q += [
        "What is your last 12 months revenue? (Numbers only)",
        "What is your last 12 months profit? (Numbers only)",
        "What is your last 12 months spends? (Numbers only)",
        "What is your monthly profit? (Numbers only)",
        "What is your Daily Active Users (DAU)? (Numbers only)",
        "What is your Monthly Active Users (MAU)? (Numbers only)"
    ]
    return q

def validate_answer(step, text, user_state):
    listing = user_state["responses"][0].lower() if user_state["responses"] else ""
    current_question = user_state["questions"][step]

    # Validate links
    if "app store link" in current_question.lower():
        if not text.startswith("https://apps.apple.com"):
            return False, "❌ Invalid App Store link. Please provide a valid URL."
    if "play store link" in current_question.lower():
        if not text.startswith("https://play.google.com"):
            return False, "❌ Invalid Play Store link. Please provide a valid URL."

    # Validate numeric
    if any(x in current_question.lower() for x in ["revenue", "profit", "spends", "dau", "mau"]):
        if not text.replace(".", "").isdigit():
            return False, "❌ Please enter a valid number."

    return True, None

def save_to_sheet(user_id, state):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    listing = state["responses"][0].lower()
    index = 1
    app_store_link = ""
    play_store_link = ""

    if listing == "app store":
        app_store_link = state["responses"][index]
        index += 1
    elif listing == "play store":
        play_store_link = state["responses"][index]
        index += 1
    elif listing == "both":
        app_store_link = state["responses"][index]
        index += 1
        play_store_link = state["responses"][index]
        index += 1

    remaining = state["responses"][index:]
    row = [now, user_id, listing, app_store_link, play_store_link] + remaining
    sheet.append_row(row)

    # Highlight monthly profit if >=7000
    try:
        monthly_profit = float(row[8])
        row_number = len(sheet.get_all_values())
        if monthly_profit >= 7000:
            sheet.format(f"I{row_number}", {"backgroundColor": {"red": 0.6, "green": 0.9, "blue": 0.6}})
    except:
        pass

# ===================== WEBHOOK ENDPOINT =====================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Invalid verification token", 403

    data = request.get_json()
    if "entry" in data:
        for entry in data["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if messages:
                    msg = messages[0]
                    user_id = msg["from"]
                    text = msg.get("text", {}).get("body", "").strip()
                    button_reply = msg.get("interactive", {}).get("button_reply", {}).get("id")

                    if user_id not in user_states:
                        if text.lower() in ["hi", "hello"]:
                            send_whatsapp_message(
                                user_id,
                                "Hi, I am Kalagato AI Agent. Are you interested in selling your app?",
                                ["Yes", "No"]
                            )
                            user_states[user_id] = {"step": 0, "responses": []}
                        continue

                    state = user_states[user_id]
                    step = state["step"]

                    # Handle No
                    if button_reply == "no":
                        send_whatsapp_message(user_id, "Thanks, if you have any queries contact us on aman@kalagato.co")
                        del user_states[user_id]
                        continue

                    # Handle Yes
                    if button_reply == "yes" and step == 0:
                        send_whatsapp_message(
                            user_id,
                            "Is your app listed on App Store, Play Store, or Both?",
                            ["App Store", "Play Store", "Both"]
                        )
                        continue

                    # Listing selection
                    if step == 0:
                        listing_answer = button_reply or text
                        state["responses"].append(listing_answer.lower())
                        state["questions"] = get_questions_for_user(listing_answer.lower())
                        state["step"] = 0
                        send_whatsapp_message(user_id, state["questions"][0])
                        continue

                    # Validate
                    valid, error_msg = validate_answer(step, text, state)
                    if not valid:
                        send_whatsapp_message(user_id, error_msg)
                        continue

                    # Save response
                    state["responses"].append(text)
                    state["step"] += 1

                    if state["step"] < len(state["questions"]):
                        send_whatsapp_message(user_id, state["questions"][state["step"]])
                    else:
                        save_to_sheet(user_id, state)
                        send_whatsapp_message(user_id, "✅ Thank you! Your responses have been saved.")
                        del user_states[user_id]

    return "OK", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
