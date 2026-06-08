import json

from flask import jsonify, request
from html import escape

from services.common import (
    BOT_STATE_COLLECTING,
    BOT_STATE_IDLE,
    BOT_STATE_REVIEW,
    BOT_STATE_WAITING_MODE,
    CANCEL_CALLBACK,
    CONFIRM_CALLBACK,
    MODE_GUIDED,
    MODE_GUIDED_CALLBACK,
    MODE_JSON,
    MODE_JSON_CALLBACK,
    SESSION_STATUS_COLLECTING,
    SESSION_STATUS_COMPLETED,
    SESSION_STATUS_REVIEW,
    answer_telegram_callback,
    build_inline_keyboard,
    logger,
    log_event_to_db,
    now_iso,
    send_telegram_message,
    supabase,
    update_bot_state,
)
from services.invoice_logic import (
    apply_edit_to_invoice,
    build_guided_item_queue,
    build_guided_queue,
    build_json_followup_queue,
    build_review_summary,
    create_invoice_session,
    default_invoice_payload,
    extract_edit_command,
    generate_and_store_invoice,
    get_active_invoice_session,
    get_latest_completed_session,
    get_session_invoice,
    get_session_queue,
    load_sample_invoice,
    persist_session,
    set_session_archived,
    set_session_completed,
    set_session_invoice,
)


def send_mode_selection(chat_id):
    reply_markup = build_inline_keyboard(
        [
            [
                {"text": "1) Send JSON data", "callback_data": MODE_JSON_CALLBACK},
                {"text": "2) Send data 1 by 1", "callback_data": MODE_GUIDED_CALLBACK},
            ]
        ]
    )
    send_telegram_message(chat_id, "Choose how you want to send invoice details:", reply_markup=reply_markup)


def send_review_prompt(chat_id, session):
    reply_markup = build_inline_keyboard(
        [
            [
                {"text": "Generate PDF", "callback_data": CONFIRM_CALLBACK},
                {"text": "Cancel", "callback_data": CANCEL_CALLBACK},
            ]
        ]
    )
    invoice = get_session_invoice(session)
    set_session_invoice(session, invoice, queue=[], status=SESSION_STATUS_REVIEW, state=BOT_STATE_REVIEW)
    update_bot_state(chat_id, BOT_STATE_REVIEW)
    send_telegram_message(
        chat_id,
        build_review_summary(invoice) + "\n\nReply with 'attn: New Name' or 'item 1 qty: 10' to edit a field, or use the buttons below.",
        reply_markup=reply_markup,
    )


def send_next_queue_prompt(chat_id, session):
    queue = list((session.get("session_data") or {}).get("queue") or [])
    if not queue:
        send_review_prompt(chat_id, session)
        return
    send_telegram_message(chat_id, queue[0]["prompt"])


def expand_count_into_item_queue(session, item_count):
    invoice = get_session_invoice(session)
    invoice["items"] = [{} for _ in range(item_count)]
    queue = build_guided_item_queue(item_count)
    set_session_invoice(session, invoice, queue=queue, status=SESSION_STATUS_COLLECTING, state=BOT_STATE_COLLECTING)
    return queue


def prompt_active_queue_or_review(chat_id, session):
    queue = get_session_queue(session)
    if queue:
        send_next_queue_prompt(chat_id, session)
    else:
        send_review_prompt(chat_id, session)


def archive_active_session(chat_id):
    session = get_active_invoice_session(chat_id)
    if session:
        set_session_archived(session)
    update_bot_state(chat_id, BOT_STATE_IDLE)


def handle_guided_answer(chat_id, session, text):
    queue = list((session.get("session_data") or {}).get("queue") or [])
    if not queue:
        send_review_prompt(chat_id, session)
        return

    current = queue[0]
    invoice = get_session_invoice(session)
    value = text.strip()

    if current["type"] == "field" and current["name"] == "items_count":
        if not value.isdigit() or int(value) <= 0:
            send_telegram_message(chat_id, "Please enter a valid number of items.")
            send_next_queue_prompt(chat_id, session)
            return
        queue = expand_count_into_item_queue(session, int(value))
        if queue:
            send_next_queue_prompt(chat_id, session)
        else:
            send_review_prompt(chat_id, session)
        return

    if current["type"] == "field":
        field_name = current["name"]
        if field_name == "invoice_date":
            invoice[field_name] = invoice.get(field_name) or value
            if value == ".":
                from datetime import datetime

                invoice[field_name] = datetime.utcnow().strftime("%Y-%m-%d")
        elif field_name == "delivery_address" and value == ".":
            invoice[field_name] = invoice.get("billing_address", "")
        else:
            invoice[field_name] = value
    elif current["type"] == "item":
        index = current["index"]
        while len(invoice["items"]) <= index:
            invoice["items"].append({})
        field_name = current["name"]
        if field_name == "remarks" and value == ".":
            value = ""
        invoice["items"][index][field_name] = value

    queue = queue[1:]
    set_session_invoice(session, invoice, queue=queue, status=SESSION_STATUS_COLLECTING, state=BOT_STATE_COLLECTING)
    if queue:
        send_next_queue_prompt(chat_id, session)
    else:
        send_review_prompt(chat_id, session)


def process_json_invoice_input(chat_id, session, text):
    try:
        raw_invoice = json.loads(text)
    except json.JSONDecodeError:
        send_telegram_message(chat_id, "Invalid JSON format. Please send valid JSON or type /cancel to abort.")
        return

    from services.invoice_logic import normalize_invoice_payload

    invoice = normalize_invoice_payload(raw_invoice)
    queue = build_json_followup_queue(invoice)
    set_session_invoice(session, invoice, queue=queue, status=SESSION_STATUS_COLLECTING, state=BOT_STATE_COLLECTING)

    if queue:
        send_telegram_message(chat_id, "JSON received. I still need a few missing fields before I can generate the PDF.")
        send_next_queue_prompt(chat_id, session)
    else:
        send_review_prompt(chat_id, session)


def complete_session_generation(session, result):
    if not session:
        return
    session_data = dict(session.get("session_data") or {})
    session_data["generated_invoice"] = result
    set_session_completed(session, generated_invoice_id=result.get("record_id"))
    persist_session(session, session_data=session_data, completed_at=now_iso(), state=BOT_STATE_IDLE, status=SESSION_STATUS_COMPLETED)


def handle_callback_query(update):
    callback_query = update.get("callback_query", {})
    callback_id = callback_query.get("id")
    callback_data = callback_query.get("data")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    username = callback_query.get("from", {}).get("username", "unknown")

    if chat_id:
        log_event_to_db(chat_id, username, "callback", callback_data, update, "received")

    if not chat_id:
        return True

    if callback_data == MODE_JSON_CALLBACK:
        answer_telegram_callback(callback_id, "JSON mode selected")
        session = create_invoice_session(
            chat_id,
            MODE_JSON,
            invoice=default_invoice_payload(),
            queue=[{"type": "field", "name": "json_payload", "prompt": "Send the invoice JSON data now."}],
            status=SESSION_STATUS_COLLECTING,
            state=BOT_STATE_COLLECTING,
        )
        update_bot_state(chat_id, BOT_STATE_COLLECTING)
        send_telegram_message(chat_id, "Send your invoice JSON. I will extract the fields and keep asking until everything is complete.")
        if not session:
            logger.warning("Failed to create JSON invoice session for chat %s", chat_id)
        return True

    if callback_data == MODE_GUIDED_CALLBACK:
        answer_telegram_callback(callback_id, "1-by-1 mode selected")
        session = create_invoice_session(
            chat_id,
            MODE_GUIDED,
            invoice=default_invoice_payload(),
            queue=build_guided_queue(),
            status=SESSION_STATUS_COLLECTING,
            state=BOT_STATE_COLLECTING,
        )
        update_bot_state(chat_id, BOT_STATE_COLLECTING)
        if session:
            send_next_queue_prompt(chat_id, session)
        else:
            logger.warning("Failed to create guided invoice session for chat %s", chat_id)
        return True

    if callback_data == CONFIRM_CALLBACK:
        answer_telegram_callback(callback_id, "Generating PDF")
        session = get_active_invoice_session(chat_id)
        if not session:
            send_telegram_message(chat_id, "No active invoice draft found. Send /invoice to start again.")
            return True

        invoice_data = get_session_invoice(session)
        if get_session_queue(session):
            send_telegram_message(chat_id, "Some required fields are still missing. Please continue answering the prompts.")
            prompt_active_queue_or_review(chat_id, session)
            return True

        send_telegram_message(chat_id, "Generating PDF, please wait...")
        try:
            result = generate_and_store_invoice(
                invoice_data=invoice_data,
                chat_id=chat_id,
                username=username,
                source="telegram",
                notify_telegram=True,
            )
            complete_session_generation(session, result)
            update_bot_state(chat_id, BOT_STATE_IDLE)
            logger.info("Generated invoice %s -> %s", result["invoice_no"], result["file_name"])
        except Exception as e:
            logger.error("Generation error: %s", e)
            send_telegram_message(chat_id, f"Error generating PDF: {str(e)}")
            update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    if callback_data == CANCEL_CALLBACK:
        answer_telegram_callback(callback_id, "Invoice draft archived")
        session = get_active_invoice_session(chat_id)
        if session:
            set_session_archived(session)
        update_bot_state(chat_id, BOT_STATE_IDLE)
        send_telegram_message(chat_id, "Invoice draft archived. Send /invoice to start a new one.")
        return True

    return False


def register_routes(app):
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

        return jsonify(
            {
                "ok": True,
                "supabase_configured": bool(supabase),
                "supabase_db_ok": db_ok,
                "supabase_db_error": db_error,
                "pdf_ready": True,
            }
        )

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

        return jsonify({"ok": True, **result})

    @app.route("/webhook", methods=["POST"])
    def webhook():
        update = request.get_json(silent=True) or {}
        if not update:
            return "OK", 200

        if update.get("callback_query"):
            if handle_callback_query(update):
                return "OK", 200

        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        username = message.get("from", {}).get("username", "unknown")
        text = (message.get("text", "") or "").strip()

        if not chat_id or not text:
            return "OK", 200

        log_event_to_db(chat_id, username, "message", text[:50], update, "received")

        current_state = BOT_STATE_IDLE
        active_session = get_active_invoice_session(chat_id)
        if active_session:
            current_state = active_session.get("state") or BOT_STATE_IDLE
        else:
            current_state = BOT_STATE_IDLE
        current_state = current_state if current_state else BOT_STATE_IDLE

        if text.startswith("/invoice"):
            if active_session:
                update_bot_state(chat_id, active_session.get("state") or BOT_STATE_COLLECTING)
                prompt_active_queue_or_review(chat_id, active_session)
                return "OK", 200

            update_bot_state(chat_id, BOT_STATE_WAITING_MODE)
            send_mode_selection(chat_id)
            if get_latest_completed_session(chat_id):
                send_telegram_message(chat_id, "I kept your last completed invoice session on file, so you can reuse or regenerate it later.")
            return "OK", 200

        if text.startswith("/queue"):
            if supabase:
                res = supabase.table("webhook_events").select("*").limit(10).order("received_at", desc=True).execute()
                events = res.data or []
                lines = ["Recent Queue Activity:", "", "<pre>", f"{'SUMMARY':50} {'STATUS':10} {'USERNAME':20}", "-" * 80]
                for idx, ev in enumerate(events, start=1):
                    summary = escape(str(ev.get("summary", "")))
                    status = escape(str(ev.get("status", "")))
                    event_username = escape(str(ev.get("username", "unknown")))
                    lines.append(f"{idx}. {summary[:50]:50} {status[:10]:10} {event_username[:20]:20}")
                lines.append("</pre>")
                send_telegram_message(chat_id, "\n".join(lines), "HTML")
            else:
                send_telegram_message(chat_id, "Database not configured.")
            return "OK", 200

        if text.startswith("/cancel"):
            if active_session:
                set_session_archived(active_session)
            update_bot_state(chat_id, BOT_STATE_IDLE)
            send_telegram_message(chat_id, "Invoice draft archived. Send /invoice to start a new one.")
            return "OK", 200

        if active_session:
            session_mode = active_session.get("mode")
            queue = get_session_queue(active_session)

            if active_session.get("state") == BOT_STATE_REVIEW:
                edit = extract_edit_command(text)
                if not edit:
                    send_telegram_message(chat_id, "To edit, send something like 'attn: New Name' or 'item 1 qty: 10'. Use the buttons to generate or cancel.")
                    return "OK", 200

                invoice = apply_edit_to_invoice(get_session_invoice(active_session), edit)
                set_session_invoice(active_session, invoice, queue=[], status=SESSION_STATUS_REVIEW, state=BOT_STATE_REVIEW)
                send_review_prompt(chat_id, active_session)
                return "OK", 200

            if session_mode == MODE_JSON and queue and queue[0].get("name") == "json_payload":
                process_json_invoice_input(chat_id, active_session, text)
                return "OK", 200

            if queue:
                handle_guided_answer(chat_id, active_session, text)
                return "OK", 200

        if current_state == BOT_STATE_WAITING_MODE:
            send_mode_selection(chat_id)

        return "OK", 200
