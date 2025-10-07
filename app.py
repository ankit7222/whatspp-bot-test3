# app.py
import os
import json
import re
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ---------------------------
# Environment variables (set these)
# ---------------------------
# WhatsApp
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")                # WhatsApp Cloud permanent token
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")              # phone number id
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")    # webhook verify token

# Google Sheets
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # single-line JSON string OR leave blank if using service_account.json file
SHEET_ID = os.getenv("SHEET_ID")                        # spreadsheet ID (not full URL)
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")          # worksheet/tab name

# Gmail SMTP (App password)
GMAIL_USER = os.getenv("GMAIL_USER")                    # e.g. dealflow@kalagato.ai
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")            # google app password

# Optional CC env (comma-separated)
VALUATION_CC = os.getenv("VALUATION_CC", "")            # e.g. "ops@kalagato.co,accounts@kalagato.co"

# Bot texts (override via env if desired)
GREETING_TEXT = os.getenv("GREETING_TEXT", "Hi, I am Kalagato AI Agent. Would you like a free app valuation?")
NO_RESPONSE_TEXT = os.getenv("NO_RESPONSE_TEXT", "Thanks — if you have any queries contact us on aman@kalagato.co")
THANK_YOU_TEXT = os.getenv("THANK_YOU_TEXT", "✅ Thank you! We saved your details and emailed your valuation.")

# WhatsApp API endpoint
WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

# ---------------------------
# Validate required essentials early (only warn for optional items)
# ---------------------------
if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
    raise RuntimeError("WHATSAPP_TOKEN and PHONE_NUMBER_ID must be set in environment variables")

if not SHEET_ID:
    raise RuntimeError("SHEET_ID must be set in environment variables")

# ---------------------------
# Google Sheets setup
# ---------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
if GOOGLE_SHEETS_CREDENTIALS:
    try:
        creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    except Exception as e:
        raise RuntimeError(f"Failed parsing GOOGLE_SHEETS_CREDENTIALS: {e}")
else:
    # fallback to local file for dev
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)

gc = gspread.authorize(creds)
worksheet = gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)

# Ensure headers
EXPECTED_HEADERS = [
    "Timestamp", "User ID", "Name", "App Link",
    "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
    "Revenue Type", "Email", "Phone",
    "Estimated Valuation Min", "Estimated Valuation Max", "Estimated Valuation Mid", "CCs"
]
try:
    current_header = worksheet.row_values(1)
    if current_header[: len(EXPECTED_HEADERS)] != EXPECTED_HEADERS:
        worksheet.insert_row(EXPECTED_HEADERS, index=1)
except Exception as e:
    print("Warning: Could not ensure headers in sheet:", e)

# ---------------------------
# Conversation flow (in-memory)
# ---------------------------
user_states = {}

QUESTION_FLOW = [
    {"key": "name", "text": "What is your name?", "type": "text", "required": True},
    {"key": "appLink", "text": "Please provide the App Store or Play Store link (https://...)", "type": "link", "required": True},
    {"key": "revenue", "text": "Approx. Annual Revenue (USD) — numbers only", "type": "number", "required": True},
    {"key": "marketingCost", "text": "Annual Marketing Cost (USD) — numbers only", "type": "number", "required": True},
    {"key": "serverCost", "text": "Annual Server Cost (USD) — numbers only", "type": "number", "required": True},
    {"key": "profit", "text": "Approx. Annual Profit (USD) — numbers only", "type": "number", "required": True},
    {"key": "revenueType", "text": "What is the primary revenue type? Reply with number:\n1. Ad Revenue\n2. Subscription Revenue\n3. Others", "type": "choice", "choices": {"1":"ad","2":"subscription","3":"others"}, "required": True},
    {"key": "email", "text": "Your email address (we will send valuation there)", "type": "email", "required": True},
    {"key": "phone", "text": "Phone number (optional). Reply 'skip' to skip.", "type": "phone", "required": False},
    # optional: allow user to provide cc emails as last step (comma separated) -- optional
    {"key": "cc_emails", "text": "Optional: If you want to CC others, reply with comma-separated emails (or 'skip')", "type": "cc", "required": False}
]

# ---------------------------
# WhatsApp helpers
# ---------------------------
def send_whatsapp_text(to, text):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
    except Exception as e:
        print("WhatsApp send error:", e)

def send_whatsapp_buttons(to, text, buttons):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.lower().replace(" ", "_"), "title": b}} for b in buttons
                ]
            }
        }
    }
    try:
        requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
    except Exception as e:
        print("WhatsApp send error:", e)

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
    """Parse and validate up to 3 user-supplied CC emails (comma-separated)."""
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
    # dedupe and limit to max 3
    seen = []
    for x in valid:
        if x not in seen:
            seen.append(x)
    return seen[:3]

# ---------------------------
# Valuation computation (rules mirrored from your Deno function)
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
# Email sending with env-driven CC support
# ---------------------------
def send_valuation_email(to_email, to_name, plain_text, full_html, cc_list=None):
    """
    Sends valuation email to main recipient + CC (env VALUATION_CC and optional cc_list param).
    Returns True on success, False on failure.
    """
    if not GMAIL_USER or not GMAIL_APP_PASS:
        print("GMAIL credentials not set; skipping email send.")
        return False

    # build CC list: env + param
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
        print("Email sent to", to_email, "cc:", cc_all)
        return True
    except Exception as e:
        print("Email send failed:", e)
        return False

# ---------------------------
# Save to Google Sheet
# ---------------------------
def save_to_sheet(user_id, answers, valuation_min, valuation_max, estimated, cc_list):
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
        worksheet.append_row(row)
    except Exception as e:
        print("Failed to append to sheet:", e)

# ---------------------------
# Webhook endpoint
# ---------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # verification for GET
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verification token", 403

    payload = request.get_json(silent=True)
    if not payload:
        return "No payload", 400

    # iterate entries & changes (WhatsApp cloud structure)
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

            # new user -> send greeting buttons
            if user_id not in user_states:
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                user_states[user_id] = {"step": -1, "answers": {}}
                continue

            state = user_states[user_id]
            step = state["step"]

            # handle No at greeting
            if (button_id == "no" or (text and text.lower() == "no")) and step == -1:
                send_whatsapp_text(user_id, NO_RESPONSE_TEXT)
                del user_states[user_id]
                continue

            # handle Yes at greeting -> start flow
            if (button_id == "yes" or (text and text.lower() == "yes")) and step == -1:
                state["step"] = 0
                send_whatsapp_text(user_id, QUESTION_FLOW[0]["text"])
                continue

            # fallback if still -1
            if step == -1:
                state["step"] = 0
                send_whatsapp_text(user_id, QUESTION_FLOW[0]["text"])
                continue

            # in-flow: validate current answer
            current_q = QUESTION_FLOW[step]
            key = current_q["key"]
            qtype = current_q["type"]
            val = (button_id or text or "").strip()

            # allow skip for optional phone/cc step
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
            elif qtype == "cc":
                # will parse later
                pass

            if not valid:
                send_whatsapp_text(user_id, err)
                continue

            # store answer
            state["answers"][key] = val
            state["step"] += 1

            # ask next question or finish
            if state["step"] < len(QUESTION_FLOW):
                next_q = QUESTION_FLOW[state["step"]]["text"]
                send_whatsapp_text(user_id, next_q)
            else:
                answers = state["answers"]
                # parse CCs: env + user
                user_ccs = parse_user_ccs(answers.get("cc_emails", ""))
                # combine env CCs (built into send function) and user_ccs passed in param
                vmin, vmax, mid = compute_valuation(answers.get("profit"), answers.get("revenueType"))

                # Save to sheet
                save_to_sheet(user_id, answers, vmin, vmax, mid, user_ccs)

                # prepare email content
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

                # send email (includes env CC + user CCs param)
                email_success = send_valuation_email(answers.get("email"), safe_name, plain_text, full_html, cc_list=user_ccs)

                # reply user
                if email_success:
                    send_whatsapp_text(user_id, THANK_YOU_TEXT)
                else:
                    send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. " + THANK_YOU_TEXT)

                # cleanup
                del user_states[user_id]

    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
