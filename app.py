# app.py
import os
import json
import re
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

app = Flask(__name__)

# ---------------------------
# ENV / Config
# ---------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # single-line JSON OR leave empty to use service_account.json file
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")

VALUATION_CC = os.getenv("VALUATION_CC", "")  # optional comma-separated CCs

GREETING_TEXT = os.getenv("GREETING_TEXT", "Hi, I am Kalagato AI Agent. Would you like a free app valuation?")
NO_RESPONSE_TEXT = os.getenv("NO_RESPONSE_TEXT", "Thanks — if you have any queries contact us on aman@kalagato.co")
THANK_YOU_TEXT = os.getenv("THANK_YOU_TEXT", "✅ Thank you! We saved your details and emailed your valuation.")

PORT = int(os.getenv("PORT", 5000))
WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

# ---------------------------
# Basic checks
# ---------------------------
if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
    app.logger.warning("WHATSAPP_TOKEN or PHONE_NUMBER_ID not set. Bot won't be able to send WhatsApp messages.")

if not SHEET_ID:
    raise RuntimeError("SHEET_ID environment variable is required and not set.")

# ---------------------------
# Google Sheets auth + worksheet creation if missing
# ---------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def get_worksheet():
    """Authorize and return the worksheet. Create tab if missing."""
    try:
        if GOOGLE_SHEETS_CREDENTIALS:
            creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
            creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    except Exception as e:
        app.logger.error("Failed to load Google credentials: %s", e)
        raise

    client = gspread.authorize(creds)

    try:
        spreadsheet = client.open_by_key(SHEET_ID)
    except Exception as e:
        app.logger.error("Cannot open spreadsheet with id %s: %s", SHEET_ID, e)
        raise

    # Try to open worksheet; if not found, create it
    try:
        worksheet = spreadsheet.worksheet(SHEET_NAME)
    except WorksheetNotFound:
        app.logger.info("Worksheet '%s' not found — creating it.", SHEET_NAME)
        # create worksheet with default rows/cols
        worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="20")
        # set headers
        headers = [
            "Timestamp", "User ID", "Name", "App Link",
            "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
            "Revenue Type", "Email", "Phone",
            "Estimated Valuation Min", "Estimated Valuation Max", "Estimated Valuation Mid", "CCs"
        ]
        worksheet.insert_row(headers, index=1)
    except Exception as e:
        app.logger.error("Error accessing worksheet: %s", e)
        raise

    return worksheet

# lazily initialize worksheet (so app starts quickly)
worksheet = None
def ensure_worksheet():
    global worksheet
    if worksheet is None:
        worksheet = get_worksheet()
    return worksheet

# ---------------------------
# Conversation state (in-memory)
# ---------------------------
user_states = {}
QUESTION_FLOW = [
    {"key": "name", "text": "What is your name?", "type": "text", "required": True},
    {"key": "appLink", "text": "Please provide the App Store or Play Store link (https://...)", "type": "link", "required": True},
    {"key": "revenue", "text": "Approx. Annual Revenue (USD) — numbers only", "type": "number", "required": True},
    {"key": "marketingCost", "text": "Annual Marketing Cost (USD) — numbers only", "type": "number", "required": True},
    {"key": "serverCost", "text": "Annual Server Cost (USD) — numbers only", "type": "number", "required": True},
    {"key": "profit", "text": "Approx. Annual Profit (USD) — numbers only", "type": "number", "required": True},
    {"key": "revenueType", "text": "What is the primary revenue type? Reply:\n1. Ad Revenue\n2. Subscription Revenue\n3. Others", "type": "choice", "choices": {"1":"ad","2":"subscription","3":"others"}, "required": True},
    {"key": "email", "text": "Your email address (we will send valuation there)", "type": "email", "required": True},
    {"key": "phone", "text": "Phone number (optional). Reply 'skip' to skip.", "type": "phone", "required": False},
    {"key": "cc_emails", "text": "Optional: reply with comma-separated emails to CC (or 'skip')", "type": "cc", "required": False}
]

# ---------------------------
# WhatsApp helpers
# ---------------------------
def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("Skipping WhatsApp send (missing token/phone id): %s", text)
        return
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp send status: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        app.logger.error("WhatsApp send exception: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("Skipping WhatsApp interactive send (missing token/phone id): %s", text)
        return
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b.lower().replace(" ", "_"), "title": b}} for b in buttons]}
        }
    }
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp buttons status: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        app.logger.error("WhatsApp send exception: %s", e)

# ---------------------------
# Validation helpers
# ---------------------------
def is_number(val):
    try:
        float(str(val))
        return True
    except:
        return False

def is_valid_link(url):
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))

def is_valid_email(email):
    return isinstance(email, str) and re.match(r"[^@]+@[^@]+\.[^@]+", email)

def parse_user_ccs(raw):
    if not raw:
        return []
    raw = raw.strip()
    if raw.lower() == "skip":
        return []
    cands = [e.strip() for e in raw.split(",") if e.strip()]
    valid = []
    for e in cands:
        if re.match(r"[^@]+@[^@]+\.[^@]+", e):
            valid.append(e)
    seen = []
    for x in valid:
        if x not in seen:
            seen.append(x)
    return seen[:3]

# ---------------------------
# Valuation logic
# ---------------------------
def compute_valuation(profit_value, revenue_type):
    try:
        profit_num = float(profit_value) if profit_value not in (None, "") else 0.0
    except:
        profit_num = 0.0
    rt = (revenue_type or "").lower()
    valuation_min = valuation_max = estimated = 0.0

    if profit_num <= 0 or profit_num < 1000:
        valuation_min = valuation_max = estimated = 1000.0
    elif rt == "ad" or "ad" in rt:
        valuation_min = profit_num * 1.0
        valuation_max = profit_num * 1.7
        estimated = (valuation_min + valuation_max) / 2.0
    elif rt == "subscription" or "sub" in rt or rt == "others" or "other" in rt:
        valuation_min = profit_num * 1.5
        valuation_max = profit_num * 2.3
        estimated = (valuation_min + valuation_max) / 2.0
    else:
        estimated = profit_num * 2.5
        valuation_min = valuation_max = estimated

    return valuation_min, valuation_max, estimated

# ---------------------------
# Email with CC support
# ---------------------------
def send_valuation_email(to_email, to_name, plain_text, full_html, cc_list=None):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        app.logger.warning("Gmail credentials not set; skipping email send.")
        return False

    env_cc_list = [e.strip() for e in (VALUATION_CC or "").split(",") if e.strip()]
    param_cc_list = cc_list or []
    cc_all = []
    for e in env_cc_list + param_cc_list:
        if e and e not in cc_all:
            cc_all.append(e)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your App Valuation Estimate is Here!"
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    if cc_all:
        msg["Cc"] = ", ".join(cc_all)

    part1 = MIMEText(plain_text, "plain")
    part2 = MIMEText(full_html, "html")
    msg.attach(part1)
    msg.attach(part2)

    recipients = [to_email] + cc_all

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=20)
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())
        server.quit()
        app.logger.info("Email sent to %s cc=%s", to_email, cc_all)
        return True
    except Exception as e:
        app.logger.error("Email send failed: %s", e)
        return False

# ---------------------------
# Save to Google Sheet
# ---------------------------
def save_to_sheet(user_id, answers, valuation_min, valuation_max, estimated, cc_list):
    try:
        ws = ensure_worksheet()
    except Exception as e:
        app.logger.error("Cannot access worksheet: %s", e)
        return
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,
        user_id,
        answers.get("name"),
        answers.get("appLink"),
        answers.get("revenue"),
        answers.get("marketingCost"),
        answers.get("serverCost"),
        answers.get("profit"),
        answers.get("revenueType"),
        answers.get("email"),
        answers.get("phone"),
        valuation_min,
        valuation_max,
        estimated,
        ", ".join(cc_list or [])
    ]
    try:
        ws.append_row(row)
    except Exception as e:
        app.logger.error("Failed to append to sheet: %s", e)

# ---------------------------
# Health route
# ---------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# ---------------------------
# Webhook route
# ---------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verification token", 403

    payload = request.get_json(silent=True)
    if not payload:
        return "No payload", 400

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue
            msg = messages[0]
            user_id = msg.get("from")
            text = msg.get("text", {}).get("body", "").strip()
            button_id = msg.get("interactive", {}).get("button_reply", {}).get("id")

            if user_id not in user_states:
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                user_states[user_id] = {"step": -1, "answers": {}}
                continue

            state = user_states[user_id]
            step = state["step"]

            if (button_id == "no" or (text and text.lower() == "no")) and step == -1:
                send_whatsapp_text(user_id, NO_RESPONSE_TEXT)
                del user_states[user_id]
                continue

            if (button_id == "yes" or (text and text.lower() == "yes")) and step == -1:
                state["step"] = 0
                send_whatsapp_text(user_id, QUESTION_FLOW[0]["text"])
                continue

            if step == -1:
                state["step"] = 0
                send_whatsapp_text(user_id, QUESTION_FLOW[0]["text"])
                continue

            current_q = QUESTION_FLOW[step]
            key = current_q["key"]
            qtype = current_q["type"]
            val = (button_id or text or "").strip()

            if qtype in ("phone", "cc") and val.lower() == "skip":
                val = ""

            valid = True
            err = None

            if qtype == "number":
                if not is_number(val) and current_q.get("required", True):
                    valid = False
                    err = "❌ Please send a numeric value (numbers only)."
            elif qtype == "link":
                if not is_valid_link(val):
                    valid = False
                    err = "❌ Please send a valid URL starting with http:// or https://"
            elif qtype == "email":
                if not is_valid_email(val):
                    valid = False
                    err = "❌ Please send a valid email address."
            elif qtype == "choice":
                choices = current_q["choices"]
                if val in choices:
                    val = choices[val]
                elif val not in choices.values():
                    valid = False
                    err = "❌ Please reply with the option number (e.g. '1' for Ad Revenue)."

            if not valid:
                send_whatsapp_text(user_id, err)
                continue

            state["answers"][key] = val
            state["step"] += 1

            if state["step"] < len(QUESTION_FLOW):
                send_whatsapp_text(user_id, QUESTION_FLOW[state["step"]]["text"])
            else:
                answers = state["answers"]
                user_ccs = parse_user_ccs(answers.get("cc_emails", ""))
                vmin, vmax, mid = compute_valuation(answers.get("profit"), answers.get("revenueType"))

                save_to_sheet(user_id, answers, vmin, vmax, mid, user_ccs)

                safe_name = answers.get("name") or ""
                if vmin == vmax:
                    valuation_html = f'<h2 style="font-size:36px;color:#007bff;">${vmin:,.2f}</h2>'
                    plain_text = f"Your App Valuation Estimate is: ${vmin:,.2f}"
                else:
                    valuation_html = f'<h2 style="font-size:28px;color:#007bff;">${vmin:,.2f} to ${vmax:,.2f}</h2>'
                    plain_text = f"Your App Valuation Estimate is: ${vmin:,.2f} to ${vmax:,.2f}"

                full_html = f"""
                <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #fff; border-radius:8px;">
                  <p>Hi {safe_name or 'there'},</p>
                  <p>Thank you for using our valuation tool. Based on the details you provided, here is your app's estimated valuation:</p>
                  <div style="margin:20px 0;">{valuation_html}</div>
                  <p>This is a valuation range — the final value may vary depending on engagement and other factors.</p>
                  <p>Best regards,<br/>The Kalagato Team</p>
                </div>
                """

                email_success = send_valuation_email(answers.get("email"), safe_name, plain_text, full_html, cc_list=user_ccs)

                if email_success:
                    send_whatsapp_text(user_id, THANK_YOU_TEXT)
                else:
                    send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. " + THANK_YOU_TEXT)

                del user_states[user_id]

    return "OK", 200

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    # Ensure worksheet is created at startup to catch credential errors early
    try:
        ensure_worksheet()
    except Exception as e:
        app.logger.error("Worksheet initialization error: %s", e)

    app.run(host="0.0.0.0", port=PORT)
