# app.py
import os
import json
import re
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-bot")

# ---------- Flask ----------
app = Flask(__name__)

# ---------- Environment / Config ----------
SHEET_NAME = os.getenv("SHEET_NAME")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # full JSON
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_FUNCTION_NAME = os.getenv("SUPABASE_FUNCTION_NAME", "send-valuation")
EMAIL_CCS = os.getenv("EMAIL_CCS", "")  # comma-separated CC emails to pass to supabase function (if function supports it)

if not all([SHEET_NAME, GOOGLE_SHEETS_CREDENTIALS, PHONE_NUMBER_ID, WHATSAPP_TOKEN]):
    logger.warning("One or more core environment variables are missing. Make sure SHEET_NAME, GOOGLE_SHEETS_CREDENTIALS, PHONE_NUMBER_ID and WHATSAPP_TOKEN are set.")

# ---------- Google Sheets Setup ----------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# GOOGLE_SHEETS_CREDENTIALS expected to be full JSON string (multiline JSON allowed)
try:
    creds_json = json.loads(GOOGLE_SHEETS_CREDENTIALS) if GOOGLE_SHEETS_CREDENTIALS else None
    if creds_json:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
        gclient = gspread.authorize(creds)
        sheet = gclient.open(SHEET_NAME).sheet1
        logger.info("Google Sheets client initialized and sheet opened.")
    else:
        sheet = None
        logger.warning("No Google Sheets credentials found; sheet operations will be skipped.")
except Exception as e:
    sheet = None
    logger.exception("Failed to initialize Google Sheets client: %s", e)

# ---------- WhatsApp API Setup ----------
WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

# ---------- Conversation state ----------
# In-memory state for demo. For production use Redis or DB to persist between processes.
user_states = {}

# ---------- Helper functions ----------
def send_whatsapp_text(to, text):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data, timeout=10)
        resp.raise_for_status()
        logger.info("Sent text to %s", to)
        return resp.json()
    except Exception as e:
        logger.exception("Failed to send WhatsApp text: %s", e)
        return None

def send_whatsapp_buttons(to, body_text, buttons):
    """
    buttons: list of strings (titles). Each reply id will be the lowercased title.
    """
    interactive = {
        "type": "button",
        "body": {"text": body_text},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b.lower(), "title": b}}
                for b in buttons
            ]
        }
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive
    }
    try:
        resp = requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data, timeout=10)
        resp.raise_for_status()
        logger.info("Sent buttons to %s: %s", to, buttons)
        return resp.json()
    except Exception as e:
        logger.exception("Failed to send WhatsApp buttons: %s", e)
        return None

def _is_number_text(text: str) -> bool:
    if text is None:
        return False
    t = str(text).strip()
    if t == "":
        return False
    # remove currency symbols and spaces but keep digits, dot, minus
    cleaned = re.sub(r"[^\d\.\-]", "", t)
    return bool(re.match(r"^-?\d+(\.\d+)?$", cleaned))

def _parse_number(text: str) -> float:
    if text is None:
        return 0.0
    t = str(text).strip()
    cleaned = re.sub(r"[^\d\.\-]", "", t)
    try:
        return float(cleaned) if cleaned not in ("", "-", None) else 0.0
    except:
        return 0.0

def _parse_revenue_types(text: str):
    """
    Accepts '1,3' or 'iap,ad' or 'Ad,iap' or '1' etc.
    Returns dict of flags {IAP: Yes/No, Subscription: Yes/No, Ad: Yes/No}
    """
    selected = set()
    if not text:
        return {"IAP": "No", "Subscription": "No", "Ad": "No"}
    parts = [p.strip().lower() for p in re.split(r"[,\s]+", text) if p.strip()]
    mapping = {"1": "IAP", "2": "Subscription", "3": "Ad"}
    for p in parts:
        if p in mapping:
            selected.add(mapping[p])
        else:
            if "iap" in p:
                selected.add("IAP")
            if "sub" in p:
                selected.add("Subscription")
            if "ad" in p:
                selected.add("Ad")
    return {
        "IAP": "Yes" if "IAP" in selected else "No",
        "Subscription": "Yes" if "Subscription" in selected else "No",
        "Ad": "Yes" if "Ad" in selected else "No",
    }

def compute_valuation(profit_num: float, revenue_types_text: str):
    """
    Uses same rules as your Supabase function:
      - profit < 1000: fixed 1000
      - Ad only: 1.0x - 1.7x
      - Subscription / Others / IAP: 1.5x - 2.3x
      - fallback: 2.5x single multiplier
    Returns valuationMin, valuationMax, estimatedAvg, formattedString
    """
    rt = _parse_revenue_types(revenue_types_text)
    if profit_num < 1000:
        valuation_min = valuation_max = 1000.0
    else:
        # If Ad selected and no subscription
        if rt["Ad"] == "Yes" and rt["Subscription"] == "No" and rt["IAP"] == "No":
            valuation_min = profit_num * 1.0
            valuation_max = profit_num * 1.7
        elif rt["Subscription"] == "Yes" or rt["IAP"] == "Yes":
            valuation_min = profit_num * 1.5
            valuation_max = profit_num * 2.3
        else:
            # fallback
            valuation_min = valuation_max = profit_num * 2.5
    estimated = (valuation_min + valuation_max) / 2.0
    if valuation_min == valuation_max:
        formatted = f"{valuation_min:,.2f}"
    else:
        formatted = f"{valuation_min:,.2f} to {valuation_max:,.2f}"
    # return numeric min/max/avg and string with currency formatting
    return valuation_min, valuation_max, estimated, f"${formatted}"

def append_row_to_sheet(row):
    if sheet is None:
        logger.warning("Sheet not initialized; skipping append.")
        return False
    try:
        sheet.append_row(row)
        logger.info("Appended row to sheet.")
        return True
    except Exception as e:
        logger.exception("Failed to append to Google Sheet: %s", e)
        return False

def call_supabase_function(payload):
    """
    Invoke Supabase Edge Function 'send-valuation' with the payload.
    Uses SERVICE_ROLE_KEY if present for Authorization, otherwise uses anon key.
    """
    if not SUPABASE_URL:
        logger.warning("SUPABASE_URL not set; skipping supabase function call.")
        return None, "no-supabase-url"

    fn_url = SUPABASE_URL.rstrip("/") + f"/functions/v1/{SUPABASE_FUNCTION_NAME}"
    headers = {"Content-Type": "application/json"}
    # prefer service role for privileged operations:
    auth_key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
    if not auth_key:
        logger.warning("No supabase key found; cannot call function.")
        return None, "no-supabase-key"
    headers["apikey"] = SUPABASE_ANON_KEY or auth_key
    headers["Authorization"] = f"Bearer {auth_key}"
    try:
        r = requests.post(fn_url, headers=headers, json=payload, timeout=15)
        try:
            r.raise_for_status()
        except Exception as e:
            logger.error("Supabase function returned error %s: %s", r.status_code, r.text)
            # return response details
            return r, f"http-{r.status_code}"
        logger.info("Supabase function invoked successfully.")
        return r, None
    except Exception as e:
        logger.exception("Error invoking supabase function: %s", e)
        return None, str(e)

# ---------- Question flow helpers ----------
def get_questions_for_listing(listing):
    """
    listing: 'app store', 'play store', 'both'
    Returns list of question prompts in order AFTER the listing selection.
    We always ask name first before listing selection in the main flow.
    """
    qs = []
    # After listing was chosen we will ask links accordingly later.
    # THIS function returns the numeric questions etc that come after links.
    qs += [
        "What is your last 12 months revenue? (Numbers only)",
        "What is your last 12 months profit? (Numbers only)",
        "What is your last 12 months spends? (Numbers only)",
        "What is your monthly profit? (Numbers only)",
        "What is your annual marketing cost (USD)? Enter numbers only.",
        "What is your annual server cost (USD)? Enter numbers only.",
        "What is your annual profit (USD)? Enter numbers only.",
        "Which revenue types? Reply with numbers separated by commas:\n1) IAP\n2) Subscription\n3) Ad (example: 1,3)",
        "Please share your email address (we will send valuation there)."
    ]
    return qs

# ---------- Webhook endpoint ----------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification challenge for GET
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verify token", 403

    data = request.get_json(silent=True)
    if not data:
        logger.warning("Webhook invoked with no JSON.")
        return "ok", 200

    # Parse incoming messages in the structure Facebook sends
    entries = data.get("entry", [])
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue
            msg = messages[0]
            user_id = msg.get("from")
            # message text or interactive reply
            text = msg.get("text", {}).get("body", "")
            interactive = msg.get("interactive", {})
            button_reply_id = None
            if interactive:
                # button or list reply
                br = interactive.get("button_reply") or interactive.get("list_reply")
                if br:
                    button_reply_id = br.get("id") or br.get("title") or None

            # Initialize state if new user
            state = user_states.get(user_id)
            if not state:
                # new user
                # greet them only when they send "hi" or similar, else send greeting
                # We'll send initial greeting always for simplicity
                send_whatsapp_buttons(
                    user_id,
                    "Hi, I am Kalagato AI Agent. Are you interested in selling your app?",
                    ["Yes", "No"]
                )
                # state keys:
                # step: -1 waiting for initial yes/no
                # responses: list of raw responses (first element will be their "Yes"/"No" or listing etc)
                # questions: the remaining questions prompts mapping to responses
                user_states[user_id] = {
                    "step": -1,
                    "responses": [],
                    "questions": [],
                    "listing": None,  # 'app store'/'play store'/'both'
                    "awaiting": "initial_yesno"  # simple intent tracker
                }
                continue

            # existing user
            awaiting = state.get("awaiting")
            step = state.get("step", 0)

            # If awaiting initial yes/no (buttons)
            if awaiting == "initial_yesno":
                chosen = (button_reply_id or text).strip().lower()
                if chosen in ("no", "n"):
                    send_whatsapp_text(user_id, "Thanks, if you have any queries contact us on aman@kalagato.co")
                    # cleanup
                    user_states.pop(user_id, None)
                    continue
                elif chosen in ("yes", "y"):
                    # ask for name first
                    send_whatsapp_text(user_id, "Great — what's your name?")
                    state["awaiting"] = "name"
                    state["responses"] = []  # reset
                    state["step"] = 0
                    continue
                else:
                    send_whatsapp_buttons(user_id, "Please select Yes or No:", ["Yes", "No"])
                    continue

            # Name
            if awaiting == "name":
                name = button_reply_id or text
                if not name or name.strip() == "":
                    send_whatsapp_text(user_id, "Please tell us your name.")
                    continue
                state["responses"].append(name.strip())
                # ask listing next
                send_whatsapp_buttons(user_id, "Is your app listed on App Store, Play Store, or Both?", ["App Store", "Play Store", "Both"])
                state["awaiting"] = "listing"
                continue

            # Listing selection
            if awaiting == "listing":
                chosen = (button_reply_id or text).strip().lower()
                if chosen in ("app store", "appstore", "app store"):
                    state["listing"] = "app store"
                    # ask appstore link
                    send_whatsapp_text(user_id, "Please provide the App Store link (https://apps.apple.com/...).")
                    state["awaiting"] = "app_store_link"
                    continue
                if chosen in ("play store", "playstore", "play store"):
                    state["listing"] = "play store"
                    send_whatsapp_text(user_id, "Please provide the Play Store link (https://play.google.com/...).")
                    state["awaiting"] = "play_store_link"
                    continue
                if chosen in ("both",):
                    state["listing"] = "both"
                    send_whatsapp_text(user_id, "Please provide the App Store link (https://apps.apple.com/...). If none, reply skip.")
                    state["awaiting"] = "app_store_link"
                    # we'll then ask play store link after
                    continue
                # if user typed something unexpected
                send_whatsapp_buttons(user_id, "Please choose one:", ["App Store", "Play Store", "Both"])
                continue

            # App Store link entry
            if awaiting == "app_store_link":
                val = (button_reply_id or text or "").strip()
                if val.lower() == "skip":
                    state["responses"].append("")  # empty app store link
                else:
                    if not (val.startswith("https://apps.apple.com") or val.startswith("http://apps.apple.com")):
                        send_whatsapp_text(user_id, "❌ Invalid App Store link. Please provide a valid URL starting with https://apps.apple.com or reply 'skip' to skip.")
                        continue
                    state["responses"].append(val)
                # next: if listing==both ask play store, else proceed with numeric questions
                if state["listing"] == "both":
                    send_whatsapp_text(user_id, "Please provide the Play Store link (https://play.google.com/...). If none, reply skip.")
                    state["awaiting"] = "play_store_link"
                else:
                    # move to numeric question sequence
                    qlist = get_questions_for_listing(state["listing"])
                    state["questions"] = qlist
                    state["step"] = 0
                    state["awaiting"] = "question_answer"
                    send_whatsapp_text(user_id, state["questions"][0])
                continue

            # Play Store link entry
            if awaiting == "play_store_link":
                val = (button_reply_id or text or "").strip()
                if val.lower() == "skip":
                    state["responses"].append("")  # empty play store link
                else:
                    if not (val.startswith("https://play.google.com") or val.startswith("http://play.google.com")):
                        send_whatsapp_text(user_id, "❌ Invalid Play Store link. Please provide a valid URL starting with https://play.google.com or reply 'skip' to skip.")
                        continue
                    state["responses"].append(val)
                # after collecting links, move to numeric questions
                qlist = get_questions_for_listing(state["listing"])
                state["questions"] = qlist
                state["step"] = 0
                state["awaiting"] = "question_answer"
                send_whatsapp_text(user_id, state["questions"][0])
                continue

            # Handling numeric and remaining questions
            if awaiting == "question_answer":
                # current question text
                qidx = state["step"]
                questions = state.get("questions", [])
                if qidx >= len(questions):
                    # Shouldn't happen - but finalize
                    logger.warning("Step index beyond questions for %s", user_id)
                    send_whatsapp_text(user_id, "An error happened; please try again.")
                    user_states.pop(user_id, None)
                    continue
                current_q = questions[qidx]
                answer = (button_reply_id or text or "").strip()
                # Validate based on question
                ql = current_q.lower()
                # numeric validations
                if any(k in ql for k in ["revenue", "profit", "spend", "marketing", "server", "monthly profit"]):
                    if not _is_number_text(answer):
                        send_whatsapp_text(user_id, "❌ Please enter a valid number (commas and $ OK).")
                        continue
                # email validation
                if "email" in ql:
                    if not re.match(r"[^@]+@[^@]+\.[^@]+", answer):
                        send_whatsapp_text(user_id, "❌ Please enter a valid email address.")
                        continue
                # revenue types question expects CSV / numbered answers - accept free text but don't strictly validate
                # Save answer
                state.setdefault("responses", state.get("responses", []))
                state["responses"].append(answer)
                state["step"] = state["step"] + 1

                # next question or finish
                if state["step"] < len(state["questions"]):
                    next_q = state["questions"][state["step"]]
                    send_whatsapp_text(user_id, next_q)
                    continue
                else:
                    # Completed sequence -> save to sheet and call supabase function to send email
                    # Build a payload from responses
                    try:
                        # Responses order:
                        # state["responses"] currently contains:
                        # [name, app_store_link(if asked) or '', play_store_link(if asked) or '', q1, q2, ..., qN]
                        # but because we appended links earlier before numeric questions, the indices need careful mapping.
                        # We'll reconstruct robustly by using known question prompts:
                        all_resps = state["responses"][:]  # copy
                        name = all_resps[0] if len(all_resps) > 0 else ""
                        # Next items depend on whether app/play links were asked:
                        link_idx = 1
                        app_store_link = ""
                        play_store_link = ""
                        if state["listing"] in ("app store", "both"):
                            app_store_link = all_resps[link_idx]
                            link_idx += 1
                        if state["listing"] in ("play store", "both"):
                            play_store_link = all_resps[link_idx]
                            link_idx += 1

                        # numeric answers start at link_idx
                        remaining = all_resps[link_idx:]
                        # Our questions sequence inside get_questions_for_listing was:
                        # [revenue, profit, spends, monthly profit, marketing cost, server cost, annual profit, revenue types, email]
                        # Map them accordingly:
                        r_revenue = remaining[0] if len(remaining) > 0 else ""
                        r_profit = remaining[1] if len(remaining) > 1 else ""
                        r_spends = remaining[2] if len(remaining) > 2 else ""
                        r_monthly_profit = remaining[3] if len(remaining) > 3 else ""
                        r_marketing = remaining[4] if len(remaining) > 4 else ""
                        r_server = remaining[5] if len(remaining) > 5 else ""
                        r_annual_profit = remaining[6] if len(remaining) > 6 else ""
                        r_revenue_types = remaining[7] if len(remaining) > 7 else ""
                        r_email = remaining[8] if len(remaining) > 8 else ""

                        # parse numbers to floats for valuation
                        annual_profit_value = _parse_number(r_annual_profit)
                        valuation_min, valuation_max, estimated_avg, formatted = compute_valuation(annual_profit_value, r_revenue_types)

                        # Prepare sheet row (customize columns as you like)
                        # Example column order:
                        # Timestamp, Name, Listing, AppStoreLink, PlayStoreLink,
                        # AnnualRevenue, AnnualProfit, AnnualSpends, MonthlyProfit, MarketingCost, ServerCost,
                        # IAP (Yes/No), Subscription (Yes/No), Ad (Yes/No), Email, ValuationAvg
                        rt_flags = _parse_revenue_types(r_revenue_types)
                        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                        row = [
                            timestamp,
                            name,
                            state["listing"] or "",
                            app_store_link,
                            play_store_link,
                            _parse_number(r_revenue),
                            _parse_number(r_annual_profit),
                            _parse_number(r_spends),
                            _parse_number(r_monthly_profit),
                            _parse_number(r_marketing),
                            _parse_number(r_server),
                            rt_flags["IAP"],
                            rt_flags["Subscription"],
                            rt_flags["Ad"],
                            r_email,
                            f"{estimated_avg:.2f}"
                        ]
                        append_row_to_sheet(row)

                        # Call supabase function to send email
                        payload = {
                            "name": name,
                            "revenueType": r_revenue_types,
                            "appLink": app_store_link or play_store_link or "",
                            "revenue": _parse_number(r_revenue),
                            "marketingCost": _parse_number(r_marketing),
                            "serverCost": _parse_number(r_server),
                            "profit": _parse_number(r_annual_profit),
                            "email": r_email
                        }
                        # include cc list if provided (function must respect it)
                        if EMAIL_CCS:
                            cc_list = [e.strip() for e in EMAIL_CCS.split(",") if e.strip()]
                            if cc_list:
                                payload["cc"] = cc_list

                        resp, err = call_supabase_function(payload)
                        if err:
                            logger.error("Supabase function returned error tag: %s", err)
                            send_whatsapp_text(user_id, "✅ Saved your data, but we couldn't send the email automatically. Please contact aman@kalagato.co if needed.")
                        else:
                            send_whatsapp_text(user_id, f"✅ Thank you {name}. We've sent your valuation ({formatted}) to your email.")
                    except Exception as e:
                        logger.exception("Error completing flow for user %s: %s", user_id, e)
                        send_whatsapp_text(user_id, "Sorry, something went wrong while saving your data. Please try again later.")
                    # cleanup user state
                    user_states.pop(user_id, None)
                    continue

    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
