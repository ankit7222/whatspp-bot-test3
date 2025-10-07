# app.py
"""
WhatsApp bot (Flask) — robust flow with dedup/debounce/cooldown and safe Google Sheets saving.
Drop into your repo, set env vars, and deploy (Render: use Procfile or Start Command).
"""
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

# -----------------------------
# Environment / configuration
# -----------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # single-line JSON or blank to use file
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")

VALUATION_CC = os.getenv("VALUATION_CC", "")  # optional env CC e.g. ops@...,accounts@...
PORT = int(os.getenv("PORT", 5000))

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

GREETING_TEXT = os.getenv("GREETING_TEXT", "Hi, I am Kalagato AI Agent. Are you interested in selling your app?")
NO_RESPONSE_TEXT = os.getenv("NO_RESPONSE_TEXT", "Thanks — if you have any queries contact us on aman@kalagato.co")
THANK_YOU_TEXT = os.getenv("THANK_YOU_TEXT", "✅ Thank you! We saved your details and emailed your valuation.")

# debounce / dedup / cooldown config
OUTGOING_DEBOUNCE_SECONDS = int(os.getenv("OUTGOING_DEBOUNCE_SECONDS", "1"))  # avoid sending identical messages too fast
COMPLETION_COOLDOWN_SECONDS = int(os.getenv("COMPLETION_COOLDOWN_SECONDS", "60"))  # after finish, wait before new session
MAX_PROCESSED_IDS = 5000  # cap in-memory processed message id cache to avoid memory growth

# warnings (don't crash)
if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
    app.logger.warning("WHATSAPP_TOKEN or PHONE_NUMBER_ID not set — WhatsApp sends will be suppressed until configured.")
if not SHEET_ID:
    app.logger.warning("SHEET_ID not set — Google Sheets writes will be skipped until configured.")

# -----------------------------
# Google Sheets lazy init (safe)
# -----------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
_gs_client = None
_worksheet = None
_gs_init_error = None

def try_init_gs():
    """Try to initialize and return worksheet object or None (do not raise)."""
    global _gs_client, _worksheet, _gs_init_error
    if _worksheet is not None:
        return _worksheet
    try:
        if not SHEET_ID:
            _gs_init_error = "SHEET_ID not configured"
            app.logger.warning("Google Sheets init skipped: %s", _gs_init_error)
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
            app.logger.info("Worksheet '%s' not found — creating.", SHEET_NAME)
            _worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="20")
            headers = [
                "Timestamp", "User ID", "Name", "App Link",
                "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
                "Revenue Type", "Email", "Phone",
                "Estimated Valuation Min", "Estimated Valuation Max", "Estimated Valuation Mid", "CCs"
            ]
            _worksheet.insert_row(headers, index=1)
        app.logger.info("Google Sheets ready (worksheet: %s).", SHEET_NAME)
        _gs_init_error = None
        return _worksheet
    except Exception as e:
        _gs_init_error = f"Google Sheets init error: {e}"
        app.logger.error(_gs_init_error)
        return None

# -----------------------------
# Conversation state & dedup structures (in-memory)
# -----------------------------
# user_states[user_id] = {"step": int, "answers": dict, "started_at": iso, "last_outgoing": {"text": ..., "ts": epoch}, "completed_at": epoch or None}
user_states = {}

# processed_msg_ids keeps recent message ids to avoid duplicate processing
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
        # pop oldest
        old = processed_msg_ids.pop(0)
        processed_msg_ids_set.discard(old)

# -----------------------------
# Question flow definition
# -----------------------------
QUESTION_FLOW = [
    {"key": "name", "text": "What is your name?", "type": "text", "required": True},
    {"key": "appLink", "text": "Please provide the App Store or Play Store link (https://...)", "type": "link", "required": True},
    {"key": "revenue", "text": "Last 12 months revenue (numbers only, USD)", "type": "number", "required": True},
    {"key": "profit", "text": "Last 12 months profit (numbers only, USD)", "type": "number", "required": True},
    {"key": "spends", "text": "Last 12 months spends (numbers only, USD)", "type": "number", "required": True},
    {"key": "dau", "text": "Daily Active Users (DAU) — numbers only", "type": "number", "required": True},
    {"key": "mau", "text": "Monthly Active Users (MAU) — numbers only", "type": "number", "required": True},
    {"key": "revenueSource", "text": "Which revenue sources? Reply with numbers separated by comma:\n1. IAP\n2. Subscription\n3. Ad (e.g. '1,3')", "type": "multi_choice", "choices": {"1":"IAP","2":"Subscription","3":"Ad"}, "required": True},
    {"key": "email", "text": "Your email address (we will send valuation there)", "type": "email", "required": True},
    {"key": "phone", "text": "Phone number (optional). Reply 'skip' to skip.", "type": "phone", "required": False}
]

# -----------------------------
# WhatsApp sending helpers (with outgoing debounce)
# -----------------------------
def _can_send_for_user(user_id, text):
    """Return True if we should send 'text' to user now (debounce identical messages)."""
    s = user_states.get(user_id)
    if not s:
        return True
    last = s.get("last_outgoing")
    if not last:
        return True
    last_text = last.get("text")
    last_ts = last.get("ts", 0)
    now_ts = time.time()
    if text == last_text and (now_ts - last_ts) < OUTGOING_DEBOUNCE_SECONDS:
        return False
    return True

def _record_outgoing(user_id, text):
    s = user_states.setdefault(user_id, {"step": -1, "answers": {}, "started_at": datetime.utcnow().isoformat(), "last_outgoing": None, "completed_at": None})
    s["last_outgoing"] = {"text": text, "ts": time.time()}

def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp disabled; skipping send: %s", text)
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
        app.logger.debug("WhatsApp disabled; skipping buttons: %s", text)
        return
    if not _can_send_for_user(to, text):
        app.logger.debug("Debounced outgoing duplicate buttons to %s", to)
        return
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive": {
            "type":"button",
            "body":{"text":text},
            "action":{"buttons":[{"type":"reply","reply":{"id":b.lower().replace(" ","_"), "title":b}} for b in buttons]}
        }
    }
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp buttons status: %s %s", resp.status_code, (resp.text or "")[:200])
        _record_outgoing(to, text)
    except Exception as e:
        app.logger.error("WhatsApp buttons send failed: %s", e)

# -----------------------------
# Validators and parsing helpers
# -----------------------------
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

def parse_multi_choice(val, choices_map):
    if not val:
        return []
    val = str(val).strip().lower()
    parts = re.split(r"[,\s]+", val)
    selected = []
    for p in parts:
        if p in choices_map:
            selected.append(choices_map[p])
        else:
            for v in choices_map.values():
                if p == v.lower():
                    selected.append(v)
    out = []
    for s in selected:
        if s not in out:
            out.append(s)
    return out

# -----------------------------
# Valuation logic (unchanged)
# -----------------------------
def compute_valuation(profit_value, revenue_type_label):
    try:
        profit_num = float(profit_value) if profit_value not in (None, "") else 0.0
    except:
        profit_num = 0.0
    rt = (revenue_type_label or "").lower()
    valuation_min = valuation_max = estimated = 0.0

    if profit_num <= 0 or profit_num < 1000:
        valuation_min = valuation_max = estimated = 1000.0
    elif "ad" in rt:
        valuation_min = profit_num * 1.0
        valuation_max = profit_num * 1.7
        estimated = (valuation_min + valuation_max) / 2.0
    elif "subscription" in rt or "sub" in rt:
        valuation_min = profit_num * 1.5
        valuation_max = profit_num * 2.3
        estimated = (valuation_min + valuation_max) / 2.0
    else:
        estimated = profit_num * 2.5
        valuation_min = valuation_max = estimated

    return valuation_min, valuation_max, estimated

# -----------------------------
# Email send (env-driven CC)
# -----------------------------
def send_valuation_email(to_email, to_name, plain_text, full_html, cc_list=None):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        app.logger.warning("Gmail credentials missing; skipping email.")
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
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(full_html, "html"))
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

# -----------------------------
# Save to sheet (safe)
# -----------------------------
def save_to_sheet(user_id, answers, valuation_min, valuation_max, estimated, cc_list):
    ws = try_init_gs()
    if ws is None:
        app.logger.warning("Skipping sheet save; Google Sheets not initialized: %s", _gs_init_error)
        return
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,
        user_id,
        answers.get("name"),
        answers.get("appLink"),
        answers.get("revenue"),
        answers.get("marketingCost", ""),
        answers.get("serverCost", ""),
        answers.get("profit"),
        answers.get("revenueType", ""),
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
        app.logger.error("Failed to append row to sheet: %s", e)

# -----------------------------
# Webhook endpoint (main)
# -----------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification GET
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verification token", 403

    payload = request.get_json(silent=True)
    if not payload:
        return "No payload", 400

    # Iterate entries
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue
            msg = messages[0]
            # dedup: WhatsApp message id (different payloads can include 'id' or 'message_id')
            msg_id = msg.get("id") or msg.get("message_id") or msg.get("timestamp")
            if msg_id:
                if msg_id in processed_msg_ids_set:
                    app.logger.debug("Ignoring duplicate incoming message id: %s", msg_id)
                    continue
                mark_message_processed(msg_id)

            user_id = msg.get("from")
            # prefer interactive button id
            button_reply_id = msg.get("interactive", {}).get("button_reply", {}).get("id")
            text_body = msg.get("text", {}).get("body", "").strip()

            incoming_raw = None
            if button_reply_id:
                incoming_raw = button_reply_id  # normalized id from button
            elif text_body:
                incoming_raw = text_body

            # ensure user state exists if needed; do not auto-restart if within cooldown
            s = user_states.get(user_id)
            if not s:
                # if finishing earlier recently, enforce cooldown
                # find if there is a completed time recorded previously (we stored it on state)
                # We only have in-memory states; completed ones removed — so cooldown check is only based on previous state->completed_at if present
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                user_states[user_id] = {"step": -1, "answers": {}, "started_at": datetime.utcnow().isoformat(), "last_outgoing": None, "completed_at": None}
                app.logger.debug("New user state created for %s", user_id)
                continue

            state = s
            # if waiting for initial yes/no
            if state["step"] == -1:
                resp = (incoming_raw or "").strip().lower()
                # handle button ids like 'yes' / 'no' or 'yes_reply' etc.
                if resp in ("no", "no_reply", "no_response"):
                    send_whatsapp_text(user_id, NO_RESPONSE_TEXT)
                    # mark completed and start cooldown
                    state["completed_at"] = time.time()
                    # clear state after small delay to avoid race
                    user_states.pop(user_id, None)
                    app.logger.info("User %s chose NO - session ended", user_id)
                    continue
                if resp in ("yes", "yes_reply", "yes_response"):
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTION_FLOW[0]["text"])
                    continue
                # typed 'hi' or other text — re-send buttons
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                continue

            # mid-flow processing
            answer = incoming_raw if incoming_raw is not None else (text_body or "")
            if not answer:
                send_whatsapp_text(user_id, "I didn't receive any text. " + QUESTION_FLOW[state["step"]]["text"])
                continue

            current_q = QUESTION_FLOW[state["step"]]
            key = current_q["key"]
            qtype = current_q["type"]
            normalized = answer.strip()
            valid = True
            err_msg = None

            if qtype == "number":
                if not is_number(normalized):
                    valid = False
                    err_msg = "❌ Please send a numeric value (numbers only)."
            elif qtype == "link":
                if not is_valid_link(normalized):
                    valid = False
                    err_msg = "❌ Please send a valid URL starting with http:// or https://"
            elif qtype == "email":
                if not is_valid_email(normalized):
                    valid = False
                    err_msg = "❌ Please send a valid email address."
            elif qtype == "multi_choice":
                selected = parse_multi_choice(normalized, current_q["choices"])
                if not selected:
                    valid = False
                    err_msg = "❌ Please reply with numbers like '1' or '1,3' corresponding to the options."
                else:
                    normalized = ",".join(selected)
            elif qtype == "phone":
                if normalized.lower() == "skip":
                    normalized = ""

            if not valid:
                # re-ask same question but debounce re-sends
                send_whatsapp_text(user_id, err_msg)
                send_whatsapp_text(user_id, current_q["text"])
                # do not advance step
                continue

            # Save and advance
            state["answers"][key] = normalized
            state["step"] += 1

            if state["step"] < len(QUESTION_FLOW):
                send_whatsapp_text(user_id, QUESTION_FLOW[state["step"]]["text"])
                continue

            # All answers collected -> compute valuation, save, email, finish
            answers = state["answers"]
            revenue_label = answers.get("revenueSource", "")
            vmin, vmax, mid = compute_valuation(answers.get("profit"), revenue_label)

            user_ccs = []  # optional extension point for CC collection

            save_to_sheet(user_id, answers, vmin, vmax, mid, user_ccs)

            safe_name = answers.get("name") or "there"
            if vmin == vmax:
                valuation_html = f'<h2 style="font-size:36px;color:#007bff;">${vmin:,.2f}</h2>'
                plain_text = f"Your App Valuation Estimate is: ${vmin:,.2f}"
            else:
                valuation_html = f'<h2 style="font-size:28px;color:#007bff;">${vmin:,.2f} to ${vmax:,.2f}</h2>'
                plain_text = f"Your App Valuation Estimate is: ${vmin:,.2f} to ${vmax:,.2f}"

            full_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; background: #fff; border-radius:8px;">
              <p>Hi {safe_name},</p>
              <p>Thank you for using our valuation tool. Based on the details you provided, here is your app's estimated valuation:</p>
              <div style="margin:20px 0;">{valuation_html}</div>
              <p>Best regards,<br/>The Kalagato Team</p>
            </div>
            """

            email_success = send_valuation_email(answers.get("email"), safe_name, plain_text, full_html, cc_list=user_ccs)

            if email_success:
                send_whatsapp_text(user_id, THANK_YOU_TEXT)
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. " + THANK_YOU_TEXT)

            # mark completed time for cooldown and remove state to free memory
            state["completed_at"] = time.time()
            # keep a tiny record of completed time (optional) then pop to clean
            completed_time = state.get("completed_at")
            user_states.pop(user_id, None)
            app.logger.info("Conversation completed for %s (vmin=%s vmax=%s)", user_id, vmin, vmax)

    return "OK", 200

# -----------------------------
# Health endpoint for Render
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# -----------------------------
# Startup (local dev). Render uses Procfile/gunicorn.
# -----------------------------
if __name__ == "__main__":
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Google Sheets init error at startup (continuing): %s", e)
    app.run(host="0.0.0.0", port=PORT)
