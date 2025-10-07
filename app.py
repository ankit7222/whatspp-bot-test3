# app.py
import os
import json
import re
import time
import requests
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ---------------------------
# ENVIRONMENT VARIABLES
# ---------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")
VALUATION_CC = os.getenv("VALUATION_CC", "")

PORT = int(os.getenv("PORT", 5000))
WHATSAPP_API = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

# ---------------------------
# QUESTIONS FLOW
# ---------------------------
QUESTIONS = [
    {"key": "name", "text": "What is your name?", "type": "text"},
    {"key": "app_link", "text": "Please provide your App Store or Play Store link (https://...)", "type": "link"},
    {"key": "revenue", "text": "What is your annual revenue (USD)?", "type": "number"},
    {"key": "marketing_cost", "text": "What is your annual marketing cost (USD)?", "type": "number"},
    {"key": "server_cost", "text": "What is your annual server cost (USD)?", "type": "number"},
    {"key": "profit", "text": "What is your annual profit (USD)?", "type": "number"},
    {"key": "revenue_type", "text": "What is your revenue type? (Ad, Subscription, IAP — use commas if multiple)", "type": "text"},
    {"key": "email", "text": "Please share your email address (valuation will be sent there).", "type": "email"},
]

# ---------------------------
# STATE
# ---------------------------
user_states = {}
processed_msg_ids = set()

# ---------------------------
# HELPERS
# ---------------------------
def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("WhatsApp not configured")
        return
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(WHATSAPP_API, headers=HEADERS, json=payload)

def send_whatsapp_buttons(to, text, buttons):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b.lower(), "title": b}} for b in buttons]},
        },
    }
    requests.post(WHATSAPP_API, headers=HEADERS, json=payload)

def is_number(val):
    try:
        float(val)
        return True
    except:
        return False

def is_valid_email(email):
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))

def is_valid_link(url):
    return url.startswith("http://") or url.startswith("https://")

# ---------------------------
# GOOGLE SHEET
# ---------------------------
def get_sheet():
    creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_info)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows="1000", cols="20")
        headers = [
            "Timestamp", "User ID", "Name", "App Link",
            "Annual Revenue", "Marketing Cost", "Server Cost",
            "Annual Profit", "Revenue Type", "Email",
            "Valuation Min", "Valuation Max", "Valuation Avg"
        ]
        ws.insert_row(headers, index=1)
    return ws

def save_to_sheet(user_id, data, vmin, vmax, vavg):
    ws = get_sheet()
    ws.append_row([
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        user_id,
        data.get("name"),
        data.get("app_link"),
        data.get("revenue"),
        data.get("marketing_cost"),
        data.get("server_cost"),
        data.get("profit"),
        data.get("revenue_type"),
        data.get("email"),
        vmin, vmax, vavg
    ])

# ---------------------------
# EMAIL
# ---------------------------
def send_email(to_email, name, valuation_range):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        print("Gmail not configured")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your App Valuation Estimate"
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    if VALUATION_CC:
        msg["Cc"] = VALUATION_CC

    text = f"Hi {name},\n\nYour app valuation is estimated at {valuation_range}.\n\nRegards,\nKalagato Team"
    html = f"""
    <div style="font-family:Arial,sans-serif;">
        <p>Hi {name},</p>
        <p>Your app valuation is estimated at <b>{valuation_range}</b>.</p>
        <p>Regards,<br>Kalagato Team</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    recipients = [to_email] + [e.strip() for e in VALUATION_CC.split(",") if e.strip()]
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())
        server.quit()
        print(f"Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

# ---------------------------
# VALUATION LOGIC
# ---------------------------
def compute_valuation(profit, revenue_type):
    try:
        profit = float(profit)
    except:
        profit = 0
    if profit <= 1000:
        return 1000, 1000, 1000
    rtype = (revenue_type or "").lower()
    if "ad" in rtype:
        vmin, vmax = profit * 1.0, profit * 1.7
    elif "sub" in rtype or "subscription" in rtype:
        vmin, vmax = profit * 1.5, profit * 2.3
    elif "iap" in rtype:
        vmin, vmax = profit * 1.2, profit * 1.8
    else:
        vmin = vmax = profit * 2.0
    return vmin, vmax, (vmin + vmax) / 2

# ---------------------------
# WEBHOOK
# ---------------------------
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
            msgs = value.get("messages", [])
            if not msgs:
                continue
            msg = msgs[0]
            msg_id = msg.get("id")
            if msg_id in processed_msg_ids:
                continue
            processed_msg_ids.add(msg_id)

            user_id = msg["from"]
            text = msg.get("text", {}).get("body", "").strip()
            button_id = msg.get("interactive", {}).get("button_reply", {}).get("id")
            incoming = button_id or text

            # Start new session
            if user_id not in user_states:
                send_whatsapp_buttons(user_id, "Hi, I am Kalagato AI Agent. Are you interested in selling your app?", ["Yes", "No"])
                user_states[user_id] = {"step": -1, "answers": {}}
                continue

            state = user_states[user_id]

            # Handle Yes/No
            if state["step"] == -1:
                if incoming.lower() in ["no", "no_reply"]:
                    send_whatsapp_text(user_id, "Thank you! If you have any queries, contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                elif incoming.lower() in ["yes", "yes_reply"]:
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTIONS[0]["text"])
                    continue
                else:
                    send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes", "No"])
                    continue

            # Handle normal questions
            q = QUESTIONS[state["step"]]
            key = q["key"]
            qtype = q["type"]

            valid = True
            if qtype == "number" and not is_number(incoming):
                valid = False
                send_whatsapp_text(user_id, "❌ Please enter a number.")
            elif qtype == "email" and not is_valid_email(incoming):
                valid = False
                send_whatsapp_text(user_id, "❌ Please enter a valid email.")
            elif qtype == "link" and not is_valid_link(incoming):
                valid = False
                send_whatsapp_text(user_id, "❌ Please send a valid link (https://...).")

            if not valid:
                send_whatsapp_text(user_id, q["text"])
                continue

            # Save answer
            state["answers"][key] = incoming
            state["step"] += 1

            # Next question or finish
            if state["step"] < len(QUESTIONS):
                send_whatsapp_text(user_id, QUESTIONS[state["step"]]["text"])
            else:
                a = state["answers"]
                vmin, vmax, vavg = compute_valuation(a.get("profit"), a.get("revenue_type"))
                valuation_text = f"${vmin:,.0f} to ${vmax:,.0f}"
                save_to_sheet(user_id, a, vmin, vmax, vavg)
                email_sent = send_email(a["email"], a["name"], valuation_text)
                if email_sent:
                    send_whatsapp_text(user_id, f"✅ Thank you! We've sent your valuation ({valuation_text}) to your email.")
                else:
                    send_whatsapp_text(user_id, "✅ Data saved, but failed to send email. Check configuration.")
                user_states.pop(user_id, None)
    return "OK", 200

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
