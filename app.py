import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
from services.pdf_service import generate_invoice_pdf

load_dotenv()

app = Flask(__name__)

# Basic logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Initialize Supabase only if configured
if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None

def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'your_telegram_bot_token_here':
        logger.warning("Telegram Bot Token is missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)

def send_telegram_document(chat_id, file_path):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'your_telegram_bot_token_here':
        logger.warning("Telegram Bot Token is missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(file_path, "rb") as f:
        files = {"document": f}
        data = {"chat_id": chat_id}
        requests.post(url, data=data, files=files)

def log_event_to_db(chat_id, username, event_type, summary, payload_json, status="success"):
    if not supabase: return
    try:
        supabase.table("webhook_events").insert({
            "chat_id": chat_id,
            "username": username,
            "event_type": event_type,
            "summary": summary,
            "payload": payload_json,
            "status": status
        }).execute()
    except Exception as e:
        logger.error(f"DB Log Error: {e}")

def get_bot_state(chat_id):
    if not supabase: return "IDLE"
    res = supabase.table("bot_states").select("state").eq("chat_id", chat_id).execute()
    if res.data and len(res.data) > 0:
        return res.data[0]['state']
    return "IDLE"

def update_bot_state(chat_id, new_state):
    if not supabase: return
    try:
        supabase.table("bot_states").upsert({
            "chat_id": chat_id,
            "state": new_state,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"State Update Error: {e}")

@app.route("/")
def index():
    return jsonify({"status": "running", "message": "Render-Link Bot Backend Active"})

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()
    if not update:
        return "OK", 200

    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    username = message.get("from", {}).get("username", "unknown")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return "OK", 200

    # 1. Log incoming message
    log_event_to_db(chat_id, username, "message", text[:50], update, "received")

    current_state = get_bot_state(chat_id)

    # 2. Command routing
    if text.startswith("/invoice"):
        update_bot_state(chat_id, "WAITING_FOR_INVOICE_JSON")
        send_telegram_message(chat_id, "Please send the invoice JSON data to generate the PDF.")
        return "OK", 200

    elif text.startswith("/queue"):
        # Fetch recent requests
        if supabase:
            res = supabase.table("webhook_events").select("*").limit(5).order("received_at", desc=True).execute()
            events = res.data
            msg = "Recent Queue Activity:\n\n"
            for ev in events:
                msg += f"- {ev['summary']} ({ev['status']})\n"
            send_telegram_message(chat_id, msg)
        else:
            send_telegram_message(chat_id, "Database not configured.")
        return "OK", 200

    elif text.startswith("/cancel"):
        update_bot_state(chat_id, "IDLE")
        send_telegram_message(chat_id, "Operation cancelled.")
        return "OK", 200

    # 3. Handle State-based operations
    if current_state == "WAITING_FOR_INVOICE_JSON":
        try:
            invoice_data = json.loads(text)
            send_telegram_message(chat_id, "JSON received. Generating PDF, please wait...")
            
            # Step A: Insert a placeholder in the DB to securely reserve the next Auto-Increment ID (e.g. 80000)
            db_id = "00000"
            if supabase:
                res = supabase.table("generated_invoices").insert({
                    "chat_id": chat_id,
                    "attention": invoice_data.get("attn", "unknown"),
                    "status": "generating"
                }).execute()
                db_id = str(res.data[0]['id'])
            
            # Step B: Assign this safe ID to the invoice_no
            invoice_data['invoice_no'] = db_id
            
            # Temporary file configuration
            attn_slug = ''.join(e for e in str(invoice_data.get('attn', 'unknown')) if e.isalnum())
            timestamp = datetime.now().strftime("%d%m%y-%H%M")
            file_name = f"invoice-{attn_slug}-{timestamp}.pdf"
            output_path = os.path.join('/tmp', file_name) if os.name != 'nt' else file_name
            
            # Generate PDF
            generate_invoice_pdf(invoice_data, output_path)
            
            # (Optional) Upload to Supabase Storage
            public_url = ""
            if supabase:
                try:
                    with open(output_path, 'rb') as f:
                        res = supabase.storage.from_("invoices").upload(file_name, f)
                        public_url = supabase.storage.from_("invoices").get_public_url(file_name)
                    
                    # Update the originally inserted row with the final URL
                    supabase.table("generated_invoices").update({
                        "file_path": file_name,
                        "public_url": public_url,
                        "status": "completed"
                    }).eq("id", db_id).execute()
                except Exception as e:
                    logger.error(f"Supabase Storage Upload Error: {e}")

            # Send back to Telegram
            send_telegram_document(chat_id, output_path)
            if public_url:
                send_telegram_message(chat_id, f"Stored link: {public_url}")
            
            # Clean up
            update_bot_state(chat_id, "IDLE")
            log_event_to_db(chat_id, username, "invoice_generated", f"Generated {file_name}", invoice_data, "success")
            
        except json.JSONDecodeError:
            send_telegram_message(chat_id, "Invalid JSON format. Please try again or type /cancel to abort.")
        except Exception as e:
            logger.error(f"Generation error: {e}")
            send_telegram_message(chat_id, f"Error generating PDF: {str(e)}")
            update_bot_state(chat_id, "IDLE")

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
