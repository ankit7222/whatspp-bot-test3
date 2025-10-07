import os
import json
import re
import time
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

app = Flask(__name__)

# -------------------------
# Environment / Config
# -------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # JSON string
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")
VALUATION_CC = os.getenv("VALUATION_CC", "")  # optional comma-separated

PORT = int(os.getenv("PORT", 5000))

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

SESSION_TIMEOUT = timedelta(minutes=15)
OUTGOING_DEBOUNCE_SECONDS = 1
MAX_PROCESSED_IDS = 5000

# -------------------------
# Safety warnings (no crash)
# -------------------------
if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
    app.logger.warning("WHATSAPP_TOKEN or PHONE_NUMBER_ID not set — WhatsApp sending will be skipped until configured.")
if not SHEET_ID:
    app.logger.warning("SHEET_ID not set — Google Sheets writes will be skipped until configured.")
if not GMAIL_USER or not GMAIL_APP_PASS:
    app.logger.warning("Gmail credentials not fully set — emails will fail until configured (GMAIL_USER & GMAIL_APP_PASS).")

# -------------------------
# Google Sheets lazy init
# -------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
_gs_client = None
_worksheet = None
_gs_init_error = None

def try_init_gs():
    global _gs_client, _worksheet, _gs_init_error
    if _worksheet is not None:
        return _worksheet
    try:
        if not SHEET_ID:
            _gs_init_error = "SHEET_ID not configured"
            app.logger.warning(_gs_init_error)
            return None

        if GOOGLE_SHEETS_CREDENTIALS:
            try:
                creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
                creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
            except Exception as e:
                _gs_init_error = f"Failed parsing GOOGLE_SHEETS_CREDENTIALS: {e}"
                app.logger.error(_gs_init_error)
                return None
        else:
            try:
                creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
            except Exception as e:
                _gs_init_error = f"service_account.json not found/invalid: {e}"
                app.logger.error(_gs_init_error)
                return None

        _gs_client = gspread.authorize(creds)
        spreadsheet = _gs_client.open_by_key(SHEET_ID)
        try:
            _worksheet = spreadsheet.worksheet(SHEET_NAME)
        except WorksheetNotFound:
            _worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="20")
            headers = [
                "Timestamp", "User ID", "Name", "App Link",
                "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
                "Revenue Type", "Email", "Phone", "Valuation Min", "Valuation Max", "Valuation Mid", "CCs"
            ]
            _worksheet.insert_row(headers, index=1)
        app.logger.info("Google Sheets initialized (worksheet: %s).", SHEET_NAME)
        _gs_init_error = None
        return _worksheet
    except Exception as e:
        _gs_init_error = f"Google Sheets init error: {e}"
        app.logger.error(_gs_init_error)
        return None

# -------------------------
# Conversation state + dedup
# -------------------------
user_states = {}
processed_msg_ids = []
processed_msg_ids_set = set()

def mark_message_processed(msg_id):
    global processed_msg_ids, processed_msg_ids_set
    if not msg_id:
        return
    if msg_id in processed_msg_ids_set:
        return
    processed_msg_ids.append(msg_id)
    processed_msg_ids_set.add(msg_id)
    if len(processed_msg_ids) > MAX_PROCESSED_IDS:
        old = processed_msg_ids.pop(0)
        processed_msg_ids_set.discard(old)

# -------------------------
# Questions (linear flow)
# -------------------------
QUESTIONS = [
    {"key": "name", "text": "What is your name?", "type": "text"},
    {"key": "app_link", "text": "Please provide your App Store or Play Store link (https://...)", "type": "link"},
    {"key": "revenue", "text": "What is your annual revenue (USD)?", "type": "number"},
    {"key": "marketing_cost", "text": "What is your annual marketing cost (USD)?", "type": "number"},
    {"key": "server_cost", "text": "What is your annual server cost (USD)?", "type": "number"},
    {"key": "profit", "text": "What is your annual profit (USD)?", "type": "number"},
    {"key": "revenue_type", "text": "Which revenue types? Reply using commas (Ad, Subscription, IAP). Example: Ad, IAP", "type": "text"},
    {"key": "email", "text": "Please share your email address (we will send valuation there)", "type": "email"},
    {"key": "phone", "text": "Phone number (optional). Reply 'skip' to skip.", "type": "phone", "required": False}
]

# -------------------------
# WhatsApp send helpers (debounced)
# -------------------------
def _can_send_for_user(user_id, text):
    s = user_states.get(user_id)
    if not s:
        return True
    last = s.get("last_outgoing")
    if not last:
        return True
    if last.get("text") == text and (time.time() - last.get("ts", 0)) < OUTGOING_DEBOUNCE_SECONDS:
        return False
    return True

def _record_outgoing(user_id, text):
    s = user_states.setdefault(user_id, {"step": -1, "answers": {}, "started_at": datetime.utcnow().isoformat(), "last_outgoing": None, "completed_at": None})
    s["last_outgoing"] = {"text": text, "ts": time.time()}

def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp disabled - skipping send: %s", text)
        return
    if not _can_send_for_user(to, text):
        app.logger.debug("Debounced outgoing duplicate to %s: %s", to, text)
        return
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp send status: %s %s", resp.status_code, (resp.text or "")[:200])
        _record_outgoing(to, text)
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp disabled - skipping buttons: %s", text)
        return
    if not _can_send_for_user(to, text):
        app.logger.debug("Debounced outgoing duplicate buttons to %s: %s", to, text)
        return
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":text},
            "action":{"buttons":[{"type":"reply","reply":{"id":b.lower().replace(' ','_'),"title":b}} for b in buttons]}
        }
    }
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp interactive status: %s %s", resp.status_code, (resp.text or "")[:200])
        _record_outgoing(to, text)
    except Exception as e:
        app.logger.error("WhatsApp send error: %s", e)

# -------------------------
# Validators / Parsers
# -------------------------
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

def parse_revenue_types(text):
    if not text:
        return []
    parts = re.split(r"[,\s]+", text.strip())
    out = []
    for p in parts:
        p = p.lower()
        if not p:
            continue
        if "ad" in p:
            out.append("Ad")
        elif "sub" in p:
            out.append("Subscription")
        elif "iap" in p:
            out.append("IAP")
    # dedupe
    res = []
    for x in out:
        if x not in res:
            res.append(x)
    return res

# -------------------------
# Valuation logic
# -------------------------
def compute_valuation(profit_value, revenue_type_text):
    try:
        profit_num = float(profit_value) if profit_value not in (None, "") else 0.0
    except:
        profit_num = 0.0
    rt_list = parse_revenue_types(revenue_type_text)
    if profit_num <= 0 or profit_num < 1000:
        return 1000.0, 1000.0, 1000.0
    if "Ad" in rt_list:
        vmin = profit_num * 1.0
        vmax = profit_num * 1.7
        mid = (vmin + vmax) / 2.0
    elif "Subscription" in rt_list:
        vmin = profit_num * 1.5
        vmax = profit_num * 2.3
        mid = (vmin + vmax) / 2.0
    elif "IAP" in rt_list:
        vmin = profit_num * 1.5
        vmax = profit_num * 2.0
        mid = (vmin + vmax) / 2.0
    else:
        mid = profit_num * 2.5
        vmin = vmax = mid
    return vmin, vmax, mid

# -------------------------
# Email sending via Gmail SMTP (App Password)
# -------------------------
def send_valuation_email(to_email, name, plain_text, html_content, cc_list=None):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        app.logger.warning("Gmail not configured; skipping email.")
        return False

    env_ccs = [e.strip() for e in (VALUATION_CC or "").split(",") if e.strip()]
    param_ccs = cc_list or []
    cc_all = []
    for e in env_ccs + param_ccs:
        if e and e not in cc_all:
            cc_all.append(e)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your App Valuation Estimate from Kalagato"
    msg["From"] = GMAIL_USER
    msg["To"] = to_email
    if cc_all:
        msg["Cc"] = ", ".join(cc_all)

    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_content, "html"))

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

# -------------------------
# Save to sheet (safe)
# -------------------------
def save_to_sheet(user_id, answers, vmin, vmax, mid, cc_list):
    ws = try_init_gs()
    if ws is None:
        app.logger.warning("Skipping sheet save: %s", _gs_init_error)
        return
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,
        user_id,
        answers.get("name", ""),
        answers.get("app_link", ""),
        answers.get("revenue", ""),
        answers.get("marketing_cost", ""),
        answers.get("server_cost", ""),
        answers.get("profit", ""),
        answers.get("revenue_type", ""),
        answers.get("email", ""),
        answers.get("phone", ""),
        f"{vmin:.2f}",
        f"{vmax:.2f}",
        f"{mid:.2f}",
        ", ".join(cc_list or [])
    ]
    try:
        ws.append_row(row)
    except Exception as e:
        app.logger.error("Failed to append row to sheet: %s", e)

# -------------------------
# Webhook endpoint
# -------------------------
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

            # dedup incoming
            msg_id = msg.get("id") or msg.get("message_id") or str(msg.get("timestamp"))
            if msg_id in processed_msg_ids_set:
                app.logger.debug("Ignored duplicate incoming id: %s", msg_id)
                continue
            mark_message_processed(msg_id)

            user_id = msg.get("from")
            button_reply_id = msg.get("interactive", {}).get("button_reply", {}).get("id", "")
            text_body = msg.get("text", {}).get("body", "").strip()
            incoming = (button_reply_id or text_body or "").strip()

            if not incoming:
                continue

            now = datetime.utcnow()
            # ensure user state
            if user_id not in user_states:
                user_states[user_id] = {
                    "step": -1,
                    "answers": {},
                    "session_started": False,
                    "last_active": now,
                    "last_outgoing": None,
                    "completed_at": None
                }
                send_whatsapp_buttons(user_id, "Hi, I am Kalagato AI Agent. Are you interested in selling your app?", ["Yes", "No"])
                continue

            state = user_states[user_id]
            step = state["step"]
            last_active = state.get("last_active", now)

            # session timeout
            if now - last_active > SESSION_TIMEOUT:
                user_states.pop(user_id, None)
                send_whatsapp_buttons(user_id, "👋 Session expired. Let's start over.\nAre you interested in selling your app?", ["Yes", "No"])
                continue

            state["last_active"] = now

            # greeting phase
            if step == -1:
                resp = incoming.lower()
                if resp in ("no", "no_reply", "no_response"):
                    send_whatsapp_text(user_id, "Thank you! If you have any queries, contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                if resp in ("yes", "yes_reply", "yes_response"):
                    state["session_started"] = True
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTIONS[0]["text"])
                    continue
                send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes", "No"])
                continue

            # ignore stray yes/no mid-flow
            if step >= 0 and incoming.lower() in ("yes", "no", "yes_reply", "no_reply"):
                app.logger.debug("Ignored stray yes/no from %s mid-flow", user_id)
                continue

            # current question
            questions = QUESTIONS
            if step < 0 or step >= len(questions):
                send_whatsapp_text(user_id, "Unexpected state — restarting. Please say 'Hi' to begin.")
                user_states.pop(user_id, None)
                continue

            current_q = questions[step]
            key = current_q["key"]
            qtype = current_q.get("type", "text")
            val = incoming

            # allow skip for phone
            if qtype == "phone" and val.lower() == "skip":
                val = ""

            valid = True
            if qtype == "number":
                if not is_number(val):
                    valid = False
            elif qtype == "link":
                if not is_valid_link(val):
                    valid = False
            elif qtype == "email":
                if not is_valid_email(val):
                    valid = False

            if not valid:
                send_whatsapp_text(user_id, f"❌ Please enter a valid {qtype}.")
                send_whatsapp_text(user_id, current_q["text"])
                continue

            # save answer and advance
            state["answers"][key] = val
            state["step"] = step + 1

            if state["step"] < len(questions):
                send_whatsapp_text(user_id, questions[state["step"]]["text"])
                continue

            # finished -> compute valuation, save, email
            answers = state["answers"]
            vmin, vmax, mid = compute_valuation(answers.get("profit"), answers.get("revenue_type"))
            # prepare email
            safe_name = answers.get("name", "there")
            if vmin == vmax:
                val_text = f"${vmin:,.2f}"
                plain = f"Hi {safe_name},\n\nYour app valuation estimate is {val_text}.\n\nRegards,\nKalagato Team"
                html = f"<p>Hi {safe_name},</p><p>Your app valuation estimate is <strong>{val_text}</strong>.</p><p>Regards,<br/>Kalagato Team</p>"
            else:
                val_text = f"${vmin:,.2f} to ${vmax:,.2f}"
                plain = f"Hi {safe_name},\n\nYour app valuation estimate is {val_text}.\n\nRegards,\nKalagato Team"
                html = f"<p>Hi {safe_name},</p><p>Your app valuation estimate is <strong>{val_text}</strong>.</p><p>Regards,<br/>Kalagato Team</p>"

            # send email (with CC if configured)
            user_ccs = []  # placeholder, no per-user CC collected currently
            email_ok = send_valuation_email(answers.get("email"), safe_name, plain, html, cc_list=user_ccs)

            # save to sheet
            save_to_sheet(user_id, answers, vmin, vmax, mid, user_ccs)

            if email_ok:
                send_whatsapp_text(user_id, f"✅ Thank you {safe_name}! We've sent your valuation ({val_text}) to your email.")
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. Please contact aman@kalagato.co if needed.")

            # cleanup
            state["completed_at"] = time.time()
            user_states.pop(user_id, None)

    return "OK", 200

# -------------------------
# Health endpoint
# -------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# -------------------------
# Run (local dev)
# -------------------------
if __name__ == "__main__":
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Initial Google Sheets init failed (continuing): %s", e)
    app.run(host="0.0.0.0", port=PORT)
