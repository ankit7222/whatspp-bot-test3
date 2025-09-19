import os
import datetime
from flask import Flask, request, jsonify
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# ------------------- GOOGLE SHEETS SETUP -------------------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

SHEET_NAME = "YourSheetName"  # <-- replace with your Google Sheet name
sheet = client.open(SHEET_NAME).sheet1

# ------------------- QUESTIONS -------------------
questions = {
    "listing": "Is your app listed on App Store, Play Store, or Both?",
    "app_store_link": "Please share your App Store link:",
    "play_store_link": "Please share your Play Store link:",
    "revenue": "What was your last 12 months revenue (number only)?",
    "profit": "What was your last 12 months profit (number only)?",
    "spends": "What was your last 12 months spends (number only)?",
    "monthly_profit": "What is your Monthly Profit (number only)?",
    "dau": "How many Daily Active Users (DAU)?",
    "mau": "How many Monthly Active Users (MAU)?"
}

flow_order = ["listing", "app_store_link", "play_store_link",
              "revenue", "profit", "spends",
              "monthly_profit", "dau", "mau"]

# Track user answers in memory (in production use DB/Redis)
user_answers = {}

# ------------------- NEXT QUESTION LOGIC -------------------
def get_next_question(user_number):
    answers = user_answers.get(user_number, {})

    if "listing" not in answers:
        return "listing"

    listing = answers.get("listing", "").lower()

    if listing == "app store":
        if "app_store_link" not in answers:
            return "app_store_link"
        return next_in_order(answers, skip="play_store_link")

    elif listing == "play store":
        if "play_store_link" not in answers:
            return "play_store_link"
        return next_in_order(answers, skip="app_store_link")

    elif listing == "both":
        if "app_store_link" not in answers:
            return "app_store_link"
        if "play_store_link" not in answers:
            return "play_store_link"
        return next_in_order(answers)

    return next_in_order(answers)

def next_in_order(answers, skip=None):
    for q in flow_order:
        if skip and q == skip:
            continue
        if q not in answers:
            return q
    return None

# ------------------- SAVE TO SHEETS -------------------
def save_to_google_sheets(user_number, answers):
    date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = [
        date,
        user_number,
        answers.get("listing", ""),
        answers.get("app_store_link", ""),
        answers.get("play_store_link", ""),
        answers.get("revenue", ""),
        answers.get("profit", ""),
        answers.get("spends", ""),
        answers.get("monthly_profit", ""),
        answers.get("dau", ""),
        answers.get("mau", "")
    ]
    sheet.append_row(row)

    # Highlight monthly profit if >= 7000
    try:
        monthly_profit = float(answers.get("monthly_profit", 0))
        if monthly_profit >= 7000:
            last_row = len(sheet.get_all_values())
            sheet.format(f"I{last_row}", {  # I = Monthly Profit column (after adding WhatsApp Number)
                "backgroundColor": {"red": 0.8, "green": 1, "blue": 0.8}
            })
    except:
        pass

# ------------------- WHATSAPP API -------------------
WHATSAPP_API_URL = "https://graph.facebook.com/v17.0/YOUR_PHONE_NUMBER_ID/messages"
WHATSAPP_TOKEN = "YOUR_PERMANENT_ACCESS_TOKEN"

def send_whatsapp_message(to, text):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(WHATSAPP_API_URL, headers=headers, json=data)

# ------------------- WEBHOOK -------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify_token = "your_verify_token"
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        else:
            return "Verification failed", 403

    if request.method == "POST":
        data = request.get_json()
        try:
            user_number = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
            message_body = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body'].strip()

            if user_number not in user_answers:
                user_answers[user_number] = {}

            # Save the latest answer
            next_q = get_next_question(user_number)
            if next_q:
                user_answers[user_number][next_q] = message_body

            # Get next question
            next_q = get_next_question(user_number)
            if next_q:
                send_whatsapp_message(user_number, questions[next_q])
            else:
                # Save and finish
                save_to_google_sheets(user_number, user_answers[user_number])
                send_whatsapp_message(user_number, "✅ Thanks! Your details have been saved. If you have queries, email us at aman@kalagato.co")
                del user_answers[user_number]

        except Exception as e:
            print("Error:", e)

        return "EVENT_RECEIVED", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
