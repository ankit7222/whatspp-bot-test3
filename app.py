# app.py
import os
import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from datetime import datetime
import requests
import logging

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ---------------------------
# ENV / config
# ---------------------------
GOOGLE_CREDS_ENV = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")

PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

# ---------------------------
# Google Sheets initialization helper (robust)
# ---------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
_gs_client = None
_worksheet = None
_gs_init_error = None

def _load_service_account_info():
    """
    Returns service account dict or raises Exception.
    Accept formats:
      - GOOGLE_SHEETS_CREDENTIALS environment var containing full JSON (multiline allowed)
      - GOOGLE_SHEETS_CREDENTIALS env var set to a filename (e.g., 'service_account.json')
      - file 'service_account.json' in project root
    """
    global _gs_init_error
    raw = GOOGLE_CREDS_ENV
    # 1) If env var looks like a filename, try load file from disk
    if raw:
        # If the env contains a path ending with .json, treat it as filename
        if raw.endswith(".json") and os.path.exists(raw):
            with open(raw, "r") as f:
                return json.load(f)
        # If the env contains braces attempt to parse it as JSON (multiline allowed)
        try:
            parsed = json.loads(raw)
            return parsed
        except Exception as e:
            # Might contain literal newlines but still be valid; JSON loader handles that.
            # If parsing failed, we will fallthrough and try file fallback below.
            app.logger.warning("GOOGLE_SHEETS_CREDENTIALS parsing failed: %s", e)

    # 2) try reading default file from project root
    fallback = "service_account.json"
    if os.path.exists(fallback):
        with open(fallback, "r") as f:
            return json.load(f)

    raise Exception("No valid Google service account credentials found in env var or service_account.json file.")

def try_init_gs():
    """
    Initialize global worksheet and return worksheet or None on failure.
    """
    global _gs_client, _worksheet, _gs_init_error
    if _worksheet is not None:
        return _worksheet
    try:
        creds_info = _load_service_account_info()
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _gs_client = gspread.authorize(creds)
        if not SHEET_ID:
            _gs_init_error = "SHEET_ID not configured"
            app.logger.error(_gs_init_error)
            return None
        spreadsheet = _gs_client.open_by_key(SHEET_ID)
        try:
            _worksheet = spreadsheet.worksheet(SHEET_NAME)
        except WorksheetNotFound:
            # create the sheet and header row if missing
            _worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="2000", cols="20")
            headers = [
                "Timestamp", "User ID", "Name", "App Link",
                "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
                "Revenue Type", "Email", "Phone", "Valuation Min", "Valuation Max", "Valuation Mid"
            ]
            _worksheet.insert_row(headers, index=1)
        app.logger.info("Google Sheets initialized (sheet: %s)", SHEET_NAME)
        _gs_init_error = None
        return _worksheet
    except Exception as e:
        _gs_init_error = f"Google Sheets auth failed: {e}"
        app.logger.error(_gs_init_error)
        return None

# ---------------------------
# Email sending (Gmail via SSL)
# ---------------------------
def send_valuation_email(to_email, name, plain_text, html_content, cc_list=None):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        app.logger.warning("Gmail credentials not configured; skipping email send.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your App Valuation Estimate"
        msg["From"] = GMAIL_USER
        msg["To"] = to_email
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)

        msg.attach(MIMEText(plain_text, "plain"))
        msg.attach(MIMEText(html_content, "html"))

        recipients = [to_email] + (cc_list or [])

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_USER, recipients, msg.as_string())

        app.logger.info("Email sent to %s (cc=%s)", to_email, cc_list)
        return True
    except Exception as e:
        app.logger.error("Email send failed: %s", e)
        return False

# ---------------------------
# Whatsapp helpers
# ---------------------------
def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp token/phone not configured - skipping send.")
        return
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    try:
        r = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=8)
        app.logger.debug("WhatsApp send status %s %s", r.status_code, r.text[:200])
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp token/phone not configured - skipping send.")
        return
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":text},
            "action":{"buttons":[{"type":"reply","reply":{"id":b.lower().replace(" ","_"),"title":b}} for b in buttons]}
        }
    }
    try:
        r = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=8)
        app.logger.debug("WhatsApp interactive status %s %s", r.status_code, r.text[:200])
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)

# ---------------------------
# Conversation flow + valuation
# ---------------------------
user_states = {}
processed_msg_ids = set()
SESSION_TIMEOUT_SECONDS = 15 * 60

QUESTIONS = [
    {"key":"name","text":"What is your name?","type":"text"},
    {"key":"app_link","text":"Please provide your App Store or Play Store link (https://...)","type":"link"},
    {"key":"revenue","text":"What is your annual revenue (USD)?","type":"number"},
    {"key":"marketing_cost","text":"What is your annual marketing cost (USD)?","type":"number"},
    {"key":"server_cost","text":"What is your annual server cost (USD)?","type":"number"},
    {"key":"profit","text":"What is your annual profit (USD)?","type":"number"},
    {"key":"revenue_type","text":"Which revenue types? Reply using commas (Ad, Subscription, IAP). Example: Ad, IAP","type":"text"},
    {"key":"email","text":"Please share your email address (we will send valuation there)","type":"email"},
    {"key":"phone","text":"Phone number (optional). Reply 'skip' to skip.","type":"phone","required":False}
]

def compute_valuation(profit_val, revenue_type_text):
    try:
        profit = float(profit_val) if profit_val not in (None,"") else 0.0
    except:
        profit = 0.0
    def fmt(v): return float(v)
    rt = (revenue_type_text or "").lower()
    if profit <= 0 or profit < 1000:
        return 1000.0,1000.0,1000.0
    if "ad" in rt:
        vmin = profit*1.0; vmax = profit*1.7
    elif "subscription" in rt or "sub" in rt:
        vmin = profit*1.5; vmax = profit*2.3
    elif "iap" in rt:
        vmin = profit*1.5; vmax = profit*2.0
    else:
        vmin = profit*2.5; vmax = profit*2.5
    return vmin, vmax, (vmin+vmax)/2.0

# ---------------------------
# Save row to sheet
# ---------------------------
def save_to_sheet(user_id, answers, vmin, vmax, mid):
    ws = try_init_gs()
    if ws is None:
        app.logger.warning("Skipping sheet save: %s", _gs_init_error)
        return False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now, user_id,
        answers.get("name",""),
        answers.get("app_link",""),
        answers.get("revenue",""),
        answers.get("marketing_cost",""),
        answers.get("server_cost",""),
        answers.get("profit",""),
        answers.get("revenue_type",""),
        answers.get("email",""),
        answers.get("phone",""),
        f"{vmin:.2f}", f"{vmax:.2f}", f"{mid:.2f}"
    ]
    try:
        ws.append_row(row)
        app.logger.info("Saved row to sheet for user %s", user_id)
        return True
    except Exception as e:
        app.logger.error("Failed to append row: %s", e)
        return False

# ---------------------------
# Webhook endpoint
# ---------------------------
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # Verify
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        if token and token == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Invalid verify token", 403

    payload = request.get_json(silent=True)
    if not payload:
        return "No payload", 400

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value",{})
            messages = value.get("messages",[])
            if not messages:
                continue
            msg = messages[0]
            # dedupe
            msg_id = msg.get("id") or str(msg.get("timestamp")) or None
            if msg_id:
                if msg_id in processed_msg_ids:
                    app.logger.debug("Duplicate incoming %s ignored", msg_id)
                    continue
                processed_msg_ids.add(msg_id)
                # cap set size
                if len(processed_msg_ids) > 5000:
                    processed_msg_ids.pop()

            user_id = msg.get("from")
            text_body = msg.get("text",{}).get("body","").strip()
            button_id = msg.get("interactive",{}).get("button_reply",{}).get("id","")
            incoming = (button_id or text_body or "").strip()

            if not incoming:
                continue

            now_ts = datetime.utcnow().timestamp()
            # new user/session
            if user_id not in user_states:
                user_states[user_id] = {"step": -1, "answers": {}, "started_at": now_ts, "last_active": now_ts}
                # greet only on hi/hello OR if incoming is Yes/No from button
                if text_body.lower() in ("hi","hello") or incoming.lower() in ("yes","no"):
                    send_whatsapp_buttons(user_id, "Hi, I am Kalagato AI Agent. Are you interested in selling your app?", ["Yes","No"])
                else:
                    # send greeting buttons anyway
                    send_whatsapp_buttons(user_id, "Hi, I am Kalagato AI Agent. Are you interested in selling your app?", ["Yes","No"])
                continue

            state = user_states[user_id]
            # timeout handling
            if now_ts - state.get("last_active", now_ts) > SESSION_TIMEOUT_SECONDS:
                user_states.pop(user_id, None)
                send_whatsapp_buttons(user_id, "Session expired. Let's start again. Are you interested in selling your app?", ["Yes","No"])
                continue
            state["last_active"] = now_ts

            # greeting step
            if state["step"] == -1:
                low = incoming.lower()
                if low in ("no","no_reply"):
                    send_whatsapp_text(user_id, "Thanks! If you have queries contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                if low in ("yes","yes_reply"):
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTIONS[0]["text"])
                    continue
                # else ask to press Yes/No
                send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes","No"])
                continue

            # ignore stray yes/no mid-flow
            if state["step"] >= 0 and incoming.lower() in ("yes","no","yes_reply","no_reply"):
                app.logger.debug("Ignored stray yes/no from %s mid-flow", user_id)
                continue

            # current question
            step = state["step"]
            if step < 0 or step >= len(QUESTIONS):
                send_whatsapp_text(user_id, "Unexpected state. Please say Hi to restart.")
                user_states.pop(user_id, None)
                continue

            q = QUESTIONS[step]
            key = q["key"]
            val = incoming

            # simple validators
            valid = True
            if q["type"] == "number":
                try:
                    float(val)
                except:
                    valid = False
            elif q["type"] == "email":
                valid = bool(val and "@" in val and "." in val)
            elif q["type"] == "link":
                valid = val.startswith("http://") or val.startswith("https://")

            if not valid:
                send_whatsapp_text(user_id, f"❌ Please enter a valid {q['type']}.")
                send_whatsapp_text(user_id, q["text"])
                continue

            # Save
            state["answers"][key] = val if q.get("type") != "phone" or val.lower() != "skip" else ""
            state["step"] = step + 1

            if state["step"] < len(QUESTIONS):
                send_whatsapp_text(user_id, QUESTIONS[state["step"]]["text"])
                continue

            # All done: compute valuation, save and email
            a = state["answers"]
            vmin, vmax, mid = compute_valuation(a.get("profit") or a.get("annual_profit"), a.get("revenue_type") or a.get("revenue_type",""))
            if vmin == vmax:
                val_text = f"${vmin:,.2f}"
            else:
                val_text = f"${vmin:,.2f} to ${vmax:,.2f}"

            # prepare email content
            name = a.get("name","there")
            plain = f"Hi {name},\n\nYour app valuation estimate is: {val_text}\n\nRegards,\nKalagato"
            html = f"<p>Hi {name},</p><p>Your app valuation estimate is: <strong>{val_text}</strong></p><p>Regards,<br/>Kalagato</p>"

            email_ok = send_valuation_email(a.get("email",""), name, plain, html)

            saved = save_to_sheet(user_id, a, vmin, vmax, mid)

            if email_ok:
                send_whatsapp_text(user_id, f"✅ Thank you {name}! We've sent your valuation ({val_text}) to your email.")
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but couldn't send the email automatically. Please contact aman@kalagato.co if needed.")

            user_states.pop(user_id, None)

    return "OK", 200

# simple health check
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

if __name__ == "__main__":
    # optional warm-up attempt for sheet init (non-fatal)
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Initial sheets init failed (continuing): %s", e)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
