import os
import json
import re
import requests
import gspread
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# ===================== FLASK APP =====================
app = Flask(__name__)

# ===================== ENV VARIABLES =====================
GOOGLE_CREDENTIALS = json.loads(os.getenv("GOOGLE_SHEETS_CREDENTIALS"))
SHEET_NAME = os.getenv("SHEET_NAME")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# ===================== GOOGLE SHEETS SETUP =====================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(GOOGLE_CREDENTIALS, scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME).sheet1

# ===================== CONSTANTS =====================
SESSION_TIMEOUT = timedelta(minutes=15)
WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

# ===================== STATE MEMORY =====================
user_states = {}
processed_msg_ids = set()

# ===================== QUESTIONS =====================
QUESTIONS = [
    {"key": "name", "text": "What is your name?", "type": "text"},
    {"key": "app_link", "text": "Please provide the App Store or Play Store link (https://...)", "type": "link"},
    {"key": "revenue", "text": "What is your annual revenue (USD)?", "type": "number"},
    {"key": "marketing_cost", "text": "What is your annual marketing cost (USD)?", "type": "number"},
    {"key": "server_cost", "text": "What is your annual server cost (USD)?", "type": "number"},
    {"key": "profit", "text": "What is your annual profit (USD)?", "type": "number"},
    {"key": "revenue_type", "text": "Which revenue sources? Reply with numbers separated by comma:\n1. IAP\n2. Subscription\n3. Ad (e.g., '1,3')", "type": "text"},
    {"key": "email", "text": "Your email address (we’ll send valuation there)", "type": "email"}
]

# ===================== WHATSAPP HELPERS =====================
def send_whatsapp_text(to, text):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data)

def send_whatsapp_buttons(to, text, buttons):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": b.lower(), "title": b}} for b in buttons]
            }
        }
    }
    requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data)

# ===================== VALUATION LOGIC =====================
def compute_valuation(profit, revenue_type):
    try:
        profit = float(profit or 0)
    except:
        profit = 0

    if profit <= 1000:
        return 1000, 1000, 1000

    rtype = (revenue_type or "").lower()
    if "ad" in rtype:
        return profit * 1.0, profit * 1.7, profit * 1.35
    elif "subscription" in rtype or "iap" in rtype:
        return profit * 1.5, profit * 2.3, profit * 1.9
    else:
        return profit * 2.5, profit * 2.5, profit * 2.5

# ===================== SHEET + EMAIL =====================
def save_to_sheet(user_id, a, vmin, vmax, vavg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now, user_id, a.get("name", ""), a.get("app_link", ""), a.get("revenue", ""),
        a.get("marketing_cost", ""), a.get("server_cost", ""), a.get("profit", ""),
        a.get("revenue_type", ""), a.get("email", ""), f"${vmin:,.0f} - ${vmax:,.0f}"
    ]
    sheet.append_row(row)

def send_email(to, name, valuation):
    try:
        # You can plug any email API here (e.g., SendGrid, Gmail SMTP, etc.)
        # Placeholder: success simulation
        print(f"Sending valuation email to {to}: {valuation}")
        return True
    except Exception as e:
        print("Email send failed:", e)
        return False

# ===================== MAIN WEBHOOK =====================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Invalid verify token", 403

    data = request.get_json()
    if not data:
        return "No data", 400

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue

            msg = messages[0]
            msg_id = msg.get("id")
            if msg_id in processed_msg_ids:
                continue
            processed_msg_ids.add(msg_id)

            user_id = msg["from"]
            text = msg.get("text", {}).get("body", "").strip()
            button_id = msg.get("interactive", {}).get("button_reply", {}).get("id", "").lower()
            incoming = button_id or text

            now = datetime.utcnow()

            # Create or check session
            if user_id not in user_states:
                user_states[user_id] = {
                    "step": -1,
                    "answers": {},
                    "session_started": False,
                    "last_active": now,
                }
                send_whatsapp_buttons(
                    user_id,
                    "Hi, I am Kalagato AI Agent. Are you interested in selling your app?",
                    ["Yes", "No"]
                )
                continue

            state = user_states[user_id]
            step = state["step"]
            last_active = state.get("last_active", now)

            # Timeout check
            if now - last_active > SESSION_TIMEOUT:
                user_states.pop(user_id, None)
                send_whatsapp_buttons(
                    user_id,
                    "👋 Session expired. Let's start over.\nAre you interested in selling your app?",
                    ["Yes", "No"]
                )
                continue

            state["last_active"] = now

            # Greeting step
            if step == -1:
                if incoming in ["no", "no_reply"]:
                    send_whatsapp_text(user_id, "Thank you! If you have any queries, contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                elif incoming in ["yes", "yes_reply"]:
                    state["session_started"] = True
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTIONS[0]["text"])
                    continue
                else:
                    send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes", "No"])
                    continue

            # Ignore stray yes/no during active chat
            if step >= 0 and incoming in ["yes", "no", "yes_reply", "no_reply"]:
                continue

            # Handle current question
            q = QUESTIONS[step]
            key, qtype = q["key"], q["type"]
            val = incoming.strip()
            valid = True

            if qtype == "number":
                try:
                    float(val)
                except ValueError:
                    valid = False
            elif qtype == "email":
                valid = bool(re.match(r"[^@]+@[^@]+\.[^@]+", val))
            elif qtype == "link":
                valid = val.startswith("http://") or val.startswith("https://")

            if not valid:
                send_whatsapp_text(user_id, f"❌ Please enter a valid {qtype}.")
                send_whatsapp_text(user_id, q["text"])
                continue

            # Save answer
            state["answers"][key] = val
            state["step"] += 1

            if state["step"] < len(QUESTIONS):
                send_whatsapp_text(user_id, QUESTIONS[state["step"]]["text"])
                continue

            # Done → compute valuation
            a = state["answers"]
            vmin, vmax, vavg = compute_valuation(a.get("profit"), a.get("revenue_type"))
            valuation_range = f"${vmin:,.0f} to ${vmax:,.0f}"

            save_to_sheet(user_id, a, vmin, vmax, vavg)
            sent = send_email(a["email"], a["name"], valuation_range)

            if sent:
                send_whatsapp_text(user_id, f"✅ Thank you {a['name']}! We've sent your valuation ({valuation_range}) to your email.")
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but couldn't send the email automatically.")

            user_states.pop(user_id, None)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
