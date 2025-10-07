# app.py
import os
import json
import time
from datetime import datetime
from flask import Flask, request
import requests
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

app = Flask(__name__)
app.logger.setLevel("INFO")

# ----------------- Config from env -----------------
GOOGLE_CREDS_ENV = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "Sheet1").strip()

PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token").strip()

SUPABASE_FUNCTION_URL = os.getenv("SUPABASE_FUNCTION_URL", "").strip()
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY", "").strip()

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# ----------------- Google Sheets init (robust) -----------------
_gs_client = None
_worksheet = None
_gs_init_error = None

def _load_service_account_info():
    raw = GOOGLE_CREDS_ENV
    if raw:
        # if a filename provided and exists, load it
        if raw.endswith(".json") and os.path.exists(raw):
            with open(raw, "r") as f:
                return json.load(f)
        # try parse JSON directly (multi-line allowed)
        try:
            return json.loads(raw)
        except Exception as e:
            app.logger.warning("Failed to parse GOOGLE_SHEETS_CREDENTIALS: %s", e)
    # fallback to local file
    if os.path.exists("service_account.json"):
        with open("service_account.json", "r") as f:
            return json.load(f)
    raise Exception("No valid Google service account credentials found. Set GOOGLE_SHEETS_CREDENTIALS or upload service_account.json")

def try_init_gs():
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
            _worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows="2000", cols="20")
            headers = [
                "Timestamp", "User ID", "Listing", "App Store Link", "Play Store Link",
                "Annual Revenue", "Marketing Cost", "Server Cost", "Annual Profit",
                "Revenue Type", "Email", "Valuation"
            ]
            _worksheet.insert_row(headers, index=1)
        app.logger.info("Google Sheets initialized: %s", SHEET_NAME)
        _gs_init_error = None
        return _worksheet
    except Exception as e:
        _gs_init_error = f"Google Sheets auth failed: {e}"
        app.logger.error(_gs_init_error)
        return None

# ----------------- WhatsApp helpers -----------------
def send_whatsapp_text(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp not configured - skipping send.")
        return
    payload = {"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    try:
        r = requests.post(WHATSAPP_API_URL, headers=WHATSAPP_HEADERS, json=payload, timeout=8)
        app.logger.debug("WA text sent status %s", r.status_code)
    except Exception as e:
        app.logger.error("WA send failed: %s", e)

def send_whatsapp_buttons(to, text, buttons):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.debug("WhatsApp not configured - skipping buttons.")
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
        app.logger.debug("WA buttons status %s", r.status_code)
    except Exception as e:
        app.logger.error("WA send failed: %s", e)

# ----------------- Validation helpers -----------------
def is_valid_url(u, store):
    if not u: return False
    u = u.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    if store == "app_store":
        return "apps.apple.com" in u
    if store == "play_store":
        return "play.google.com" in u or "market.android.com" in u
    return True

def is_number(s):
    try:
        float(str(s).replace(",","").strip())
        return True
    except:
        return False

def parse_number(s):
    try:
        if s is None:
            return None
        return float(str(s).replace(",","").strip())
    except:
        return None

def is_valid_email(e):
    if not e: return False
    return "@" in e and "." in e

# ----------------- Revenue normalization -----------------
def normalize_revenue_input(incoming: str):
    if not incoming:
        return ""
    s = incoming.strip().lower()
    mapping = {"1":"IAP","2":"Subscription","3":"Ad","iap":"IAP","subscription":"Subscription","sub":"Subscription","ad":"Ad"}
    parts = [p.strip() for p in s.replace(";",",").split(",") if p.strip()]
    results = []
    for p in parts:
        if p in mapping:
            results.append(mapping[p])
            continue
        if p.startswith("ad"):
            results.append("Ad"); continue
        if "iap" in p or "in-app" in p or "in app" in p:
            results.append("IAP"); continue
        if "sub" in p:
            results.append("Subscription"); continue
    seen = set(); out = []
    for item in results:
        if item not in seen:
            out.append(item); seen.add(item)
    return ", ".join(out)

# ----------------- Valuation logic (same rules used by bot) -----------------
def compute_valuation(profit_val, revenue_type_text):
    # profit_val is expected to be numeric already (float) or parseable
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

# ----------------- Save to Google Sheets -----------------
def save_to_sheet(user_id, answers, valuation_text):
    ws = try_init_gs()
    if ws is None:
        app.logger.warning("Skipping sheet save: %s", _gs_init_error)
        return False
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    app_store_link = answers.get("app_store_link","")
    play_store_link = answers.get("play_store_link","")
    row = [
        now,
        user_id,
        answers.get("listing",""),
        app_store_link,
        play_store_link,
        answers.get("annual_revenue",""),
        answers.get("marketing_cost",""),
        answers.get("server_cost",""),
        answers.get("annual_profit",""),
        answers.get("revenue_type_normalized",""),
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

# ----------------- Call Supabase Edge Function (robust logging & payload) -----------------
def call_supabase_send_email(payload: dict):
    if not SUPABASE_FUNCTION_URL:
        app.logger.warning("SUPABASE_FUNCTION_URL not configured.")
        return False, "No function URL"

    # Ensure numeric profit is present
    ap = payload.get("annual_profit_numeric", None)
    if ap is None:
        ap = parse_number(payload.get("data", {}).get("annual_profit"))
        payload["annual_profit_numeric"] = ap

    # Ensure revenue_type_normalized is present
    if not payload.get("revenue_type_normalized"):
        payload["revenue_type_normalized"] = payload.get("data", {}).get("revenue_type", "")

    # Ensure valuation_text is present (last resort compute here)
    if not payload.get("valuation_text"):
        try:
            profit_val = payload.get("annual_profit_numeric", 0) or 0
            rt = payload.get("revenue_type_normalized", "")
            vmin, vmax, mid = compute_valuation(profit_val, rt)
            if vmin == vmax:
                payload["valuation_text"] = f"${vmin:,.2f}"
            else:
                payload["valuation_text"] = f"${vmin:,.2f} to ${vmax:,.2f}"
        except Exception as e:
            app.logger.exception("Failed to compute fallback valuation: %s", e)
            payload["valuation_text"] = "$1,000"

    # Add hint that Supabase should prefer provided valuation_text
    payload["force_valuation"] = True

    app.logger.info(
        "Calling Supabase function; email=%s profit=%s revenue_type=%s valuation=%s",
        payload.get("email"),
        payload.get("annual_profit_numeric"),
        payload.get("revenue_type_normalized"),
        payload.get("valuation_text")
    )

    headers = {"Content-Type":"application/json"}
    if SUPABASE_API_KEY:
        headers["Authorization"] = f"Bearer {SUPABASE_API_KEY}"

    try:
        r = requests.post(SUPABASE_FUNCTION_URL, json=payload, headers=headers, timeout=15)
        app.logger.info("Supabase function response status=%s text=%s", r.status_code, (r.text[:300] + "...") if r.text and len(r.text)>300 else r.text)
        if r.status_code in (200,201,202):
            return True, r.text
        else:
            return False, r.text
    except Exception as e:
        app.logger.exception("Call to Supabase function failed: %s", e)
        return False, str(e)

# ----------------- Conversation flow -----------------
user_states = {}
processed_msg_ids = set()
SESSION_TIMEOUT_SECONDS = 15 * 60

OTHER_QUESTIONS = [
    {"key":"annual_revenue","text":"What is your annual revenue (USD)?","type":"number"},
    {"key":"marketing_cost","text":"What is your annual marketing cost (USD)?","type":"number"},
    {"key":"server_cost","text":"What is your annual server cost (USD)?","type":"number"},
    {"key":"annual_profit","text":"What is your annual profit (USD)?","type":"number"},
    {"key":"revenue_type","text":"Which revenue types apply? Tap a button for single choice or reply with numbers/names for multiple (e.g. 1,3 or Ad,IAP):\n1) IAP\n2) Subscription\n3) Ad","type":"revenue"},
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

    data = request.get_json(silent=True)
    if not data:
        return "No payload", 400

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
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

            # start new session
            if user_id not in user_states:
                user_states[user_id] = {
                    "step": -1,  # -1 greeting
                    "answers": {},
                    "questions": [],
                    "started_at": time.time(),
                    "last_active": time.time()
                }
                send_whatsapp_buttons(user_id, "Hi, I am Kalagato AI Agent. Are you interested in selling your app?", ["Yes","No"])
                continue

            state = user_states[user_id]
            # timeout
            if time.time() - state.get("last_active",0) > SESSION_TIMEOUT_SECONDS:
                user_states.pop(user_id, None)
                send_whatsapp_buttons(user_id, "Session expired. Start again? Are you interested in selling your app?", ["Yes","No"])
                continue
            state["last_active"] = time.time()

            # greeting step
            if state["step"] == -1:
                low = incoming.lower()
                if low in ("no","no_reply"):
                    send_whatsapp_text(user_id, "Thanks! If you have any queries contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                if low in ("yes","yes_reply"):
                    # ask name first
                    state["step"] = 0
                    send_whatsapp_text(user_id, "What is your name?")
                    continue
                send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes","No"])
                continue

            # step 0 = name entered
            if state["step"] == 0:
                state["answers"]["name"] = incoming
                # now ask listing via buttons
                send_whatsapp_buttons(user_id, "Is your app listed on Play Store, App Store, or Both?", ["Play Store","App Store","Both"])
                state["step"] = -2  # waiting listing
                continue

            # listing selection
            if state["step"] == -2:
                li = incoming.lower()
                listing = ""
                if li in ("play_store","play store","play"):
                    listing = "play_store"
                elif li in ("app_store","app store","app"):
                    listing = "app_store"
                elif li in ("both","both_reply"):
                    listing = "both"
                else:
                    if "play" in li and "app" not in li:
                        listing = "play_store"
                    elif "app" in li and "play" not in li:
                        listing = "app_store"
                    elif "both" in li:
                        listing = "both"
                if not listing:
                    send_whatsapp_buttons(user_id, "Please choose one: Play Store, App Store, or Both.", ["Play Store","App Store","Both"])
                    continue
                state["answers"]["listing"] = listing
                # build dynamic questions (store links then other questions)
                questions = []
                if listing == "app_store":
                    questions.append({"key":"app_store_link","text":"Please provide the App Store link (https://...)", "type":"link_appstore"})
                elif listing == "play_store":
                    questions.append({"key":"play_store_link","text":"Please provide the Play Store link (https://...)", "type":"link_playstore"})
                else:
                    questions.append({"key":"app_store_link","text":"Please provide the App Store link (https://...)", "type":"link_appstore"})
                    questions.append({"key":"play_store_link","text":"Please provide the Play Store link (https://...)", "type":"link_playstore"})
                # append other questions
                for q in OTHER_QUESTIONS:
                    questions.append(q.copy())
                state["questions"] = questions
                # next expected is index 0 in questions; step uses offset: step=1 => questions[0]
                state["step"] = 1
                send_whatsapp_text(user_id, state["questions"][0]["text"])
                continue

            # ignore stray yes/no mid-flow
            if state["step"] >= 1 and incoming.lower() in ("yes","no","yes_reply","no_reply"):
                app.logger.debug("Ignored stray yes/no from %s mid-flow", user_id)
                continue

            # normal flow: step >=1 maps to questions index step-1
            q_index = state["step"] - 1
            if q_index < 0 or q_index >= len(state.get("questions", [])):
                send_whatsapp_text(user_id, "Unexpected state. Please say Hi to restart.")
                user_states.pop(user_id, None)
                continue

            q = state["questions"][q_index]
            key = q["key"]
            val = incoming

            # validation
            valid = True
            if q["type"] == "number":
                if not is_number(val):
                    valid = False
            elif q["type"] == "email":
                if not is_valid_email(val):
                    valid = False
            elif q["type"] == "link_appstore":
                if not is_valid_url(val, "app_store"):
                    valid = False
            elif q["type"] == "link_playstore":
                if not is_valid_url(val, "play_store"):
                    valid = False
            elif q["type"] == "revenue":
                if not val:
                    valid = False

            if not valid:
                if q["type"] == "number":
                    send_whatsapp_text(user_id, "❌ Please enter a number (digits only).")
                elif q["type"] == "email":
                    send_whatsapp_text(user_id, "❌ Invalid email. Please provide a valid email address.")
                elif q["type"] in ("link_appstore","link_playstore"):
                    send_whatsapp_text(user_id, "❌ Please send a valid URL starting with http:// or https:// and the correct store domain.")
                elif q["type"] == "revenue":
                    send_whatsapp_text(user_id, "❌ Invalid input. " + q["text"])
                else:
                    send_whatsapp_text(user_id, f"❌ Invalid input. {q['text']}")
                send_whatsapp_text(user_id, q["text"])
                continue

            # save answer (normalize revenue)
            if q["type"] == "revenue":
                norm = normalize_revenue_input(val)
                state["answers"]["revenue_type_normalized"] = norm
                # also store raw for reference if needed
                state["answers"]["revenue_type_raw"] = val
            else:
                state["answers"][key] = val

            # advance to next
            state["step"] = state["step"] + 1
            next_index = state["step"] - 1
            if next_index < len(state.get("questions", [])):
                send_whatsapp_text(user_id, state["questions"][next_index]["text"])
                # if the next question is revenue, also send buttons as a convenience
                if state["questions"][next_index]["key"] == "revenue_type":
                    send_whatsapp_buttons(user_id, "Tap a button for single option or reply with numbers/names for multiple (e.g. 1,3 or Ad,IAP).", ["IAP","Subscription","Ad"])
                continue

            # finished collecting answers
            answers = state["answers"]
            # ensure revenue_type_normalized exists
            answers["revenue_type_normalized"] = answers.get("revenue_type_normalized", "")
            # compute numeric profit
            profit_numeric = parse_number(answers.get("annual_profit", None)) or 0.0
            vmin, vmax, mid = compute_valuation(profit_numeric, answers.get("revenue_type_normalized", ""))
            if vmin == vmax:
                valuation_text = f"${vmin:,.2f}"
            else:
                valuation_text = f"${vmin:,.2f} to ${vmax:,.2f}"

            # save to sheet (string values as provided)
            saved = save_to_sheet(user_id, answers, valuation_text)

            # prepare supabase payload (numbers and normalized strings)
            payload_to_function = {
                "email": answers.get("email"),
                "name": answers.get("name"),
                "valuation_text": valuation_text,
                "data": answers,
                "annual_profit_numeric": profit_numeric,
                "revenue_type_normalized": answers.get("revenue_type_normalized", "")
            }

            ok, resp = call_supabase_send_email(payload_to_function)

            if ok:
                send_whatsapp_text(user_id, f"✅ Thank you {answers.get('name','')}. We've sent your valuation ({valuation_text}) to your email.")
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. Please contact aman@kalagato.co if needed.")
                app.logger.warning("Supabase function failed: %s", resp)

            # cleanup session
            user_states.pop(user_id, None)

    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

if __name__ == "__main__":
    try:
        try_init_gs()
    except Exception as e:
        app.logger.warning("Initial Google Sheets init failed (continuing): %s", e)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
