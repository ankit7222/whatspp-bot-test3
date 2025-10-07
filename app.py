# app.py
import os
import json
import re
import time
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

app = Flask(__name__)

# -----------------------------
# ENV
# -----------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")
VALUATION_CC = os.getenv("VALUATION_CC", "")

PORT = int(os.getenv("PORT", 5000))

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

GREETING_TEXT = "Hi, I am Kalagato AI Agent. Are you interested in selling your app?"
NO_RESPONSE_TEXT = "Thanks — if you have any queries contact us on aman@kalagato.co"
THANK_YOU_TEXT = "✅ Thank you! Your responses have been saved."

# Debounce / dedup
OUTGOING_DEBOUNCE_SECONDS = 1
MAX_PROCESSED_IDS = 5000

# -----------------------------
# Google Sheets lazy init
# -----------------------------
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
            creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
            creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
        _gs_client = gspread.authorize(creds)
        spreadsheet = _gs_client.open_by_key(SHEET_ID)
        try:
            _worksheet = spreadsheet.worksheet(SHEET_NAME)
        except WorksheetNotFound:
            _worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="20")
            headers = [
                "Timestamp", "User ID", "Listing", "App Store Link", "Play Store Link",
                "Last 12m Revenue", "Last 12m Profit", "Last 12m Spends", "Monthly Profit",
                "Revenue Sources", "Email", "Phone",
                "Valuation Min", "Valuation Max", "Valuation Mid", "CCs"
            ]
            _worksheet.insert_row(headers, index=1)
        app.logger.info("Google Sheets initialized.")
        _gs_init_error = None
        return _worksheet
    except Exception as e:
        _gs_init_error = f"Google Sheets init error: {e}"
        app.logger.error(_gs_init_error)
        return None

# -----------------------------
# In-memory state and dedup
# -----------------------------
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

# -----------------------------
# Helpers to send WhatsApp
# -----------------------------
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
        app.logger.debug("WhatsApp disabled - not sending: %s", text)
        return
    if not _can_send_for_user(to, text):
        app.logger.debug("Debounced outgoing: %s", text)
        return
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp send: %s %s", resp.status_code, (resp.text or "")[:200])
        _record_outgoing(to, text)
    except Exception as e:
        app.logger.error("WhatsApp send error: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp disabled - not sending buttons")
        return
    if not _can_send_for_user(to, text):
        app.logger.debug("Debounced outgoing buttons")
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
        app.logger.debug("WhatsApp buttons: %s %s", resp.status_code, (resp.text or "")[:200])
        _record_outgoing(to, text)
    except Exception as e:
        app.logger.error("WhatsApp buttons error: %s", e)

# -----------------------------
# Validators and parsers
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
    parts = re.split(r"[,\s]+", str(val).strip().lower())
    out = []
    for p in parts:
        if p in choices_map:
            out.append(choices_map[p])
        else:
            for v in choices_map.values():
                if p == v.lower():
                    out.append(v)
    # dedupe preserving order
    res = []
    for x in out:
        if x not in res:
            res.append(x)
    return res

# -----------------------------
# Valuation logic (same)
# -----------------------------
def compute_valuation(profit_value, revenue_type_label):
    try:
        profit_num = float(profit_value) if profit_value not in (None, "") else 0.0
    except:
        profit_num = 0.0
    rt = (revenue_type_label or "").lower()
    if profit_num <= 0 or profit_num < 1000:
        return 1000.0, 1000.0, 1000.0
    if "ad" in rt:
        vmin = profit_num * 1.0
        vmax = profit_num * 1.7
        return vmin, vmax, (vmin + vmax) / 2.0
    if "subscription" in rt or "sub" in rt:
        vmin = profit_num * 1.5
        vmax = profit_num * 2.3
        return vmin, vmax, (vmin + vmax) / 2.0
    # default
    est = profit_num * 2.5
    return est, est, est

# -----------------------------
# Email send
# -----------------------------
def send_valuation_email(to_email, to_name, plain_text, full_html, cc_list=None):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        app.logger.warning("Gmail not configured; skipping email send.")
        return False
    env_cc_list = [e.strip() for e in (VALUATION_CC or "").split(",") if e.strip()]
    param_cc_list = cc_list or []
    cc_all = []
    for e in env_cc_list + param_cc_list:
        if e and e not in cc_all:
            cc_all.append(e)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your App Valuation Estimate"
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
def save_to_sheet(user_id, answers, vmin, vmax, mid, cc_list):
    ws = try_init_gs()
    if ws is None:
        app.logger.warning("Skipping sheet save (sheets not ready): %s", _gs_init_error)
        return
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,
        user_id,
        answers.get("listing", ""),
        answers.get("app_store_link", ""),
        answers.get("play_store_link", ""),
        answers.get("revenue", ""),
        answers.get("profit", ""),
        answers.get("spends", ""),
        answers.get("monthly_profit", ""),
        answers.get("revenue_sources", ""),
        answers.get("email", ""),
        answers.get("phone", ""),
        vmin, vmax, mid,
        ", ".join(cc_list or [])
    ]
    try:
        ws.append_row(row)
    except Exception as e:
        app.logger.error("Failed to append row to sheet: %s", e)

# -----------------------------
# Webhook handler
# -----------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verify token", 403

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

            # Dedup by msg id/timestamp
            msg_id = msg.get("id") or msg.get("message_id") or str(msg.get("timestamp"))
            if msg_id in processed_msg_ids_set:
                app.logger.debug("Duplicate message ignored: %s", msg_id)
                continue
            mark_message_processed(msg_id)

            user_id = msg.get("from")
            text_body = msg.get("text", {}).get("body", "").strip()
            button_id = msg.get("interactive", {}).get("button_reply", {}).get("id")

            incoming = button_id if button_id else (text_body if text_body else None)
            if not incoming:
                # nothing to do
                continue

            # Ensure user state exists
            state = user_states.get(user_id)
            if not state:
                # New session -> send greeting with Yes/No
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                user_states[user_id] = {"step": -1, "answers": {}, "started_at": datetime.utcnow().isoformat(), "last_outgoing": None, "completed_at": None}
                continue

            # handle initial Yes/No (step -1)
            if state["step"] == -1:
                resp = str(incoming).strip().lower()
                if resp in ("no", "no_reply", "no_response"):
                    send_whatsapp_text(user_id, NO_RESPONSE_TEXT)
                    user_states.pop(user_id, None)
                    continue
                if resp in ("yes", "yes_reply", "yes_response"):
                    # ask listing
                    send_whatsapp_buttons(user_id, "Is your app listed on App Store, Play Store, or Both?", ["App Store", "Play Store", "Both"])
                    state["step"] = -2  # waiting listing
                    continue
                # else re-prompt
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                continue

            # handle listing selection (step -2)
            if state["step"] == -2:
                resp = str(incoming).strip().lower()
                listing = None
                if resp in ("app_store", "app store", "appstore", "app_store_reply"):
                    listing = "App Store"
                elif resp in ("play_store", "play store", "playstore", "play_store_reply"):
                    listing = "Play Store"
                elif resp in ("both", "both_reply"):
                    listing = "Both"
                else:
                    # allow typed variants
                    if "app store" in resp or "apps.apple.com" in resp:
                        listing = "App Store"
                    elif "play.google" in resp or "play store" in resp:
                        listing = "Play Store"
                if not listing:
                    send_whatsapp_buttons(user_id, "Please choose one: App Store, Play Store, or Both", ["App Store", "Play Store", "Both"])
                    continue

                # build dynamic questions based on listing
                qlist = []
                # skip app name per your request
                if listing in ("App Store", "Both"):
                    qlist.append({"key": "app_store_link", "text": "Please provide the App Store link (https://apps.apple.com/...)", "type": "link", "required": True})
                if listing in ("Play Store", "Both"):
                    qlist.append({"key": "play_store_link", "text": "Please provide the Play Store link (https://play.google.com/...)", "type": "link", "required": True})
                # core numeric questions
                qlist.extend([
                    {"key": "revenue", "text": "Last 12 months revenue (numbers only, USD)", "type": "number", "required": True},
                    {"key": "profit", "text": "Last 12 months profit (numbers only, USD)", "type": "number", "required": True},
                    {"key": "spends", "text": "Last 12 months spends (numbers only, USD)", "type": "number", "required": True},
                    {"key": "monthly_profit", "text": "Monthly profit (numbers only, USD)", "type": "number", "required": True},
                    {"key": "revenue_sources", "text": "Revenue sources (reply numbers separated by comma):\n1. IAP\n2. Subscription\n3. Ad", "type": "multi_choice", "choices": {"1":"IAP","2":"Subscription","3":"Ad"}, "required": True},
                    {"key": "email", "text": "Your email address (we will send valuation there)", "type": "email", "required": True},
                    {"key": "phone", "text": "Phone number (optional). Reply 'skip' to skip.", "type": "phone", "required": False}
                ])
                state["answers"]["listing"] = listing
                state["questions"] = qlist
                state["step"] = 0
                # ask first question
                send_whatsapp_text(user_id, state["questions"][0]["text"])
                continue

            # mid-flow: state["questions"] must exist
            questions = state.get("questions", [])
            step_index = state.get("step", 0)
            if step_index >= len(questions):
                # defensive: nothing to ask
                send_whatsapp_text(user_id, "Thanks — processing your responses.")
                user_states.pop(user_id, None)
                continue

            current_q = questions[step_index]
            key = current_q["key"]
            qtype = current_q.get("type", "text")
            val = str(incoming).strip()

            # allow skip for optional phone
            if qtype == "phone" and val.lower() == "skip":
                val = ""

            # validate
            valid = True
            err = None
            if qtype == "number":
                if not is_number(val):
                    valid = False; err = "❌ Please send a numeric value (numbers only)."
            elif qtype == "link":
                if not is_valid_link(val):
                    valid = False; err = "❌ Please send a valid URL starting with http:// or https://"
                else:
                    # additional check: appstore vs playstore format to avoid placing wrong link
                    if key == "app_store_link" and "apps.apple.com" not in val:
                        # but accept if it still starts with https
                        # more strict: require apps.apple.com
                        err = "❌ Please provide the App Store URL (must include apps.apple.com)."
                        valid = False
                    if key == "play_store_link" and "play.google" not in val and "play.google" not in val:
                        err = "❌ Please provide the Play Store URL (must include play.google.com)."
                        valid = False
            elif qtype == "email":
                if not is_valid_email(val):
                    valid = False; err = "❌ Please send a valid email address."
            elif qtype == "multi_choice":
                sel = parse_multi_choice(val, current_q.get("choices", {}))
                if not sel:
                    valid = False; err = "❌ Please reply with numbers like '1' or '1,3'."
                else:
                    val = ",".join(sel)

            if not valid:
                send_whatsapp_text(user_id, err)
                send_whatsapp_text(user_id, current_q["text"])
                continue

            # save and advance
            state["answers"][key] = val
            state["step"] = step_index + 1

            if state["step"] < len(state["questions"]):
                send_whatsapp_text(user_id, state["questions"][state["step"]]["text"])
                continue

            # finished: compute valuation, save, email
            a = state["answers"]
            vmin, vmax, mid = compute_valuation(a.get("profit"), a.get("revenue_sources", ""))

            user_ccs = []  # not collected here; optional extension
            save_to_sheet(user_id, a, vmin, vmax, mid, user_ccs)

            # email body
            safe_name = a.get("email", "there")
            if vmin == vmax:
                val_html = f'<h2>${vmin:,.2f}</h2>'
                plain = f"Your App Valuation Estimate is: ${vmin:,.2f}"
            else:
                val_html = f'<h2>${vmin:,.2f} to ${vmax:,.2f}</h2>'
                plain = f"Your App Valuation Estimate is: ${vmin:,.2f} to ${vmax:,.2f}"

            html = f"<div><p>Hi,</p><p>Based on the details you provided, estimated valuation: {val_html}</p><p>Regards, Kalagato</p></div>"

            sent = send_valuation_email(a.get("email"), safe_name, plain, html, cc_list=user_ccs)
            if sent:
                send_whatsapp_text(user_id, THANK_YOU_TEXT)
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. " + THANK_YOU_TEXT)

            # cleanup
            state["completed_at"] = time.time()
            user_states.pop(user_id, None)

    return "OK", 200

# -----------------------------
# Health
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# -----------------------------
# Run (for local dev)
# -----------------------------
if __name__ == "__main__":
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Sheets init failed at startup (continuing): %s", e)
    app.run(host="0.0.0.0", port=PORT)
