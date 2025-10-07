import os
import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import requests

app = Flask(__name__)

# ================= GOOGLE SHEETS SETUP =================
def get_google_sheet():
    try:
        creds_json = json.loads(os.getenv("GOOGLE_SHEETS_CREDENTIALS"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
        client = gspread.authorize(creds)
        sheet_id = os.getenv("SHEET_ID")
        sheet = client.open_by_key(sheet_id).sheet1
        return sheet
    except Exception as e:
        app.logger.error(f"Google Sheets auth failed: {e}")
        return None


# ================= EMAIL SETUP =================
def send_valuation_email(to_email, name, valuation_text):
    try:
        gmail_user = os.getenv("GMAIL_USER")
        gmail_pass = os.getenv("GMAIL_APP_PASS")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your App Valuation Estimate"
        msg["From"] = gmail_user
        msg["To"] = to_email

        text = f"Hi {name},\n\nYour app valuation estimate is:\n{valuation_text}\n\nThanks,\nTeam Kalagato"
        html = f"""
        <html><body>
        <p>Hi {name},</p>
        <p>Thank you for sharing your details.</p>
        <p><b>Your estimated app valuation:</b><br>{valuation_text}</p>
        <p>If you’d like to discuss further, schedule a call here:<br>
        <a href="https://calendly.com/ankit-yadav-kalagato/30min">Book a 30-Min Meeting</a></p>
        <p>– Team Kalagato</p>
        </body></html>
        """

        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(gmail_user, gmail_pass)
            server.send_message(msg)

        app.logger.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        app.logger.error(f"Email send failed: {e}")
        return False


# ================= WHATSAPP SETUP =================
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

WHATSAPP_API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json",
}

# ================= USER STATE =================
user_states = {}


def send_whatsapp_message(to, text, buttons=None):
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive" if buttons else "text",
    }
    if buttons:
        data["interactive"] = {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.lower(), "title": b}}
                    for b in buttons
                ]
            },
        }
    else:
        data["text"] = {"body": text}
    requests.post(WHATSAPP_API_URL, headers=HEADERS, json=data)


def calculate_valuation(profit, revenue_type):
    profit = float(profit)
    valuation_text = ""
    if profit <= 0 or profit < 1000:
        valuation_text = "$1,000 (minimum)"
    elif "ad" in revenue_type.lower():
        valuation_text = f"${profit * 1.0:,.0f} - ${profit * 1.7:,.0f}"
    elif "sub" in revenue_type.lower() or "iap" in revenue_type.lower():
        valuation_text = f"${profit * 1.5:,.0f} - ${profit * 2.3:,.0f}"
    else:
        valuation_text = f"${profit * 2.5:,.0f}"
    return valuation_text


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Webhook verification
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Invalid verification token", 403

    data = request.get_json()
    if not data:
        return "No data", 400

    # Process incoming messages
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue

            msg = messages[0]
            user_id = msg["from"]
            text = msg.get("text", {}).get("body", "").strip()
            button = msg.get("interactive", {}).get("button_reply", {}).get("id")

            # Greeting
            if user_id not in user_states:
                if text.lower() in ["hi", "hello"]:
                    send_whatsapp_message(
                        user_id,
                        "Hi, I am Kalagato AI Agent. Are you interested in selling your app?",
                        ["Yes", "No"],
                    )
                    user_states[user_id] = {"step": "start", "data": {}}
                continue

            state = user_states[user_id]

            # Handle No
            if button == "no":
                send_whatsapp_message(
                    user_id,
                    "Thanks! If you have any queries, contact us at aman@kalagato.co.",
                )
                del user_states[user_id]
                continue

            # Handle Yes
            if button == "yes" and state["step"] == "start":
                send_whatsapp_message(user_id, "What is your name?")
                state["step"] = "name"
                continue

            # Flow control
            if state["step"] == "name":
                state["data"]["name"] = text
                send_whatsapp_message(user_id, "Please provide your App Store or Play Store link (https://...)")
                state["step"] = "link"

            elif state["step"] == "link":
                if not text.startswith("http"):
                    send_whatsapp_message(user_id, "❌ Please send a valid URL starting with http:// or https://")
                    continue
                state["data"]["app_link"] = text
                send_whatsapp_message(user_id, "What is your annual revenue (USD)?")
                state["step"] = "revenue"

            elif state["step"] == "revenue":
                if not text.isdigit():
                    send_whatsapp_message(user_id, "❌ Please enter a number.")
                    continue
                state["data"]["annual_revenue"] = text
                send_whatsapp_message(user_id, "What is your annual marketing cost (USD)?")
                state["step"] = "marketing"

            elif state["step"] == "marketing":
                if not text.isdigit():
                    send_whatsapp_message(user_id, "❌ Please enter a number.")
                    continue
                state["data"]["marketing_cost"] = text
                send_whatsapp_message(user_id, "What is your annual server cost (USD)?")
                state["step"] = "server"

            elif state["step"] == "server":
                if not text.isdigit():
                    send_whatsapp_message(user_id, "❌ Please enter a number.")
                    continue
                state["data"]["server_cost"] = text
                send_whatsapp_message(user_id, "What is your annual profit (USD)?")
                state["step"] = "profit"

            elif state["step"] == "profit":
                if not text.isdigit():
                    send_whatsapp_message(user_id, "❌ Please enter a number.")
                    continue
                state["data"]["annual_profit"] = text
                send_whatsapp_message(
                    user_id,
                    "Which revenue sources apply? Reply with numbers separated by commas:\n1. IAP\n2. Subscription\n3. Ad",
                )
                state["step"] = "revenue_type"

            elif state["step"] == "revenue_type":
                rev_map = {"1": "IAP", "2": "Subscription", "3": "Ad"}
                selections = [rev_map.get(x.strip()) for x in text.split(",") if x.strip() in rev_map]
                state["data"]["revenue_type"] = ", ".join(selections) if selections else "Other"
                send_whatsapp_message(user_id, "Please share your email address (we’ll send valuation there)")
                state["step"] = "email"

            elif state["step"] == "email":
                if "@" not in text or "." not in text:
                    send_whatsapp_message(user_id, "❌ Please provide a valid email address.")
                    continue
                state["data"]["email"] = text
                send_whatsapp_message(user_id, "Phone number (optional). Reply 'skip' to skip.")
                state["step"] = "phone"

            elif state["step"] == "phone":
                if text.lower() != "skip":
                    state["data"]["phone"] = text
                else:
                    state["data"]["phone"] = ""

                data = state["data"]
                valuation = calculate_valuation(data["annual_profit"], data["revenue_type"])

                # Save to Google Sheet
                sheet = get_google_sheet()
                if sheet:
                    sheet.append_row([
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        data.get("name"),
                        data.get("app_link"),
                        data.get("annual_revenue"),
                        data.get("marketing_cost"),
                        data.get("server_cost"),
                        data.get("annual_profit"),
                        data.get("revenue_type"),
                        data.get("email"),
                        data.get("phone"),
                        valuation
                    ])
                else:
                    app.logger.warning("Sheet not connected")

                # Send email
                email_sent = send_valuation_email(data["email"], data["name"], valuation)
                if email_sent:
                    send_whatsapp_message(user_id, "✅ Thank you! We’ve sent your valuation report to your email.")
                else:
                    send_whatsapp_message(user_id, "✅ Saved your data, but couldn’t send email automatically. Please contact aman@kalagato.co if needed.")

                del user_states[user_id]

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
