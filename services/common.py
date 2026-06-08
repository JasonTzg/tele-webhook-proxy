import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
if not SUPABASE_URL:
    logger.warning("Supabase URL is missing. Database features will be disabled.")
SUPABASE_URL = (SUPABASE_URL or "").rstrip("/")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or os.environ.get("SUPABASE_ANON_KEY")
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

BOT_STATE_IDLE = "IDLE"
BOT_STATE_WAITING_MODE = "WAITING_FOR_INVOICE_MODE"
BOT_STATE_COLLECTING = "COLLECTING_INVOICE"
BOT_STATE_REVIEW = "REVIEW_INVOICE"

SESSION_STATUS_DRAFT = "draft"
SESSION_STATUS_COLLECTING = "collecting"
SESSION_STATUS_REVIEW = "review"
SESSION_STATUS_COMPLETED = "completed"
SESSION_STATUS_ARCHIVED = "archived"

MODE_JSON = "json"
MODE_GUIDED = "guided"

MODE_JSON_CALLBACK = "invoice_mode_json"
MODE_GUIDED_CALLBACK = "invoice_mode_guided"
CONFIRM_CALLBACK = "invoice_confirm_generate"
CANCEL_CALLBACK = "invoice_cancel_session"

TOP_LEVEL_PROMPTS = [
    ("attn", "1. Client name? (attn)"),
    ("tel", "2. Client number? (tel)"),
    ("invoice_date", "3. Invoice date? Put . for Current date."),
    ("billing_address", "4. Billing address? Put \\n if you want go to next line."),
    ("delivery_address", "5. Delivery address? If same as Billing address, put ."),
    ("items_count", "6. How many items/products?"),
]

ITEM_PROMPTS = [
    ("description", "Description/Product Name Selling?"),
    ("material", "Material?"),
    ("size", "Size?"),
    ("remarks", "Remarks? [This is optional]"),
    ("qty", "Qty?"),
    ("unit_price", "Unit Price?"),
]

GUIDED_EDITABLE_FIELDS = {"attn", "tel", "invoice_date", "billing_address", "delivery_address"}
ITEM_EDITABLE_FIELDS = {"description", "material", "size", "remarks", "qty", "unit_price"}

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        supabase = None
        logger.error(
            "Supabase init failed: %s. Use a JWT key (legacy anon/service_role) for supabase-py. If your key starts with 'sb_', switch to the project's legacy JWT key.",
            e,
        )
else:
    supabase = None


def now_iso():
    return datetime.utcnow().isoformat()


def build_inline_keyboard(button_rows):
    return {"inline_keyboard": button_rows}


def send_telegram_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
        logger.warning("Telegram Bot Token is missing.")
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)


def answer_telegram_callback(callback_query_id, text=None):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
        return

    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json=payload)


def send_telegram_document(chat_id, file_path):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
        logger.warning("Telegram Bot Token is missing.")
        return

    with open(file_path, "rb") as handle:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": handle},
        )


def log_event_to_db(chat_id, username, event_type, summary, payload_json, status="success"):
    if not supabase:
        return
    try:
        supabase.table("webhook_events").insert(
            {
                "chat_id": chat_id,
                "username": username,
                "event_type": event_type,
                "summary": summary,
                "payload": payload_json,
                "status": status,
            }
        ).execute()
    except Exception as e:
        logger.error("DB Log Error: %s", e)


def get_bot_state(chat_id):
    if not supabase:
        return BOT_STATE_IDLE
    res = supabase.table("bot_states").select("state").eq("chat_id", chat_id).execute()
    if res.data:
        return res.data[0]["state"]
    return BOT_STATE_IDLE


def update_bot_state(chat_id, new_state):
    if not supabase:
        return
    try:
        supabase.table("bot_states").upsert(
            {
                "chat_id": chat_id,
                "state": new_state,
                "updated_at": now_iso(),
            }
        ).execute()
    except Exception as e:
        logger.error("State Update Error: %s", e)
