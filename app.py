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

# -----------------------------
# Environment / Config
# -----------------------------
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # single-line JSON OR leave blank to use service_account.json
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")

VALUATION_CC = os.getenv("VALUATION_CC", "")  # optional env driven CC

PORT = int(os.getenv("PORT", 5000))
WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

# friendly messages
GREETING_TEXT = os.getenv("GREETING_TEXT", "Hi, I am Kalagato AI Agent. Are you interested in selling your app?")
NO_RESPONSE_TEXT = os.getenv("NO_RESPONSE_TEXT", "Thanks — if you have any queries contact us on aman@kalagato.co")
THANK_YOU_TEXT = os.getenv("THANK_YOU_TEXT", "✅ Thank you! We saved your details and emailed your valuation.")

# warn but don't crash on missing config
if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
    app.logger.warning("WHATSAPP_TOKEN or PHONE_NUMBER_ID not set — WhatsApp sends will be skipped until configured.")
if not SHEET_ID:
    app.logger.warning("SHEET_ID not set — Google Sheets writes will be skipped until configured.")

# -----------------------------
# Lazy Google Sheets initialization (safe for Render)
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
            app.logger.warning("Google Sheets skipped: %s", _gs_init_error)
            return None

        if GOOGLE_SHEETS_CREDENTIALS:
            try:
                creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
                creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
            except Exception as e:
                _gs_init_error = f"Failed to parse GOOGLE_SHEETS_CREDENTIALS: {e}"
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
# Conversation flow (in-memory state)
# -----------------------------
# Structure per-user: { "step": int, "answers": {key: value}, "started_at": timestamp }
user_states = {}

# Questions in order (simple linear flow)
QUESTION_FLOW = [
    {"key": "name", "text": "What is your name?", "type": "text", "required": True},
    {"key": "appLink", "text": "Please provide the App Store or Play Store link (https://...)", "type": "link", "required": True},
    {"key": "revenue", "text": "Last 12 months revenue (numbers only, USD)", "type": "number", "required": True},
    {"key": "profit", "text": "Last 12 months profit (numbers only, USD)", "type": "number", "required": True},
    {"key": "spends", "text": "Last 12 months spends (numbers only, USD)", "type": "number", "required": True},
    {"key": "dau", "text": "Daily Active Users (DAU) — numbers only", "type": "number", "required": True},
    {"key": "mau", "text": "Monthly Active Users (MAU) — numbers only", "type": "number", "required": True},
    {"key": "revenueSource", "text": "Which revenue sources? Reply with numbers separated by comma:\n1. IAP\n2. Subscription\n3. Ad Revenue\n(e.g. '1,3' or '2')", "type": "multi_choice", "choices": {"1":"IAP","2":"Subscription","3":"Ad"}, "required": True},
    {"key": "email", "text": "Your email address (we will send valuation there)", "type": "email", "required": True},
    {"key": "phone", "text": "Phone number (optional). Reply 'skip' to skip.", "type": "phone", "required": False}
]

# -----------------------------
# Helpers: WhatsApp senders
# -----------------------------
def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp disabled; message not sent: %s", text)
        return
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp send status: %s %s", resp.status_code, (resp.text or "")[:200])
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    # buttons is a list of titles e.g. ["Yes","No"]
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp buttons suppressed: %s", text)
        return
    payload = {
        "messaging_product":"whatsapp",
        "to":to,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":text},
            "action":{
                "buttons":[
                    {"type":"reply","reply":{"id":b.lower().replace(" ","_"), "title":b}} for b in buttons
                ]
            }
        }
    }
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=10)
        app.logger.debug("WhatsApp buttons status: %s %s", resp.status_code, (resp.text or "")[:200])
    except Exception as e:
        app.logger.error("WhatsApp buttons send failed: %s", e)

# -----------------------------
# Validators & helpers
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
    # val may be "1,3" or "1" or "IAP" etc.
    if not val:
        return []
    val = str(val).strip().lower()
    parts = re.split(r"[,\s]+", val)
    selected = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p in choices_map:
            selected.append(choices_map[p])
        else:
            # maybe it's already a name
            for v in choices_map.values():
                if p == v.lower():
                    selected.append(v)
    # dedupe
    out = []
    for s in selected:
        if s not in out:
            out.append(s)
    return out

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

# -----------------------------
# Valuation logic (same rules)
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
    elif "iap" in rt or "other" in rt:
        valuation_min = profit_num * 1.5
        valuation_max = profit_num * 2.3
        estimated = (valuation_min + valuation_max) / 2.0
    else:
        estimated = profit_num * 2.5
        valuation_min = valuation_max = estimated

    return valuation_min, valuation_max, estimated

# -----------------------------
# Email sending (env-driven CC)
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
        app.logger.error("Failed to send email: %s", e)
        return False

# -----------------------------
# Save to Google Sheet (safe)
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
        answers.get("marketingCost", ""),  # optional placeholder if not asked
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
# Webhook handling
# -----------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # GET -> verification handshake for Meta webhook
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verification token", 403

    # Parse payload
    payload = request.get_json(silent=True)
    if not payload:
        return "No payload", 400

    # iterate typical WhatsApp cloud structure
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue
            msg = messages[0]
            user_id = msg.get("from")
            # Prefer interactive button replies if present
            button_reply_id = msg.get("interactive", {}).get("button_reply", {}).get("id")
            text_body = msg.get("text", {}).get("body", "").strip()

            # Map an incoming reply ID or typed text to normalized input
            incoming = None
            if button_reply_id:
                incoming = button_reply_id  # e.g., "yes" or "no" or "app_store" depending on your buttons
            elif text_body:
                incoming = text_body

            # If user hasn't started flow -> send greeting and set a starter state
            if user_id not in user_states:
                # Start state waiting for Yes/No selection
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                user_states[user_id] = {"step": -1, "answers": {}, "started_at": datetime.utcnow().isoformat()}
                app.logger.debug("Started state for user %s", user_id)
                continue

            # Retrieve state
            state = user_states[user_id]
            step = state["step"]

            # If waiting for initial yes/no (step == -1)
            if step == -1:
                # Accept either interactive yes/no or typed yes/no
                resp = (incoming or "").strip().lower()
                if resp in ("no", "no_reply", "no_reply".lower()):  # various possible ids
                    send_whatsapp_text(user_id, NO_RESPONSE_TEXT)
                    del user_states[user_id]
                    continue
                if resp in ("yes", "yes_reply", "yes_reply".lower()):
                    # proceed to first question
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTION_FLOW[0]["text"])
                    continue
                # If user typed something else while at greeting, ask again
                send_whatsapp_buttons(user_id, GREETING_TEXT, ["Yes", "No"])
                continue

            # If mid-flow, we should use incoming (prefer button id)
            answer = incoming if incoming is not None else text_body

            # Defensive: if empty message, ignore
            if not answer:
                send_whatsapp_text(user_id, "I didn't receive any text. " + QUESTION_FLOW[step]["text"])
                continue

            # Validate based on question type
            current_q = QUESTION_FLOW[step]
            key = current_q["key"]
            qtype = current_q["type"]
            valid = True
            err_msg = None
            normalized_answer = answer.strip()

            # For multi_choice, accept comma-separated numbers OR names
            if qtype == "number":
                if not is_number(normalized_answer):
                    valid = False
                    err_msg = "❌ Please send a numeric value (numbers only)."
            elif qtype == "link":
                if not is_valid_link(normalized_answer):
                    valid = False
                    err_msg = "❌ Please send a valid URL starting with http:// or https://"
            elif qtype == "email":
                if not is_valid_email(normalized_answer):
                    valid = False
                    err_msg = "❌ Please send a valid email address."
            elif qtype == "choice":
                # not used in current flow, kept for extension
                pass
            elif qtype == "multi_choice":
                selected = parse_multi_choice(normalized_answer, current_q["choices"])
                if not selected:
                    valid = False
                    err_msg = "❌ Please reply with numbers like '1' or '1,3' corresponding to the options."
                else:
                    # store a comma-separated string label for downstream valuation logic
                    normalized_answer = ",".join(selected)
            elif qtype == "phone":
                if normalized_answer.lower() == "skip":
                    normalized_answer = ""
                # no strict validation for phone
            elif qtype == "cc":
                # handled similarly to phone; keep raw and parse later
                pass

            if not valid:
                send_whatsapp_text(user_id, err_msg)
                # re-ask same question (do not advance)
                send_whatsapp_text(user_id, current_q["text"])
                continue

            # Save normalized answer and advance
            state["answers"][key] = normalized_answer
            state["step"] += 1

            # Ask next or finish
            if state["step"] < len(QUESTION_FLOW):
                send_whatsapp_text(user_id, QUESTION_FLOW[state["step"]]["text"])
                continue

            # Flow finished — compute valuation, save and email
            answers = state["answers"]
            # Compose revenueType label from multi_choice if provided
            revenue_source_label = answers.get("revenueSource", "")
            vmin, vmax, mid = compute_valuation(answers.get("profit"), revenue_source_label)

            # parse optional user CCs if present (we didn't collect CCs in this flow; left for extension)
            user_ccs = []  # empty by default

            # Save to sheet (safe - will skip if sheets not configured)
            save_to_sheet(user_id, answers, vmin, vmax, mid, user_ccs)

            # Prepare email body
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
              <p>Thanks for submitting your app details. Based on what you provided, here is an estimate:</p>
              <div style="margin:20px 0;">{valuation_html}</div>
              <p>Best regards,<br/>Kalagato Team</p>
            </div>
            """

            email_success = send_valuation_email(answers.get("email"), safe_name, plain_text, full_html, cc_list=user_ccs)

            if email_success:
                send_whatsapp_text(user_id, THANK_YOU_TEXT)
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. " + THANK_YOU_TEXT)

            # cleanup user state
            try:
                del user_states[user_id]
            except KeyError:
                pass

    return "OK", 200

# -----------------------------
# Health route for Render
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# -----------------------------
# Start (for local dev). Render uses Procfile/start command with gunicorn.
# -----------------------------
if __name__ == "__main__":
    # Try to init Sheets but allow startup even if it fails
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Initial Google Sheets init failed (continuing): %s", e)

    app.run(host="0.0.0.0", port=PORT)
