from flask import Flask, request
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open("Kalagato Leads").sheet1  # Create this sheet in your Google Drive

# Store user states
user_states = {}

questions = [
    "What is your app name?",
    "Please share your app link (Play Store / App Store).",
    "What was your revenue in the last 12 months?",
    "What was your profit in the last 12 months?",
    "What were your spends in the last 12 months?",
    "How many Daily Active Users (DAU) do you have?",
    "How many Monthly Active Users (MAU) do you have?",
    "What is your Day 1 retention (%)?",
    "What is your Day 7 retention (%)?",
    "What is your Day 30 retention (%)?"
]

# WhatsApp API setup
WHATSAPP_API_URL = "https://graph.facebook.com/v20.0/YOUR_PHONE_NUMBER_ID/messages"
TOKEN = "YOUR_META_ACCESS_TOKEN"

def send_whatsapp_message(to, text, buttons=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to}
    
    if buttons:
        payload["type"] = "interactive"
        payload["interactive"] = {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons]}
        }
    else:
        payload["type"] = "text"
        payload["text"] = {"body": text}
    
    requests.post(WHATSAPP_API_URL, headers=headers, json=payload)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if "messages" in data["entry"][0]["changes"][0]["value"]:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
        sender = msg["from"]
        text = msg.get("text", {}).get("body", "").strip()
        button_reply = msg.get("interactive", {}).get("button_reply", {}).get("id")

        # New user greeting
        if sender not in user_states:
            if text.lower() in ["hi", "hello"]:
                user_states[sender] = {"stage": "ask_interest", "answers": []}
                send_whatsapp_message(
                    sender,
                    "Hi, I am Kalagato AI Agent. Are you interested in selling your app?",
                    buttons=[{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}]
                )

        elif user_states[sender]["stage"] == "ask_interest":
            if button_reply == "yes":
                user_states[sender]["stage"] = 0
                send_whatsapp_message(sender, questions[0])
            elif button_reply == "no":
                send_whatsapp_message(sender, "If you have any queries, contact us at aman@kalagato.co")
                user_states.pop(sender)

        elif isinstance(user_states[sender]["stage"], int):
            q_index = user_states[sender]["stage"]
            user_states[sender]["answers"].append(text)

            if q_index + 1 < len(questions):
                user_states[sender]["stage"] += 1
                send_whatsapp_message(sender, questions[q_index + 1])
            else:
                # Save responses to Google Sheets
                row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sender] + user_states[sender]["answers"]
                sheet.append_row(row)

                send_whatsapp_message(sender, "✅ Thank you! Your responses have been recorded.")
                user_states.pop(sender)

    return "OK", 200

@app.route("/", methods=["GET"])
def home():
    return "Kalagato WhatsApp Bot Running ✅"