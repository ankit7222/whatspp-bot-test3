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

questions = [
    "Please provide the App Store link.",
    "Please provide the Play Store link.",
    "What is your last 12 months revenue? (Numbers only)",
    "What is your last 12 months profit? (Numbers only)",
    "What is your last 12 months spends? (Numbers only)",
    "What is your monthly profit? (Numbers only)",
    "What is your Daily Active Users (DAU)? (Numbers only)",
    "What is your Monthly Active Users (MAU)? (Numbers only)"
]

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

def validate_answer(step, text, user_state):
    """Validate user responses based on step and listing selection"""
    listing = user_state["responses"][0].lower() if user_state["responses"] else ""

    # Step 1: App Store link
    if step == 1 and ("app store" in listing or "both" in listing):
        if not text.startswith("https://apps.apple.com"):
            return False, "❌ Invalid App Store link. Please provide a valid URL."

    # Step 2: Play Store link
    if step == 2 and ("play store" in listing or "both" in listing):
        if not text.startswith("https://play.google.com"):
            return False, "❌ Invalid Play Store link. Please provide a valid URL."

    # Numeric validations
    if step >= 3 and not text.replace(".", "").isdigit():
        return False, "❌ Please enter a valid number."

    return True, None

def save_to_sheet(user_id, responses):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    listing = responses[0].lower()

    # Initialize link columns
    app_store_link = ""
    play_store_link = ""

    # Fill links based on selection
    index = 1  # responses index after listing
    if listing == "app store":
        app_store_link = responses[index]
        index += 1
    elif listing == "play store":
        play_store_link = responses[index]
        index += 1
    elif listing == "both":
        app_store_link = responses[index]
        index += 1
        play_store_link = responses[index]
        index += 1

    # Remaining responses: revenue, profit, spends, monthly profit, DAU, MAU
    remaining = responses[index:]

    # Final row to append
    row = [now, user_id, listing, app_store_link, play_store_link] + remaining
    sheet.append_row(row)

    # Highlight monthly profit if >= 7000 (Monthly Profit = index 8)
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
                        if text.lower() in ["hi", "hello","good morning","good evening","hy","hey"]:
                            send_whatsapp_message(
                                user_id,
                                "Hi, I am KalaGato AI Agent. Are you interested in selling your app?",
                                ["Yes", "No"]
                            )
                            user_states[user_id] = {"step": 0, "responses": []}
                        continue

                    state = user_states[user_id]
                    step = state["step"]

                    # Handle "No"
                    if button_reply == "no":
                        send_whatsapp_message(user_id, "Thanks, if you have any queries contact us on aman@kalagato.co")
                        del user_states[user_id]
                        continue

                    # Handle "Yes"
                    if button_reply == "yes" and step == 0:
                        state["step"] = 0
                        send_whatsapp_message(
                            user_id,
                            "Is your app listed on App Store, Play Store, or Both?",
                            ["App Store", "Play Store", "Both"]
                        )
                        continue

                    # Listing answer
                    if step == 0:
                        listing_answer = button_reply or text
                        state["responses"].append(listing_answer)
                        if listing_answer.lower() == "app store":
                            state["step"] = 1
                            send_whatsapp_message(user_id, questions[0])
                        elif listing_answer.lower() == "play store":
                            state["step"] = 2
                            send_whatsapp_message(user_id, questions[1])
                        elif listing_answer.lower() == "both":
                            state["step"] = 1
                            send_whatsapp_message(user_id, questions[0])
                        continue

                    # Validate responses
                    valid, error_msg = validate_answer(step, text, state)
                    if not valid:
                        send_whatsapp_message(user_id, error_msg)
                        continue

                    # Save valid response
                    state["responses"].append(text)

                    # Move to next question
                    state["step"] += 1
                    if state["step"] < len(questions):
                        send_whatsapp_message(user_id, questions[state["step"]])
                    else:
                        save_to_sheet(user_id, state["responses"])
                        send_whatsapp_message(user_id, "✅ Thank you! Your responses have been saved in our database we will cotact with you ASAP")
                        del user_states[user_id]

    return "OK", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
