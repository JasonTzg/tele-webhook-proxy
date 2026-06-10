from flask import jsonify, request
from html import escape

from services.common import (
    BOT_STATE_COLLECTING,
    BOT_STATE_IDLE,
    BOT_STATE_REVIEW,
    decrypt_encrypted_asset,
    BOT_STATE_WAITING_CANCEL,
    BOT_STATE_WAITING_DELETE_CONFIRM,
    BOT_STATE_WAITING_SESSION_ACTION,
    BOT_STATE_WAITING_SESSION_SELECTION,
    BOT_STATE_WAITING_MODE,
    CANCEL_CALLBACK,
    CANCEL_ARCHIVE_CALLBACK,
    CANCEL_DISCARD_CALLBACK,
    CONFIRM_CALLBACK,
    MODE_GUIDED,
    MODE_GUIDED_CALLBACK,
    MODE_JSON,
    MODE_JSON_CALLBACK,
    SESSION_STATUS_COLLECTING,
    SESSION_STATUS_COMPLETED,
    SESSION_STATUS_ARCHIVED,
    SESSION_STATUS_REVIEW,
    answer_telegram_callback,
    build_inline_keyboard,
    logger,
    log_event_to_db,
    now_iso,
    send_telegram_message,
    send_telegram_photo,
    supabase,
    get_bot_state_data,
    update_bot_state,
    get_bot_state,
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
    get_session_by_status_index,
    get_sessions_by_status,
    get_session_invoice,
    get_session_queue,
    delete_session,
    load_sample_invoice,
    clone_session_for_review,
    format_session_list_item,
    format_session_summary,
    parse_plain_text_invoice_payload,
    restore_archived_session_for_edit,
    persist_session,
    set_session_archived,
    set_session_completed,
    set_session_invoice,
)


def build_all_at_once_prompt():
    return (
        "Paste the completed template below, then send it back in one message:\n\n"
        "1. Attn:\n"
        "2. Tel:\n"
        "3. Invoice Date (Put . for Current Date):\n"
        "4. Billing Address:\n"
        "5. Delivery Address (Put . if same as 4.):\n"
        "6. Items (You can put multiple sets below. Just duplicate the next set of item below each item):\n"
        "Description:\n"
        "Material (Put . to leave it blank):\n"
        "Size (Put . to leave it blank):\n"
        "Remarks (Put . to leave it blank):\n"
        "Unit_price:\n"
        "Qty:\n"
    )


def send_reference_image(chat_id, asset_name, caption):
    image_bytes = decrypt_encrypted_asset(asset_name)
    if not image_bytes:
        return False

    send_telegram_photo(chat_id, image_bytes, asset_name.replace(".enc", ".png"), caption=caption)
    return True


def send_mode_selection(chat_id):
    reply_markup = build_inline_keyboard(
        [
            [
                {"text": "1) Send All at Once", "callback_data": MODE_JSON_CALLBACK},
                {"text": "2) Send data 1 by 1", "callback_data": MODE_GUIDED_CALLBACK},
            ]
        ]
    )
    send_telegram_message(chat_id, "Choose how you want to send invoice details: \n Preferred 1) for Desktop, 2) for Mobile.", reply_markup=reply_markup)


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


def send_cancel_prompt(chat_id):
    reply_markup = build_inline_keyboard(
        [
            [
                {"text": "Archive draft", "callback_data": CANCEL_ARCHIVE_CALLBACK},
                {"text": "Throw away draft", "callback_data": CANCEL_DISCARD_CALLBACK},
            ]
        ]
    )
    update_bot_state(chat_id, BOT_STATE_WAITING_CANCEL)
    send_telegram_message(chat_id, "What should I do with the current draft?", reply_markup=reply_markup)


def send_completed_list(chat_id):
    sessions = get_sessions_by_status(chat_id, SESSION_STATUS_COMPLETED, limit=10)
    if not sessions:
        send_telegram_message(chat_id, "No completed invoices found.")
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return

    update_bot_state(chat_id, BOT_STATE_WAITING_SESSION_SELECTION, {"kind": "completed"})
    lines = [
        "Latest completed invoices:",
        "<pre>",
        f"{'No.':>2}  {'Attn':10}  {'Tel':10}  {'Date':16}",
        "-" * 48
    ]
    for index, session in enumerate(sessions, start=1):
        lines.append(format_session_list_item(session, index))
    lines.append("</pre>")
    lines.append("Reply with a number from 1 to 10 to select an invoice, or send /cancel to cancel.")
    send_telegram_message(chat_id, "\n".join(lines), "HTML")


def send_archived_list(chat_id):
    sessions = get_sessions_by_status(chat_id, SESSION_STATUS_ARCHIVED, limit=10)
    if not sessions:
        send_telegram_message(chat_id, "No archived invoices found.")
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return

    update_bot_state(chat_id, BOT_STATE_WAITING_SESSION_SELECTION, {"kind": "archived"})
    lines = [
        "Latest archived invoices:",
        "<pre>",
        f"{'No.':>2}  {'Attn':10}  {'Tel':10}  {'Date':16}",
        "-" * 48
    ]
    for index, session in enumerate(sessions, start=1):
        lines.append(format_session_list_item(session, index))
    lines.append("</pre>")
    lines.append("Reply with a number from 1 to 10 to select an invoice, or send /cancel to cancel.")
    send_telegram_message(chat_id, "\n".join(lines), "HTML")


def send_completed_action_summary(chat_id, session):
    update_bot_state(chat_id, BOT_STATE_WAITING_SESSION_ACTION, {"kind": "completed", "session_id": session.get("id")})
    send_telegram_message(
        chat_id,
        format_session_summary(session) + "\n\n1. Re-generate PDF\n2. Modify and re-generate\n\nReply with 1 or 2, or /cancel.",
    )


def send_archived_action_summary(chat_id, session):
    update_bot_state(chat_id, BOT_STATE_WAITING_SESSION_ACTION, {"kind": "archived", "session_id": session.get("id")})
    send_telegram_message(
        chat_id,
        format_session_summary(session) + "\n\n1. Delete\n2. Continue to edit\n\nReply with 1 or 2, or /cancel.",
    )


def send_archived_delete_confirm(chat_id, session):
    update_bot_state(chat_id, BOT_STATE_WAITING_DELETE_CONFIRM, {"session_id": session.get("id")})
    send_telegram_message(
        chat_id,
        format_session_summary(session) + "\n\n1. Confirm delete\n2. Back\n\nReply with 1 or 2, or /cancel.",
    )


def handle_history_selection(chat_id, kind, text):
    if not text.isdigit():
        send_telegram_message(chat_id, "Reply with a number from 1 to 10.")
        return True

    index = int(text) - 1
    session = get_session_by_status_index(chat_id, SESSION_STATUS_COMPLETED if kind == "completed" else SESSION_STATUS_ARCHIVED, index)
    if not session:
        send_telegram_message(chat_id, "That selection is not available.")
        return True

    if kind == "completed":
        send_completed_action_summary(chat_id, session)
    else:
        send_archived_action_summary(chat_id, session)
    return True


def handle_history_action(chat_id, state_data, text):
    kind = state_data.get("kind")
    session_id = state_data.get("session_id")
    if not kind or not session_id:
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    if text not in {"1", "2"}:
        send_telegram_message(chat_id, "Reply with 1 or 2.")
        return True

    status = SESSION_STATUS_COMPLETED if kind == "completed" else SESSION_STATUS_ARCHIVED
    sessions = get_sessions_by_status(chat_id, status, limit=10)
    session = next((item for item in sessions if str(item.get("id")) == str(session_id)), None)
    if not session:
        send_telegram_message(chat_id, "That session is no longer available.")
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    if kind == "completed":
        if text == "1":
            send_telegram_message(chat_id, "Re-generating the PDF now...")
            try:
                result = generate_and_store_invoice(
                    invoice_data=get_session_invoice(session),
                    chat_id=chat_id,
                    username="telegram",
                    source="telegram",
                    notify_telegram=True,
                )
                complete_session_generation(session, result)
                update_bot_state(chat_id, BOT_STATE_IDLE)
            except Exception as e:
                logger.error("Completed regenerate error: %s", e)
                send_telegram_message(chat_id, f"Error generating PDF: {str(e)}")
                update_bot_state(chat_id, BOT_STATE_IDLE)
            return True

        cloned_session = clone_session_for_review(session, status=SESSION_STATUS_REVIEW, state=BOT_STATE_REVIEW)
        if not cloned_session:
            send_telegram_message(chat_id, "Unable to create a draft copy for editing.")
            update_bot_state(chat_id, BOT_STATE_IDLE)
            return True

        send_review_prompt(chat_id, cloned_session)
        return True

    if text == "1":
        send_archived_delete_confirm(chat_id, session)
        return True

    restored_session = restore_archived_session_for_edit(session)
    if not restored_session:
        send_telegram_message(chat_id, "Unable to restore that archived draft.")
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    queue = get_session_queue(restored_session)
    if queue:
        missing_lines = ["I found these missing fields:"]
        for index, item in enumerate(queue, start=1):
            missing_lines.append(f"{index}. {item.get('prompt', 'no data')}")
        send_telegram_message(chat_id, "\n".join(missing_lines))
        send_next_queue_prompt(chat_id, restored_session)
    else:
        send_telegram_message(chat_id, "No missing fields found. You can still edit the summary before generating.")
        send_review_prompt(chat_id, restored_session)
    return True


def handle_delete_confirm(chat_id, state_data, text):
    session_id = state_data.get("session_id")
    if not session_id:
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    sessions = get_sessions_by_status(chat_id, SESSION_STATUS_ARCHIVED, limit=10)
    session = next((item for item in sessions if str(item.get("id")) == str(session_id)), None)
    if not session:
        send_telegram_message(chat_id, "That archived invoice is no longer available.")
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    if text == "1":
        if delete_session(session):
            send_telegram_message(chat_id, "Archived invoice deleted.")
        else:
            send_telegram_message(chat_id, "Unable to delete that archived invoice.")
        update_bot_state(chat_id, BOT_STATE_IDLE)
        return True

    send_archived_action_summary(chat_id, session)
    return True


def send_next_queue_prompt(chat_id, session):
    queue = list((session.get("session_data") or {}).get("queue") or [])
    if not queue:
        send_review_prompt(chat_id, session)
        return

    current_prompt = queue[0]
    if current_prompt.get("name") == "attn":
        send_reference_image(chat_id, "client_company_section.enc", "Client company section reference.")
    elif current_prompt.get("name") == "items_count":
        send_reference_image(chat_id, "items_section.enc", "Items section reference.")

    send_telegram_message(chat_id, queue[0]["prompt"])


def expand_count_into_item_queue(session, item_count):
    invoice = get_session_invoice(session)
    if item_count <= 0: item_count = 1
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


def discard_active_session(chat_id):
    session = get_active_invoice_session(chat_id)
    if session:
        delete_session(session)
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
        if field_name in {"material", "size", "remarks"} and value == ".":
            value = ""
        invoice["items"][index][field_name] = value

    queue = queue[1:]
    set_session_invoice(session, invoice, queue=queue, status=SESSION_STATUS_COLLECTING, state=BOT_STATE_COLLECTING)
    if queue:
        send_next_queue_prompt(chat_id, session)
    else:
        send_review_prompt(chat_id, session)


def process_json_invoice_input(chat_id, session, text):
    invoice = parse_plain_text_invoice_payload(text)
    if not invoice.get("items"):
        send_telegram_message(
            chat_id,
            "I could not find any item blocks yet. Please paste the filled-in template again and include at least one item set.",
        )
        return
    set_session_invoice(session, invoice, queue=[], status=SESSION_STATUS_REVIEW, state=BOT_STATE_REVIEW)
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
        answer_telegram_callback(callback_id, "Send all at once selected")
        session = create_invoice_session(
            chat_id,
            MODE_JSON,
            invoice=default_invoice_payload(),
            queue=[{"type": "field", "name": "all_at_once_payload", "prompt": build_all_at_once_prompt()}],
            status=SESSION_STATUS_COLLECTING,
            state=BOT_STATE_COLLECTING,
        )
        update_bot_state(chat_id, BOT_STATE_COLLECTING)
        send_reference_image(chat_id, "client_company_section.enc", "Client company section reference.")
        send_reference_image(chat_id, "items_section.enc", "Items section reference.")
        send_telegram_message(chat_id, build_all_at_once_prompt())
        if not session:
            logger.warning("Failed to create all-at-once invoice session for chat %s", chat_id)
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
            send_reference_image(chat_id, "client_company_section.enc", "Client company section reference.")
            send_next_queue_prompt(chat_id, session)
        else:
            logger.warning("Failed to create guided invoice session for chat %s", chat_id)
        return True

    if callback_data == CONFIRM_CALLBACK:
        if get_bot_state(chat_id) == BOT_STATE_WAITING_CANCEL:
            answer_telegram_callback(callback_id, "Cancel choice pending")
            send_cancel_prompt(chat_id)
            return True

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
                invoice_session=session,
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
        answer_telegram_callback(callback_id, "Choose what to do with the draft")
        send_cancel_prompt(chat_id)
        return True

    if callback_data == CANCEL_ARCHIVE_CALLBACK:
        answer_telegram_callback(callback_id, "Invoice draft archived")
        archive_active_session(chat_id)
        send_telegram_message(chat_id, "Invoice draft archived. Send /invoice to start a new one.")
        return True

    if callback_data == CANCEL_DISCARD_CALLBACK:
        answer_telegram_callback(callback_id, "Invoice draft discarded")
        discard_active_session(chat_id)
        send_telegram_message(chat_id, "Invoice draft discarded. Send /invoice to start a new one.")
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

        bot_state = get_bot_state(chat_id)
        bot_state_data = get_bot_state_data(chat_id)

        if bot_state == BOT_STATE_WAITING_SESSION_SELECTION:
            if text.startswith("/cancel"):
                update_bot_state(chat_id, BOT_STATE_IDLE)
                send_telegram_message(chat_id, "Selection cancelled.")
                return "OK", 200
            kind = bot_state_data.get("kind")
            if kind in {"completed", "archived"}:
                handle_history_selection(chat_id, kind, text)
                return "OK", 200

        if bot_state == BOT_STATE_WAITING_SESSION_ACTION:
            if text.startswith("/cancel"):
                update_bot_state(chat_id, BOT_STATE_IDLE)
                send_telegram_message(chat_id, "Selection cancelled.")
                return "OK", 200
            handle_history_action(chat_id, bot_state_data, text)
            return "OK", 200

        if bot_state == BOT_STATE_WAITING_DELETE_CONFIRM:
            if text.startswith("/cancel"):
                update_bot_state(chat_id, BOT_STATE_IDLE)
                send_telegram_message(chat_id, "Delete confirmation cancelled.")
                return "OK", 200
            handle_delete_confirm(chat_id, bot_state_data, text)
            return "OK", 200

        current_state = BOT_STATE_IDLE
        active_session = get_active_invoice_session(chat_id)
        if active_session:
            current_state = active_session.get("state") or BOT_STATE_IDLE
        else:
            current_state = BOT_STATE_IDLE
        if get_bot_state(chat_id) == BOT_STATE_WAITING_CANCEL:
            current_state = BOT_STATE_WAITING_CANCEL
        current_state = current_state if current_state else BOT_STATE_IDLE

        if current_state == BOT_STATE_WAITING_CANCEL:
            if text.startswith("/cancel"):
                send_cancel_prompt(chat_id)
            else:
                send_telegram_message(chat_id, "Please choose Archive draft or Throw away draft to continue.")
            return "OK", 200

        if text.startswith("/completed"):
            send_completed_list(chat_id)
            return "OK", 200

        if text.startswith("/archived"):
            send_archived_list(chat_id)
            return "OK", 200

        if text.startswith("/invoice"):
            if active_session:
                update_bot_state(chat_id, active_session.get("state") or BOT_STATE_COLLECTING)
                prompt_active_queue_or_review(chat_id, active_session)
                return "OK", 200

            update_bot_state(chat_id, BOT_STATE_WAITING_MODE)
            send_mode_selection(chat_id)
            if get_latest_completed_session(chat_id):
                send_telegram_message(chat_id, "Completed invoice is in /completed. Re-generate or modify from there.")
            return "OK", 200

        if text.startswith("/activity"):
            if supabase:
                from datetime import datetime
                
                res = supabase.table("webhook_events").select("*").limit(10).order("received_at", desc=True).execute()
                events = res.data or []
                
                lines = ["Recent Queue Activity:", "", "<pre>"]
                lines.append(f"{'DATE':12}  {'ATTN':12}  {'COMMANDS/EVENTS':20}  {'STATUS':10}  {'BY':15}")
                lines.append("-" * 80)
                
                for ev in events:
                    # Parse date from received_at
                    received_at_str = ev.get("received_at", "")
                    try:
                        dt = datetime.fromisoformat(received_at_str.replace('Z', '+00:00'))
                        date_str = dt.strftime("%d %b %y")
                    except:
                        date_str = "???"
                    
                    # Parse summary to extract event info
                    summary = str(ev.get("summary", ""))
                    attn = ""
                    event_desc = ""
                    
                    if summary.startswith("Generated invoice-"):
                        event_desc = "Invoice Generated"
                        # Extract client name from summary
                        # Format: "Generated invoice-{client}-{date}.pdf"
                        temp = summary.replace("Generated invoice-", "").replace(".pdf", "")
                        parts = temp.split("-")
                        if parts and parts[0]:
                            client = parts[0]
                            if len(client) > 10:
                                attn = escape(client[:7]) + "..."
                            else:
                                attn = escape(client)
                    else:
                        event_desc = escape(summary)
                        attn = ""
                    
                    status = escape(str(ev.get("status", "")))
                    username = escape(str(ev.get("username", "unknown")))
                    
                    lines.append(f"{date_str:12}  {attn:12}  {event_desc:20}  {status:10}  {username:15}")
                
                lines.append("</pre>")
                send_telegram_message(chat_id, "\n".join(lines), "HTML")
            else:
                send_telegram_message(chat_id, "Database not configured.")
            return "OK", 200

        if text.startswith("/cancel"):
            if active_session:
                send_cancel_prompt(chat_id)
            else:
                update_bot_state(chat_id, BOT_STATE_IDLE)
                send_telegram_message(chat_id, "No active invoice draft found.")
            return "OK", 200

        if active_session:
            session_mode = active_session.get("mode")
            queue = get_session_queue(active_session)

            if active_session.get("state") == BOT_STATE_REVIEW:
                edit = extract_edit_command(text)
                if not edit:
                    send_telegram_message(chat_id, "To edit, send something like 'attn: New Name' or 'item 1 qty: 10'. Use the buttons to generate or choose cancel.")
                    return "OK", 200

                invoice = apply_edit_to_invoice(get_session_invoice(active_session), edit)
                set_session_invoice(active_session, invoice, queue=[], status=SESSION_STATUS_REVIEW, state=BOT_STATE_REVIEW)
                send_review_prompt(chat_id, active_session)
                return "OK", 200

            if session_mode == MODE_JSON and queue and queue[0].get("name") == "all_at_once_payload":
                process_json_invoice_input(chat_id, active_session, text)
                return "OK", 200

            if queue:
                handle_guided_answer(chat_id, active_session, text)
                return "OK", 200

        if current_state == BOT_STATE_WAITING_MODE:
            send_mode_selection(chat_id)

        return "OK", 200
