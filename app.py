import os
import json
import re
import requests
import smtplib
from email.message import EmailMessage
from datetime import datetime
from flask import Flask, request
import gspread
from urllib.parse import urlparse

app = Flask(__name__)

# ---------- Config from env ----------
PORT = int(os.getenv("PORT") or 5000)
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
SHEET_NAME = os.getenv("SHEET_NAME", "")
SHEET_TAB = os.getenv("SHEET_TAB", None)  # optional, uses first sheet by default

SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
CC_EMAILS = [e.strip() for e in (os.getenv("CC_EMAILS", "") or "").split(",") if e.strip()]
SUPABASE_FUNCTION_URL = os.getenv("SUPABASE_FUNCTION_URL", "")  # optional; will be called after saving

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

# ---------- Google Sheets auth: either JSON string env or path ----------
def get_gspread_client():
    creds_json = None
    cred_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH")
    cred_json_env = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")

    if cred_path and os.path.exists(cred_path):
        with open(cred_path, "r", encoding="utf-8") as f:
            creds_json = json.load(f)
    elif cred_json_env:
        # support multi-line JSON stored in env directly
        try:
            creds_json = json.loads(cred_json_env)
        except Exception as exc:
            # try to fix escaped newlines
            fixed = cred_json_env.replace('\\n', '\n')
            creds_json = json.loads(fixed)

    if not creds_json:
        app.logger.error("Google Sheets credentials not found. Set GOOGLE_SHEETS_CREDENTIALS_PATH or GOOGLE_SHEETS_CREDENTIALS_JSON")
        return None

    client = gspread.service_account_from_dict(creds_json)
    return client

gclient = get_gspread_client()
sheet = None
if gclient and SHEET_NAME:
    try:
        sh = gclient.open(SHEET_NAME)
        sheet = sh.worksheet(SHEET_TAB) if SHEET_TAB else sh.sheet1
    except Exception as e:
        app.logger.error("Could not open sheet: %s", e)
        sheet = None
else:
    app.logger.warning("Google Sheets client or sheet name not configured.")

# ---------- Conversation state ----------
# In-memory dictionary: { user_id: state_dict }
# state_dict: {step: string/int, responses: dict}
user_states = {}

# ---------- Helper: sending WhatsApp messages ----------
def send_whatsapp_message(to, text, buttons=None):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
    }
    if buttons:
        data.update({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {"buttons": [{"type": "reply", "reply": {"id": b['id'], "title": b['title']}} for b in buttons]}
            }
        })
    else:
        data.update({"type": "text", "text": {"body": text}})
    try:
        requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data, timeout=10)
    except Exception as e:
        app.logger.exception("Failed to send whatsapp message: %s", e)

# ---------- Validation ----------
def is_valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and p.netloc != ""
    except:
        return False

def is_valid_appstore_link(url):
    return url.startswith("https://apps.apple.com")

def is_valid_playstore_link(url):
    return url.startswith("https://play.google.com")

def is_number(text):
    text = text.strip().replace(",", "")
    return re.fullmatch(r"\d+(\.\d+)?", text) is not None

def parse_number(text):
    return float(text.strip().replace(",", ""))

# ---------- Valuation calculation (matches your Supabase function logic) ----------
def compute_valuation(profit_value, revenue_types_str):
    profit_num = float(profit_value or 0)
    rt = (revenue_types_str or "").lower()

    valuation_min = valuation_max = estimated = 0.0
    formatted = ""

    if profit_num <= 0 or profit_num < 1000:
        valuation_min = valuation_max = estimated = 1000.0
        formatted = "$1,000"
    elif "ad" in rt and "sub" not in rt and "iap" not in rt and len(rt.strip()) > 0:
        # If only ad selected
        valuation_min = profit_num * 1.0
        valuation_max = profit_num * 1.7
        estimated = (valuation_min + valuation_max) / 2
        formatted = f"{valuation_min:,.2f} to {valuation_max:,.2f}"
    elif "sub" in rt or "subscription" in rt or "iap" in rt or "others" in rt or "other" in rt:
        valuation_min = profit_num * 1.5
        valuation_max = profit_num * 2.3
        estimated = (valuation_min + valuation_max) / 2
        formatted = f"{valuation_min:,.2f} to {valuation_max:,.2f}"
    else:
        # fallback multiplier
        multiplier = 2.5
        estimated = profit_num * multiplier
        valuation_min = valuation_max = estimated
        formatted = f"{estimated:,.2f}"

    return {
        "valuationMin": round(valuation_min, 2),
        "valuationMax": round(valuation_max, 2),
        "estimatedValuation": round(estimated, 2),
        "formattedValuation": formatted
    }

# ---------- Save to Google Sheet ----------
def save_to_sheet_row(row_data):
    """
    row_data: dict mapping header -> value
    We'll append as a row. Save headers if sheet empty.
    """
    if not sheet:
        app.logger.warning("Sheet not configured - skipping save")
        return False, "Sheet not configured"

    try:
        values = sheet.get_all_values()
        if not values or len(values) == 0:
            headers = list(row_data.keys())
            sheet.append_row(headers)
        row = [row_data.get(k, "") for k in list(row_data.keys())]
        sheet.append_row(row)
        return True, None
    except Exception as e:
        app.logger.exception("Failed to save to sheet: %s", e)
        return False, str(e)

# ---------- Send email via SMTP (Gmail) ----------
def send_email(to_email, subject, html_body, cc_list=None, from_addr=None):
    from_addr = from_addr or SMTP_USER
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.set_content(re.sub('<[^<]+?>', '', html_body))  # plaintext fallback
    msg.add_alternative(html_body, subtype='html')

    all_recipients = [to_email] + (cc_list or [])

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg, from_addr=from_addr, to_addrs=all_recipients)
        server.quit()
        return True, None
    except Exception as e:
        app.logger.exception("Failed to send email: %s", e)
        return False, str(e)

# ---------- Build valuation email HTML ----------
def build_valuation_email_html(name, formatted_valuation, app_link):
    safe_name = name or "there"
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width:600px;">
      <p>Hi {safe_name},</p>
      <p>Thank you for using our valuation tool — based on the details you provided, here is your app's estimated valuation:</p>
      <h2 style="color:#007bff;">{formatted_valuation}</h2>
      <p>This is an estimate; final valuation may vary.</p>
      <p>App link: {app_link or '—'}</p>
      <p>Best regards,<br/>KalaGato Team</p>
    </div>
    """
    return html

# ---------- Webhook endpoint ----------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Invalid verification token", 403

    data = request.get_json(silent=True) or {}
    # basic safety
    if "entry" not in data:
        return "OK", 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", []) or []
            for msg in messages:
                user_id = msg.get("from")
                text = msg.get("text", {}).get("body", "").strip()
                button_reply_id = msg.get("interactive", {}).get("button_reply", {}).get("id")
                # guard
                if not user_id:
                    continue

                state = user_states.get(user_id)

                # New user starts here
                if not state:
                    # greet when user says hi/hello or any first message
                    send_whatsapp_message(
                        user_id,
                        "Hi, I am Kalagato AI Agent. Are you interested in selling your app?",
                        buttons=[{"id":"yes","title":"Yes"},{"id":"no","title":"No"}]
                    )
                    user_states[user_id] = {"step": "await_greeting", "responses": {}}
                    continue

                step = state.get("step")

                # Handle initial yes/no
                if step == "await_greeting":
                    reply = (button_reply_id or text).strip().lower()
                    if reply in ("no", "n"):
                        send_whatsapp_message(user_id, "Thanks — if you have any queries contact us on aman@kalagato.co")
                        user_states.pop(user_id, None)
                        continue
                    elif reply in ("yes", "y"):
                        # start chain: ask name
                        state["step"] = "ask_name"
                        send_whatsapp_message(user_id, "Great — what's your name?")
                        continue
                    else:
                        send_whatsapp_message(user_id, "Please select Yes or No.", buttons=[{"id":"yes","title":"Yes"},{"id":"no","title":"No"}])
                        continue

                # Ask name
                if step == "ask_name":
                    name = text or button_reply_id or ""
                    if not name:
                        send_whatsapp_message(user_id, "Please tell us your name.")
                        continue
                    state["responses"]["name"] = name.strip()
                    state["step"] = "ask_listing"
                    # ask listing with buttons
                    send_whatsapp_message(
                        user_id,
                        "Is your app listed on Play Store, App Store, or Both?",
                        buttons=[{"id":"playstore","title":"Play Store"},{"id":"appstore","title":"App Store"},{"id":"both","title":"Both"}]
                    )
                    continue

                # Handle listing selection
                if step == "ask_listing":
                    listing = (button_reply_id or text).strip().lower()
                    # normalize
                    if listing in ("playstore","play store","play"):
                        state["responses"]["listing"] = "playstore"
                    elif listing in ("appstore","app store","app"):
                        state["responses"]["listing"] = "appstore"
                    elif listing in ("both","both stores"):
                        state["responses"]["listing"] = "both"
                    else:
                        send_whatsapp_message(user_id, "Please choose Play Store, App Store, or Both.", buttons=[{"id":"playstore","title":"Play Store"},{"id":"appstore","title":"App Store"},{"id":"both","title":"Both"}])
                        continue

                    # next: ask for the relevant link(s)
                    if state["responses"]["listing"] == "playstore":
                        state["step"] = "ask_play_link"
                        send_whatsapp_message(user_id, "Please provide the Play Store link (https://play.google.com/...)")
                    elif state["responses"]["listing"] == "appstore":
                        state["step"] = "ask_app_link"
                        send_whatsapp_message(user_id, "Please provide the App Store link (https://apps.apple.com/...)")
                    else:
                        # both
                        state["step"] = "ask_play_link"
                        state["sub_next"] = "ask_app_link"  # chain to ask app link next
                        send_whatsapp_message(user_id, "Please provide the Play Store link (https://play.google.com/...)")
                    continue

                # Play store link
                if step == "ask_play_link":
                    link = text.strip()
                    if not is_valid_playstore_link(link):
                        send_whatsapp_message(user_id, "❌ Invalid Play Store link. Please provide a valid URL starting with https://play.google.com")
                        continue
                    state["responses"]["play_link"] = link
                    # either go to app link next or continue to next numeric questions
                    if state.get("sub_next") == "ask_app_link":
                        state["step"] = "ask_app_link"
                        state.pop("sub_next", None)
                        send_whatsapp_message(user_id, "Please provide the App Store link (https://apps.apple.com/...) or reply 'skip' to skip.")
                        continue
                    else:
                        # proceed to revenue questions
                        state["step"] = "ask_revenue"
                        send_whatsapp_message(user_id, "What is your annual revenue (USD)? Reply with numbers only.")
                        continue

                # App store link
                if step == "ask_app_link":
                    if text.strip().lower() == "skip":
                        state["responses"]["app_link"] = ""
                        state["step"] = "ask_revenue"
                        send_whatsapp_message(user_id, "What is your annual revenue (USD)? Reply with numbers only.")
                        continue
                    link = text.strip()
                    if not is_valid_appstore_link(link):
                        send_whatsapp_message(user_id, "❌ Invalid App Store link. Please provide a valid URL starting with https://apps.apple.com or 'skip' to skip.")
                        continue
                    state["responses"]["app_link"] = link
                    state["step"] = "ask_revenue"
                    send_whatsapp_message(user_id, "What is your annual revenue (USD)? Reply with numbers only.")
                    continue

                # Numeric questions in order: revenue, marketing cost, server cost, profit
                if step == "ask_revenue":
                    if not is_number(text):
                        send_whatsapp_message(user_id, "❌ Please enter a number for annual revenue (digits only).")
                        continue
                    state["responses"]["annual_revenue"] = text.strip()
                    state["step"] = "ask_marketing"
                    send_whatsapp_message(user_id, "What is your annual marketing cost (USD)? Enter numbers only.")
                    continue

                if step == "ask_marketing":
                    if not is_number(text):
                        send_whatsapp_message(user_id, "❌ Please enter a number for marketing cost.")
                        continue
                    state["responses"]["marketing_cost"] = text.strip()
                    state["step"] = "ask_server"
                    send_whatsapp_message(user_id, "What is your annual server cost (USD)? Enter numbers only.")
                    continue

                if step == "ask_server":
                    if not is_number(text):
                        send_whatsapp_message(user_id, "❌ Please enter a number for server cost.")
                        continue
                    state["responses"]["server_cost"] = text.strip()
                    state["step"] = "ask_profit"
                    send_whatsapp_message(user_id, "What is your annual profit (USD)? Enter numbers only.")
                    continue

                if step == "ask_profit":
                    if not is_number(text):
                        send_whatsapp_message(user_id, "❌ Please enter a number for annual profit.")
                        continue
                    state["responses"]["annual_profit"] = text.strip()
                    # revenue type: ask to reply with comma separated options
                    state["step"] = "ask_revenue_types"
                    send_whatsapp_message(user_id, "Which revenue types? Reply using commas (Ad, Subscription, IAP). Example: Ad, IAP")
                    continue

                if step == "ask_revenue_types":
                    # accept comma separated values
                    types_text = text.strip()
                    if not types_text:
                        send_whatsapp_message(user_id, "Please reply with revenue types (Ad, Subscription, IAP) separated by commas.")
                        continue
                    # normalize
                    chosen = [t.strip().lower() for t in types_text.split(",") if t.strip()]
                    state["responses"]["revenue_types"] = ",".join(chosen)
                    state["step"] = "ask_email"
                    send_whatsapp_message(user_id, "Please share your email address (we will send valuation there).")
                    continue

                if step == "ask_email":
                    email = text.strip()
                    # basic email validation
                    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                        send_whatsapp_message(user_id, "❌ Please provide a valid email address.")
                        continue
                    state["responses"]["email"] = email
                    # Done collecting — compute valuation, save to sheet, call supabase function, send email
                    responses = state["responses"]
                    # compute valuation
                    val = compute_valuation(responses.get("annual_profit", "0"), responses.get("revenue_types", ""))
                    estimated_val = val["estimatedValuation"]
                    formatted_val = val["formattedValuation"] if val["formattedValuation"] else f"${estimated_val:,.2f}"

                    # save to sheet (columns: Timestamp, WhatsApp, Name, Listing, AppStoreLink, PlayStoreLink,
                    # AnnualRevenue, MarketingCost, ServerCost, AnnualProfit, RevenueTypes, EstimatedValuation, Email)
                    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    row_map = {
                        "Timestamp": now,
                        "WhatsApp": user_id,
                        "Name": responses.get("name",""),
                        "Listing": responses.get("listing",""),
                        "AppStoreLink": responses.get("app_link",""),
                        "PlayStoreLink": responses.get("play_link",""),
                        "AnnualRevenue": responses.get("annual_revenue",""),
                        "MarketingCost": responses.get("marketing_cost",""),
                        "ServerCost": responses.get("server_cost",""),
                        "AnnualProfit": responses.get("annual_profit",""),
                        "RevenueTypes": responses.get("revenue_types",""),
                        "EstimatedValuation": str(estimated_val),
                        "Email": responses.get("email","")
                    }
                    ok, err = save_to_sheet_row(row_map)
                    if not ok:
                        send_whatsapp_message(user_id, "✅ Saved your data, but we couldn't save to Google Sheets. Please contact aman@kalagato.co if needed.")
                    else:
                        # call supabase function if set (do not rely on this for email)
                        if SUPABASE_FUNCTION_URL:
                            try:
                                requests.post(SUPABASE_FUNCTION_URL, json={**responses, "estimatedValuation": estimated_val}, timeout=6)
                            except Exception as ex:
                                app.logger.warning("Failed to call supabase function: %s", ex)

                    # send email using SMTP from this app (so we can CC)
                    html = build_valuation_email_html(responses.get("name",""), formatted_val, responses.get("app_link") or responses.get("play_link"))
                    sent, send_err = send_email(responses.get("email"), "Your App Valuation Estimate is Here!", html, cc_list=CC_EMAILS, from_addr=SMTP_USER)
                    if sent:
                        send_whatsapp_message(user_id, f"✅ Thank you {responses.get('name','')}. We've sent your valuation ({formatted_val}) to your email.")
                    else:
                        send_whatsapp_message(user_id, "✅ Saved your data, but we couldn't send the email automatically. Please contact aman@kalagato.co if needed.")
                        app.logger.error("Email send error: %s", send_err)

                    # cleanup
                    user_states.pop(user_id, None)
                    continue

                # fallback
                send_whatsapp_message(user_id, "Sorry — I didn't understand. Please follow the prompts.")
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=(LOG_LEVEL=="DEBUG"))
