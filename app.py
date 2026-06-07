import os
import json
import logging
import requests
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
from services.pdf_service import generate_invoice_pdf

load_dotenv()

app = Flask(__name__)

# Basic logging setup
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
# terminal log if supabase_url is missing
if not SUPABASE_URL:
    logger.warning("Supabase URL is missing. Database features will be disabled.")
SUPABASE_URL = (SUPABASE_URL or "").rstrip("/")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or os.environ.get("SUPABASE_ANON_KEY")  
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Initialize Supabase only if configured
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        supabase = None
        logger.error(
            "Supabase init failed: %s. "
            "Use a JWT key (legacy anon/service_role) for supabase-py. "
            "If your key starts with 'sb_', switch to the project's legacy JWT key.",
            e,
        )
else:
    supabase = None

def send_telegram_message(chat_id, text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'your_telegram_bot_token_here':
        logger.warning("Telegram Bot Token is missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }

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

def load_sample_invoice():
    sample_path = Path(__file__).resolve().parent / "assets" / "sample_invoice.json"
    with sample_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)

def reserve_invoice_record(chat_id, attention):
    if not supabase:
        return None

    response = supabase.table("generated_invoices").insert({
        "chat_id": chat_id,
        "attention": attention,
        "status": "generating"
    }).execute()
    return response.data[0] if response.data else None

def finalize_invoice_record(record_id, file_name, public_url):
    if not supabase or not record_id:
        return

    supabase.table("generated_invoices").update({
        "file_path": file_name,
        "public_url": public_url,
        "status": "completed"
    }).eq("id", record_id).execute()

def generate_and_store_invoice(invoice_data, chat_id=None, username=None, source="local", notify_telegram=False):
    attention = invoice_data.get("attn", "unknown")
    record = reserve_invoice_record(chat_id, attention)
    invoice_id = str(record["id"]) if record and record.get("id") is not None else "00000"

    invoice_data = dict(invoice_data)
    invoice_data["invoice_no"] = invoice_id

    attn_slug = ''.join(e for e in str(attention) if e.isalnum())
    timestamp = datetime.now().strftime("%d%m%y-%H%M")
    file_name = f"invoice-{attn_slug}-{timestamp}.pdf"
    output_path = os.path.join('/tmp', file_name) if os.name != 'nt' else file_name

    generate_invoice_pdf(invoice_data, output_path)

    if not os.path.exists(output_path):
        raise FileNotFoundError(
            f"PDF generation completed but file not found: {output_path}"
        )
    logger.info(f"Generated PDF saved to {output_path}")

    public_url = ""
    if supabase:
        try:
            with open(output_path, 'rb') as f:
                supabase.storage.from_("invoices").upload(file_name, f)
                logger.info(f"Uploaded {file_name} to Supabase Storage.")
            public_url = supabase.storage.from_("invoices").get_public_url(file_name)
            finalize_invoice_record(record["id"] if record else None, file_name, public_url)
        except Exception as e:
            logger.error(f"Supabase Storage Upload Error: {e}")

    log_event_to_db(
        chat_id,
        username or source,
        f"{source}_invoice_generated",
        f"Generated {file_name}",
        invoice_data,
        "success"
    )

    if notify_telegram and chat_id:
        send_telegram_document(chat_id, output_path)
        if public_url:
            send_telegram_message(chat_id, f"Stored link: {public_url}")

    return {
        "invoice_no": invoice_id,
        "file_name": file_name,
        "output_path": output_path,
        "public_url": public_url,
        "record_id": record["id"] if record else None,
    }

@app.route("/")
def index():
    return jsonify({"status": "running", "message": "Render-Link Bot Backend Active"})

@app.route("/local/health", methods=["GET"])
def local_health():
    db_ok = False
    db_error = None

    if supabase:
        try:
            supabase.table("generated_invoices").select("id").limit(1).execute()
            db_ok = True
        except Exception as e:
            db_error = str(e)

    return jsonify({
        "ok": True,
        "supabase_configured": bool(supabase),
        "supabase_db_ok": db_ok,
        "supabase_db_error": db_error,
        "pdf_ready": True,
    })

@app.route("/local/generate", methods=["POST", "GET"])
def local_generate():
    if request.method == "POST" and request.is_json:
        invoice_data = request.get_json(silent=True) or {}
    else:
        invoice_data = load_sample_invoice()

    result = generate_and_store_invoice(
        invoice_data=invoice_data,
        chat_id=None,
        username="local",
        source="local",
        notify_telegram=False,
    )

    return jsonify({
        "ok": True,
        **result,
    })

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
            res = supabase.table("webhook_events").select("*").limit(10).order("received_at", desc=True).execute()
            events = res.data
            msg = "Recent Queue Activity:\n\n"
            msg += "<pre>"
            msg += f"{'SUMMARY':50} {'STATUS':10} {'USERNAME':20}\n"
            msg += "-" * 80 + "\n"
            n = 1
            for ev in events:
                summary = escape(str(ev.get("summary", "")))
                status = escape(str(ev.get("status", "")))
                username = escape(str(ev.get("username", "unknown")))

                if isinstance(summary, dict):
                    summary = str(summary)

                msg += f"{n}. {summary[:50]:50} {status[:10]:10} {username[:20]:20}\n"
                n += 1

            msg += "</pre>"

            send_telegram_message(chat_id, msg, "MarkdownV2")
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
            # this will change text example from as qwe: adss\nattn: aspppp to a json format like {"qwe": "adss", "attn": "aspppp"}
            invoice_data = json.loads(text)
            send_telegram_message(chat_id, "JSON received. Generating PDF, please wait...")
            result = generate_and_store_invoice(
                invoice_data=invoice_data,
                chat_id=chat_id,
                username=username,
                source="telegram",
                notify_telegram=True,
            )
            
            # Clean up
            update_bot_state(chat_id, "IDLE")
            logger.info(f"Generated invoice {result['invoice_no']} -> {result['file_name']}")
            
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
