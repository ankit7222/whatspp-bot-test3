import os
import json
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, jsonify

app = Flask(__name__)

GOOGLE_SHEETS_CREDENTIALS = '{"type":"service_account","project_id":"developer-lists","private_key_id":"66363c6d2c2effb8269dcab29ae6d9cbc8f05226","private_key":"-----BEGIN PRIVATE KEY-----\\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCdpxhaaCiUpFQT\\nWe9lY1nQ3X7LY3mBFjPZ67qVZaHXFdKLe3pO7a45ttBJX5k1deZuKE9fBZs4tokm\\nprYXBB3q3CJFCttb8L3o/qixRBko5OcAkM0Mffv4Dq+I3QZQFVPfVSGSS+m/FDXI\\nxXuaLLX3v8B6qs+XufRmPbx5uhfDowfcCT3Mlzu8X0Wqs+QJV0R81N5WLeYbY9xU\\ncDyR/1TlqL0Ad8AWrxETHOIoKIJj+ae+P+8LzoZxxtrAIsYmWtLUQl5OWrNeCmLV\\nHKA1LiFZJjsqLP3zzDNvUOH8joYMhoJqqkW2uq0P4ZEVoBNwX9McfemTOXulnSLK\\nkyuv0eH7AgMBAAECggEAAwGUucz3HGeV9TLe3xN4gL6WUYFa8133yfc75bkfLJgY\\n+qV1hzA4nH/xPF5lcC9llGtX5wWLZDnGeLydtlwBgpViSbliOp1yuCgpKQznPV7M\\nCE+XotJ7RQXGqqdlYmAApXjLVDk8n8ReTIwCBlHRHwRUFCK63xpKmkLbvrKnxc5B\\n98OMrYuCEIM1TnSv+muVeiqZXl0E2E0LLRgNulqAmI2XA4HIThP4rCRJlA4hlpGy\\nCF5KpjuMVMSW47kZ6X15a+0AWfgx7vpu1XjU1aeyQSJ26FaZgr5UIbbAk3Uniast\\nhP8GybDi4HpsSchKRRern5Yowoq4VSFYzRDzxlDZTQKBgQDM5Xj8WhyFhlJTmU1h\\nBnONOe8iX6SQ1PC67Qa9cBE2FrtKGVuZq0qCkaIexESmclIrtWrr3WQAYcXTbX8W\\n3cLu6rLiiLSMN/01j0d38a0/mSuEqiSOz2f4YX7dcQ2nZEEa//Aq65Q93aUsVQV0\\nUPaQ87g1ASZVu75mMT6e1SZydwKBgQDE+SOj2/ozUzRqrHTFlbWPs5ZzA/MKa4XZ\\n6QlTu3KWE6eUoK693v5sfur1YOB1g5vyBCpxA6xcfL2squeViYugZZO1wE3Vv+UV\\nOz+4Uvak0CeyfXPC1BXMQexUE+NBbqnazl5pkg3IR2BuIiwL7Fmbs5Xk/t1c5oS3\\n4JEinuSJnQKBgERRj1G1SiVLcE/noeFkIUtJse6oLVsNZWcueTzZDSQX2EMQyXYn\\noyR+IqxXjPxiyftA9nHG0/08nJWuwN2C++hl4VefdXP7hzZAm/fmYXn/PH9zq9Ti\\nWyx6da6ob4EM8JhsFkx5WGh4awapIrRx+oTCfv1NcNbNTuMMMHENaVBpAoGBAIW0\\n9Qt6/JkwhulOjam+GVQlvR/v82AEYwTr2of7OypCx0Pt2xBKOfzeHpJYo6VBpG8h\\ngsnai3rwtjRqgu+QQbasnRsIIg3RyCikYnm133U7U2cnH5iGLRHNQiZEpcQ54ZUE\\n9zPEkBR+1yeLjMi/NIir3DlpBEzWsgq7pumQYGRFAoGBAII2YHq7pDsi4sO3gtO6\\nF3xm41iczXh8LVhn5jO1L8e2Ek3oqF760sHmsDik+ga54K6KXBEh2A1WuHW9lUxB\\nJtEaLb5CqkKfKmLF9UyF5+vUp1tS0w9jlv/DZmv/GsUAKumUh8XOKvXMrJ0/DysW\\new7NjVNuuHs34JdssuU09XBu\\n-----END PRIVATE KEY-----\\n","client_email":"whatapp-bot@developer-lists.iam.gserviceaccount.com","client_id":"107692818249201705280","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs","client_x509_cert_url":"https://www.googleapis.com/robot/v1/metadata/x509/whatapp-bot%40developer-lists.iam.gserviceaccount.com","universe_domain":"googleapis.com"}'

creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(credentials)

SHEET_ID = "1V9c_MpFj3ttW6Yeu2WE8FsLuSgRhO1B87sHhxJswIlk"
sheet = client.open_by_key(SHEET_ID).sheet1

def is_number(value):
    try:
        float(value)
        return True
    except:
        return False

def is_valid_playstore_link(link):
    return bool(re.match(r'https://play\.google\.com/store/apps/details\?id=.*', link))

def is_valid_appstore_link(link):
    return bool(re.match(r'https://apps\.apple\.com/.*', link))

@app.route('/add_data', methods=['POST'])
def add_data():
    data = request.json

    app_listing = data.get("app_listing", "")
    app_link_playstore = data.get("app_link_playstore", "")
    app_link_appstore = data.get("app_link_appstore", "")
    last_12_month_revenue = data.get("last_12_month_revenue", "")
    last_12_month_profit = data.get("last_12_month_profit", "")
    last_12_month_spend = data.get("last_12_month_spend", "")
    monthly_profit_avg = data.get("monthly_profit_avg", "")
    options = data.get("options", [])

    # Validate numeric fields
    for field in [last_12_month_revenue, last_12_month_profit, last_12_month_spend, monthly_profit_avg]:
        if field and not is_number(field):
            return jsonify({"status": "error", "message": "Numeric values required for revenue, profit, spend, monthly avg"}), 400

    # Validate links
    if app_listing in ["Play Store", "Both"] and not is_valid_playstore_link(app_link_playstore):
        return jsonify({"status": "error", "message": "Invalid Play Store link"}), 400
    if app_listing in ["App Store", "Both"] and not is_valid_appstore_link(app_link_appstore):
        return jsonify({"status": "error", "message": "Invalid App Store link"}), 400

    # Revenue source yes/no
    iap_revenue = "Yes" if "IAP Revenue" in options else "No"
    subscription_revenue = "Yes" if "Subscription Revenue" in options else "No"
    ad_revenue = "Yes" if "Ad Revenue" in options else "No"

    # Skip non-selected store links
    if app_listing == "App Store":
        app_link_playstore = ""
    elif app_listing == "Play Store":
        app_link_appstore = ""

    sheet.append_row([
        app_listing,
        app_link_playstore,
        app_link_appstore,
        last_12_month_revenue,
        last_12_month_profit,
        last_12_month_spend,
        monthly_profit_avg,
        iap_revenue,
        subscription_revenue,
        ad_revenue
    ])

    return jsonify({"status": "success", "message": "Data added successfully"})

if __name__ == '__main__':
    app.run(debug=True)
