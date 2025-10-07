# app.py
import os
import json
import ssl
import smtplib
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

# -------------------------
# Configuration from env
# -------------------------
GOOGLE_CREDS_ENV = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "")
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1")

PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")

SUPABASE_FUNCTION_URL = os.getenv("SUPABASE_FUNCTION_URL")  # required: your Supabase Edge Function to send email
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")  # optional, if your function requires auth

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# -------------------------
# Google Sheets helpers (robust loader)
# -------------------------
_gs_client = None
_worksheet = None
_gs_init_error = None

def _load_service_account_info():
    """Load service account info from env var (JSON or filename) or fallback file."""
    raw = GOOGLE_CREDS_ENV
    # If env var set and looks like a filename, try load
    if raw:
        if raw.endswith(".json") and os.path.exists(raw):
            with open(raw, "r") as f:
                return json.load(f)
        # Try to parse as JSON (multi-line allowed)
        try:
            return json.loads(raw)
        except Exception as e:
            app.logger.warning("GOOGLE_CREDENTIALS parse failed: %s", e)

    # Fallback to service_account.json file in project root
    fallback = "service_account.json"
    if os.path.exists(fallback):
        with open(fallback, "r") as f:
            return json.load(f)

    raise Exception("No valid Google service account credentials found in env var or service_account.json file.")

def try_init_gs():
    """Initialize gspread worksheet and return worksheet or None."""
    global _gs_client, _worksheet, _gs_init_error
    if _worksheet is not None:
        return _worksheet
    try:
        info = _load_service_account_info()
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _gs_client = gspread.authorize(creds)

        if not SHEET_ID:
            _gs_init_error = "SHEET_ID not configured"
            app.logger.error(_gs_init_error)
            return None

        spreadsheet = _gs_client.open_by_key(SHEET_ID)
        try:
            _worksheet = spreadsheet.worksheet(SHEET_NAME)
        except WorksheetNotFound:
            # create with headers if needed
            _worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="2000", cols="20")
            headers = [
                "Timestamp", "User ID", "Name", "App Link",
                "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
                "Revenue Type", "Email", "Valuation"
            ]
            _worksheet.insert_row(headers, index=1)
        app.logger.info("Google Sheets initialized (sheet: %s)", SHEET_NAME)
        _gs_init_error = None
        return _worksheet
    except Exception as e:
        _gs_init_error = f"Google Sheets auth failed: {e}"
        app.logger.error(_gs_init_error)
        return None

# -------------------------
# WhatsApp helpers
# -------------------------
def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp not configured, skipping send.")
        return
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    try:
        r = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=8)
        app.logger.debug("WhatsApp send status %s %s", r.status_code, r.text[:200])
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp not configured, skipping send.")
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

# -------------------------
# Valuation logic
# -------------------------
def compute_valuation(profit_val, revenue_type_text):
    try:
        profit = float(profit_val) if profit_val not in (None,"") else 0.0
    except:
        profit = 0.0
    rt = (revenue_type_text or "").lower()
    if profit <= 0 or profit < 1000:
        return 1000.0, 1000.0, 1000.0
    if "ad" in rt:
        vmin = profit * 1.0; vmax = profit * 1.7
    elif "subscription" in rt or "sub" in rt:
        vmin = profit * 1.5; vmax = profit * 2.3
    elif "iap" in rt:
        vmin = profit * 1.5; vmax = profit * 2.0
    else:
        vmin = profit * 2.5; vmax = profit * 2.5
    return vmin, vmax, (vmin + vmax) / 2.0

# -------------------------
# Save to Google Sheet
# -------------------------
def save_to_sheet(user_id, answers, valuation_text):
    ws = try_init_gs()
    if ws is None:
        app.logger.warning("Skipping sheet save: %s", _gs_init_error)
        return False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        now,
        user_id,
        answers.get("name",""),
        answers.get("app_link",""),
        answers.get("annual_revenue",""),
        answers.get("marketing_cost",""),
        answers.get("server_cost",""),
        answers.get("annual_profit",""),
        answers.get("revenue_type",""),
        answers.get("email",""),
        valuation_text
    ]
    try:
        ws.append_row(row)
        app.logger.info("Saved row to Google Sheet for user %s", user_id)
        return True
    except Exception as e:
        app.logger.error("Failed to append row: %s", e)
        return False

# -------------------------
# Call Supabase Function to send email
# -------------------------
def call_supabase_send_email(payload: dict):
    """
    Calls the Supabase Edge Function that will send the valuation email.
    payload example: { "email": "...", "name": "...", "valuation": "...", "extra": {...} }
    """
    if not SUPABASE_FUNCTION_URL:
        app.logger.warning("SUPABASE_FUNCTION_URL not configured; cannot call function.")
        return False, "No function URL"
    headers = {"Content-Type":"application/json"}
    if SUPABASE_API_KEY:
        headers["apiKey"] = SUPABASE_API_KEY
        headers["Authorization"] = f"Bearer {SUPABASE_API_KEY}"
    try:
        r = requests.post(SUPABASE_FUNCTION_URL, headers=headers, json=payload, timeout=12)
        if r.status_code in (200, 201, 202):
            app.logger.info("Supabase function called successfully: %s", r.status_code)
            return True, r.text
        else:
            app.logger.error("Supabase function error %s: %s", r.status_code, r.text)
            return False, r.text
    except Exception as e:
        app.logger.error("Supabase function call failed: %s", e)
        return False, str(e)

# -------------------------
# Conversation flow (no phone question)
# -------------------------
user_states = {}
processed_msg_ids = set()
SESSION_TIMEOUT_SECONDS = 15 * 60

QUESTIONS = [
    {"key":"name","text":"What is your name?","type":"text"},
    {"key":"app_link","text":"Please provide your App Store or Play Store link (https://...)","type":"link"},
    {"key":"annual_revenue","text":"What is your annual revenue (USD)?","type":"number"},
    {"key":"marketing_cost","text":"What is your annual marketing cost (USD)?","type":"number"},
    {"key":"server_cost","text":"What is your annual server cost (USD)?","type":"number"},
    {"key":"annual_profit","text":"What is your annual profit (USD)?","type":"number"},
    {"key":"revenue_type","text":"Which revenue types? Reply using commas (Ad, Subscription, IAP). Example: Ad, IAP","type":"text"},
    {"key":"email","text":"Please share your email address (we will send valuation there)","type":"email"}
]

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # verification
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
            msg_id = msg.get("id") or str(msg.get("timestamp")) or None
            if msg_id:
                if msg_id in processed_msg_ids:
                    app.logger.debug("Duplicate msg %s ignored", msg_id)
                    continue
                processed_msg_ids.add(msg_id)
                if len(processed_msg_ids) > 5000:
                    processed_msg_ids.pop()

            user_id = msg.get("from")
            text_body = msg.get("text",{}).get("body","").strip()
            button_id = msg.get("interactive",{}).get("button_reply",{}).get("id","")
            incoming = (button_id or text_body or "").strip()

            if not incoming:
                continue

            # initialize user
            if user_id not in user_states:
                user_states[user_id] = {"step": -1, "answers": {}, "started_at": datetime.utcnow().timestamp(), "last_active": datetime.utcnow().timestamp()}
                send_whatsapp_buttons(user_id, "Hi, I am Kalagato AI Agent. Are you interested in selling your app?", ["Yes","No"])
                continue

            state = user_states[user_id]
            # session timeout
            if datetime.utcnow().timestamp() - state.get("last_active",0) > SESSION_TIMEOUT_SECONDS:
                user_states.pop(user_id, None)
                send_whatsapp_buttons(user_id, "Session expired. Start again? Are you interested in selling your app?", ["Yes","No"])
                continue
            state["last_active"] = datetime.utcnow().timestamp()

            # greeting step
            if state["step"] == -1:
                low = incoming.lower()
                if low in ("no","no_reply"):
                    send_whatsapp_text(user_id, "Thanks! If you have any queries contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                if low in ("yes","yes_reply"):
                    state["step"] = 0
                    send_whatsapp_text(user_id, QUESTIONS[0]["text"])
                    continue
                send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes","No"])
                continue

            # ignore stray yes/no mid-flow
            if state["step"] >= 0 and incoming.lower() in ("yes","no","yes_reply","no_reply"):
                app.logger.debug("Ignored stray yes/no from %s mid-flow", user_id)
                continue

            step = state["step"]
            if step < 0 or step >= len(QUESTIONS):
                send_whatsapp_text(user_id, "Unexpected state. Please say Hi to restart.")
                user_states.pop(user_id, None)
                continue

            q = QUESTIONS[step]
            key = q["key"]
            val = incoming

            # validators
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

            state["answers"][key] = val
            state["step"] = step + 1

            if state["step"] < len(QUESTIONS):
                send_whatsapp_text(user_id, QUESTIONS[state["step"]]["text"])
                continue

            # all answers collected -> compute valuation, save and call supabase function
            a = state["answers"]
            vmin, vmax, mid = compute_valuation(a.get("annual_profit"), a.get("revenue_type"))
            if vmin == vmax:
                valuation_text = f"${vmin:,.2f}"
            else:
                valuation_text = f"${vmin:,.2f} to ${vmax:,.2f}"

            # Save to Google Sheets
            saved = save_to_sheet(user_id, a, valuation_text)

            # Call Supabase Edge Function to send email
            payload_to_function = {
                "email": a.get("email"),
                "name": a.get("name"),
                "valuation_text": valuation_text,
                "data": a  # send full data if function wants to save or CC or whatever
            }
            func_ok, func_resp = call_supabase_send_email(payload_to_function)

            if func_ok:
                send_whatsapp_text(user_id, f"✅ Thank you {a.get('name','')}. We've sent your valuation ({valuation_text}) to your email.")
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but couldn't send the email automatically. Please contact aman@kalagato.co if needed.")
                app.logger.warning("Function call failed: %s", func_resp)

            # cleanup
            user_states.pop(user_id, None)

    return "OK", 200

# health
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

if __name__ == "__main__":
    # warm init (non-fatal)
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Initial gs init failed (continuing): %s", e)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
