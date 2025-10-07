# app.py (updated: name-first flow, multi-choice revenue input, consistent valuation)
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

# ------------- Config (env) -------------
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

# ------------- Google Sheets init -------------
_gs_client = None
_worksheet = None
_gs_init_error = None

def _load_service_account_info():
    raw = GOOGLE_CREDS_ENV
    if raw:
        if raw.endswith(".json") and os.path.exists(raw):
            with open(raw, "r") as f:
                return json.load(f)
        try:
            return json.loads(raw)
        except Exception as e:
            app.logger.warning("Failed to parse GOOGLE_SHEETS_CREDENTIALS: %s", e)
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

# ------------- WhatsApp helpers -------------
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

# ------------- Validation helpers -------------
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
        return float(str(s).replace(",","").strip())
    except:
        return None

def is_valid_email(e):
    if not e: return False
    return "@" in e and "." in e

# ------------- Valuation logic -------------
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

# ------------- Save to sheet -------------
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

# ------------- Call Supabase Function -------------
def call_supabase_send_email(payload: dict):
    if not SUPABASE_FUNCTION_URL:
        app.logger.warning("SUPABASE_FUNCTION_URL not configured.")
        return False, "No function URL"
    headers = {"Content-Type":"application/json"}
    if SUPABASE_API_KEY:
        headers["Authorization"] = f"Bearer {SUPABASE_API_KEY}"
    try:
        r = requests.post(SUPABASE_FUNCTION_URL, json=payload, headers=headers, timeout=12)
        if r.status_code in (200,201,202):
            app.logger.info("Supabase function called successfully")
            return True, r.text
        else:
            app.logger.error("Supabase function returned %s: %s", r.status_code, r.text)
            return False, r.text
    except Exception as e:
        app.logger.error("Supabase function call failed: %s", e)
        return False, str(e)

# ------------- Conversation flow (NAME first, then listing) -------------
user_states = {}
processed_msg_ids = set()
SESSION_TIMEOUT_SECONDS = 15 * 60

# base sequences (name will be explicitly injected)
OTHER_QUESTIONS = [
    {"key":"annual_revenue","text":"What is your annual revenue (USD)?","type":"number"},
    {"key":"marketing_cost","text":"What is your annual marketing cost (USD)?","type":"number"},
    {"key":"server_cost","text":"What is your annual server cost (USD)?","type":"number"},
    {"key":"annual_profit","text":"What is your annual profit (USD)?","type":"number"},
    # revenue_type will be special: present as buttons + accept multi-select by numbers/words
    {"key":"revenue_type","text":"Which revenue types apply? You can tap a button OR reply with numbers separated by commas (e.g. 1,3) or names (e.g. Ad, IAP):\n1) IAP\n2) Subscription\n3) Ad","type":"revenue"},
    {"key":"email","text":"Please share your email address (we will send valuation there)","type":"email"}
]

def normalize_revenue_input(incoming: str):
    """
    Accept:
     - button ids like 'iap', 'subscription', 'ad' (single)
     - comma separated numbers like '1,3'
     - comma separated names like 'Ad, IAP'
    Return: comma-joined canonical list: 'IAP, Subscription' etc.
    """
    if not incoming:
        return ""
    s = incoming.strip().lower()
    # direct button ids handling
    mapping = {"1":"IAP","2":"Subscription","3":"Ad","iap":"IAP","subscription":"Subscription","sub":"Subscription","ad":"Ad"}
    # if contains digits/commas
    parts = [p.strip() for p in s.replace(";",",").split(",") if p.strip()]
    results = []
    for p in parts:
        if p in mapping:
            results.append(mapping[p])
            continue
        # if user typed full name (ad, iap, subscription)
        if p in mapping:
            results.append(mapping[p])
            continue
        # if user typed words like "ad" or "iap"
        if p.startswith("ad"):
            results.append("Ad"); continue
        if "iap" in p or "in-app" in p or "in app" in p:
            results.append("IAP"); continue
        if "sub" in p:
            results.append("Subscription"); continue
        # numeric index mapping fallback
        if p.isdigit() and p in mapping:
            results.append(mapping[p]); continue
    # dedupe while preserving order
    seen = set(); out = []
    for item in results:
        if item not in seen:
            out.append(item); seen.add(item)
    return ", ".join(out)

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # verification (GET)
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

            # Start session if new user
            if user_id not in user_states:
                user_states[user_id] = {
                    "step": -1,
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

            # greeting state
            if state["step"] == -1:
                low = incoming.lower()
                if low in ("no","no_reply"):
                    send_whatsapp_text(user_id, "Thanks! If you have any queries contact aman@kalagato.co")
                    user_states.pop(user_id, None)
                    continue
                if low in ("yes","yes_reply"):
                    # ask name first (per your request)
                    state["step"] = 0
                    # prepare listing later after name
                    send_whatsapp_text(user_id, "What is your name?")
                    continue
                send_whatsapp_buttons(user_id, "Please select Yes or No.", ["Yes","No"])
                continue

            # step 0 = name entered, next ask listing (Play/App/Both)
            if state["step"] == 0:
                # treat incoming as name (no heavy validation)
                state["answers"]["name"] = incoming
                # now ask listing via buttons
                send_whatsapp_buttons(user_id, "Is your app listed on Play Store, App Store, or Both?", ["Play Store","App Store","Both"])
                state["step"] = -2  # waiting listing selection
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

                # Build questions: name is already collected; next add store links depending on selection then append OTHER_QUESTIONS
                questions = []
                if listing == "app_store":
                    questions.append({"key":"app_store_link","text":"Please provide the App Store link (https://...)", "type":"link_appstore"})
                elif listing == "play_store":
                    questions.append({"key":"play_store_link","text":"Please provide the Play Store link (https://...)", "type":"link_playstore"})
                else:  # both
                    questions.append({"key":"app_store_link","text":"Please provide the App Store link (https://...)", "type":"link_appstore"})
                    questions.append({"key":"play_store_link","text":"Please provide the Play Store link (https://...)", "type":"link_playstore"})

                # append the rest
                for q in OTHER_QUESTIONS:
                    questions.append(q.copy())

                state["questions"] = questions
                # next step index 0 into questions
                state["step"] = 1  # note: 0 was name; 1 means first of state["questions"]
                # ask first question (store link)
                send_whatsapp_text(user_id, state["questions"][0]["text"])
                continue

            # ignore stray yes/no mid-flow
            if state["step"] >= 1 and incoming.lower() in ("yes","no","yes_reply","no_reply"):
                app.logger.debug("Ignored stray yes/no from %s mid-flow", user_id)
                continue

            # normal question handling (state["step"] >= 1)
            q_index = state["step"] - 1  # because step=1 corresponds to questions[0]
            if q_index < 0 or q_index >= len(state.get("questions",[])):
                send_whatsapp_text(user_id, "Unexpected state. Please say Hi to restart.")
                user_states.pop(user_id, None)
                continue

            q = state["questions"][q_index]
            key = q["key"]
            val = incoming

            # validation per question type
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
                # revenue type we'll normalize later; accept a variety of inputs
                # allow single-button ids also (iap, subscription, ad)
                # or comma-separated numbers or names
                # minimal validation: accept anything non-empty
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

            # Save answer
            if q["type"] == "revenue":
                # if user pressed a button the button id might be 'iap' etc.
                norm = normalize_revenue_input(val)
                # If user used a single-button press like 'iap' we still convert
                state["answers"][key] = norm
            else:
                state["answers"][key] = val

            # advance step
            state["step"] = state["step"] + 1

            # ask next question or finish
            next_q_index = state["step"] - 1
            if next_q_index < len(state.get("questions",[])):
                send_whatsapp_text(user_id, state["questions"][next_q_index]["text"])
                # For revenue question, also send helper buttons for convenience
                if state["questions"][next_q_index]["key"] == "revenue_type":
                    # send buttons (note: buttons are single-select, but they help users who want a single option)
                    send_whatsapp_buttons(user_id, "Tap a button for a single option or reply with numbers/names for multiple (e.g. 1,3 or Ad, IAP).", ["IAP","Subscription","Ad"])
                continue

            # All done -> compute valuation, save and call Supabase function
            answers = state["answers"]
            # ensure revenue_type is normalized string
            answers["revenue_type"] = answers.get("revenue_type","").strip()

            # compute numeric profit
            profit_numeric = parse_number(answers.get("annual_profit","")) or 0.0
            vmin, vmax, mid = compute_valuation(profit_numeric, answers.get("revenue_type",""))
            if vmin == vmax:
                valuation_text = f"${vmin:,.2f}"
            else:
                valuation_text = f"${vmin:,.2f} to ${vmax:,.2f}"

            # Save to Google Sheets (valuation_text is used)
            saved = save_to_sheet(user_id, answers, valuation_text)

            # Prepare supabase payload: include valuation_text and numeric profit
            payload_to_function = {
                "email": answers.get("email"),
                "name": answers.get("name"),
                "valuation_text": valuation_text,
                "data": answers,
                "annual_profit_numeric": profit_numeric
            }
            app.logger.info("Calling Supabase function with payload: email=%s profit=%s valuation=%s", answers.get("email"), profit_numeric, valuation_text)

            ok, resp = call_supabase_send_email(payload_to_function)

            if ok:
                send_whatsapp_text(user_id, f"✅ Thank you {answers.get('name','')}. We've sent your valuation ({valuation_text}) to your email.")
            else:
                send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. Please contact aman@kalagato.co if needed.")
                app.logger.warning("Supabase function failed: %s", resp)

            # cleanup
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
