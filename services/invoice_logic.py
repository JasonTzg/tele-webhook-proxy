import copy
import json
import os
import re
from datetime import datetime
from html import escape
from pathlib import Path

from services.common import (
    BOT_STATE_COLLECTING,
    BOT_STATE_IDLE,
    BOT_STATE_REVIEW,
    MODE_GUIDED,
    GUIDED_EDITABLE_FIELDS,
    ITEM_EDITABLE_FIELDS,
    ITEM_PROMPTS,
    SESSION_STATUS_ARCHIVED,
    SESSION_STATUS_COLLECTING,
    SESSION_STATUS_COMPLETED,
    SESSION_STATUS_DRAFT,
    SESSION_STATUS_REVIEW,
    TOP_LEVEL_PROMPTS,
    logger,
    log_event_to_db,
    now_iso,
    send_telegram_document,
    send_telegram_message,
    supabase,
)
from services.pdf_service import generate_invoice_pdf


def default_invoice_payload():
    return {"items": []}


def ensure_invoice_shape(invoice_data):
    invoice = dict(invoice_data or {})
    if not isinstance(invoice.get("items"), list):
        invoice["items"] = []
    return invoice


def normalize_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def safe_text_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def key_matches(key, aliases):
    normalized_key = normalize_key(key)
    for alias in aliases:
        normalized_alias = normalize_key(alias)
        if normalized_key == normalized_alias or normalized_alias in normalized_key:
            return True
    return False


def find_matching_value(data, aliases):
    if isinstance(data, dict):
        for key, value in data.items():
            if key_matches(key, aliases):
                return value
        for value in data.values():
            nested = find_matching_value(value, aliases)
            if nested is not None and not is_blank(nested):
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = find_matching_value(item, aliases)
            if nested is not None and not is_blank(nested):
                return nested
    return None


def extract_address_fields(raw_data):
    billing_aliases = ["billing address", "billing", "billings"]
    delivery_aliases = ["delivery address", "delivery", "delivers"]
    generic_aliases = ["address", "addresses"]

    billing_address = find_matching_value(raw_data, billing_aliases)
    delivery_address = find_matching_value(raw_data, delivery_aliases)

    if is_blank(billing_address) and is_blank(delivery_address):
        generic_address = find_matching_value(raw_data, generic_aliases)
        if not is_blank(generic_address):
            billing_address = generic_address
            delivery_address = generic_address

    return billing_address, delivery_address


def normalize_invoice_date(value):
    if is_blank(value) or str(value).strip() == ".":
        return datetime.utcnow().strftime("%d/%b/%Y")
    return safe_text_value(value)


def normalize_item(raw_item):
    aliases = {
        "description": ["description", "descriptions", "product name", "product", "name"],
        "material": ["material", "materials", "type", "types"],
        "size": ["size", "sizes", "sizing", "sizings", "dimension", "dimensions"],
        "remarks": ["remark", "remarks", "note", "notes"],
        "qty": ["qty", "quantity"],
        "unit_price": ["unit price", "unit_price", "price", "unitprice"],
    }
    normalized = {}
    for target_field, field_aliases in aliases.items():
        value = None
        if isinstance(raw_item, dict):
            for key, candidate in raw_item.items():
                if key_matches(key, field_aliases):
                    value = candidate
                    break
        normalized[target_field] = safe_text_value(value)
    if normalized.get("material") == "." or is_blank(normalized.get("material")):
        normalized["material"] = ""
    if normalized.get("size") == "." or is_blank(normalized.get("size")):
        normalized["size"] = ""
    if normalized.get("remarks") == "." or is_blank(normalized.get("remarks")):
        normalized["remarks"] = ""
    return normalized


def normalize_items(raw_data):
    items_value = None
    if isinstance(raw_data, dict):
        for key, value in raw_data.items():
            if key_matches(key, ["items", "item", "products", "product", "line items"]):
                items_value = value
                break
    if not isinstance(items_value, list):
        return []
    return [normalize_item(item) for item in items_value if isinstance(item, dict)]


def normalize_invoice_payload(raw_data):
    raw_data = raw_data or {}
    invoice = default_invoice_payload()
    invoice["attn"] = safe_text_value(find_matching_value(raw_data, ["attn", "attention", "client name", "client", "name"]))
    invoice["tel"] = safe_text_value(find_matching_value(raw_data, ["tel", "number", "client number", "customer number", "telephone", "phone"]))
    invoice["invoice_date"] = normalize_invoice_date(find_matching_value(raw_data, ["invoice date", "date"]))
    billing_address, delivery_address = extract_address_fields(raw_data)
    invoice["billing_address"] = safe_text_value(billing_address)
    delivery_text = safe_text_value(delivery_address)
    if delivery_text == "." and not is_blank(invoice["billing_address"]):
        delivery_text = invoice["billing_address"]
    invoice["delivery_address"] = delivery_text
    invoice["items"] = normalize_items(raw_data)
    return invoice


def normalize_label(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))


def label_matches(label, aliases):
    normalized_label = normalize_label(label)
    for alias in aliases:
        normalized_alias = normalize_label(alias)
        if normalized_label == normalized_alias or normalized_alias in normalized_label:
            return True
    return False


def parse_plain_text_invoice_payload(text):
    top_level_aliases = {
        "attn": ["attn", "attention", "client name", "client", "name"],
        "tel": ["tel", "number", "client number", "customer number", "telephone", "phone"],
        "invoice_date": ["invoice date", "date"],
        "billing_address": ["billing address", "billing"],
        "delivery_address": ["delivery address", "delivery"],
    }
    item_aliases = {
        "description": ["description", "product name", "product", "name"],
        "material": ["material", "materials", "type", "types"],
        "size": ["size", "sizes", "sizing", "sizings", "dimension", "dimensions"],
        "remarks": ["remark", "remarks", "note", "notes"],
        "unit_price": ["unit price", "unit_price", "price", "unitprice"],
        "qty": ["qty", "quantity"],
    }

    invoice = default_invoice_payload()
    raw_top_level = {}
    items = []
    current_section = None
    current_field = None
    current_lines = []
    current_item = {}

    def commit_current_field():
        nonlocal current_field, current_lines, current_section, current_item
        if not current_field:
            return

        value = "\n".join(current_lines).strip()
        if current_section == "top":
            raw_top_level[current_field] = value
        elif current_section == "item":
            if current_field == "description" and current_item and any(not is_blank(existing) for existing in current_item.values()):
                items.append(current_item)
                current_item = {}
            current_item[current_field] = value

        current_field = None
        current_lines = []

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            if current_field:
                current_lines.append("")
            continue

        match = re.match(r"^(?:\d+\.\s*)?([A-Za-z_ ]+?)(?:\s*\(.*\))?\s*:\s*(.*)$", stripped)
        if match:
            commit_current_field()
            label = match.group(1)
            value = match.group(2).strip()

            matched_top_level = next((field_name for field_name, aliases in top_level_aliases.items() if label_matches(label, aliases)), None)
            matched_item_field = next((field_name for field_name, aliases in item_aliases.items() if label_matches(label, aliases)), None)

            if matched_top_level:
                current_section = "top"
                current_field = matched_top_level
                current_lines = [value] if value else []
                continue

            if matched_item_field:
                current_section = "item"
                current_field = matched_item_field
                current_lines = [value] if value else []
                continue

        if current_field:
            current_lines.append(line)

    commit_current_field()

    if current_item and any(not is_blank(existing) for existing in current_item.values()):
        items.append(current_item)

    raw_top_level["items"] = items
    return normalize_invoice_payload(raw_top_level)


def build_guided_queue():
    return [{"type": "field", "name": key, "prompt": prompt} for key, prompt in TOP_LEVEL_PROMPTS]


def build_guided_item_queue(item_count):
    queue = []
    for index in range(item_count):
        for field_name, prompt in ITEM_PROMPTS:
            queue.append({"type": "item", "index": index, "name": field_name, "prompt": prompt})
    return queue


def build_json_followup_queue(invoice):
    queue = []
    for key, prompt in TOP_LEVEL_PROMPTS[:-1]:
        if is_blank(invoice.get(key)):
            queue.append({"type": "field", "name": key, "prompt": prompt})

    items = invoice.get("items") or []
    if not items:
        queue.append({"type": "field", "name": "items_count", "prompt": "How many items/products?"})
        return queue

    for index, item in enumerate(items):
        for field_name, prompt in ITEM_PROMPTS:
            if field_name in {"material", "size", "remarks"}:
                # Optional item fields are never required follow-ups.
                continue
            if is_blank(item.get(field_name)):
                queue.append({"type": "item", "index": index, "name": field_name, "prompt": prompt})

    return queue


def get_active_invoice_session(chat_id):
    if not supabase:
        return None
    res = (
        supabase.table("invoice_sessions")
        .select("*")
        .eq("chat_id", chat_id)
        .order("updated_at", desc=True)
        .limit(10)
        .execute()
    )
    for row in res.data or []:
        if row.get("status") in {SESSION_STATUS_DRAFT, SESSION_STATUS_COLLECTING, SESSION_STATUS_REVIEW}:
            row["session_data"] = row.get("session_data") or {}
            return row
    return None


def get_latest_completed_session(chat_id):
    if not supabase:
        return None
    res = (
        supabase.table("invoice_sessions")
        .select("*")
        .eq("chat_id", chat_id)
        .order("updated_at", desc=True)
        .limit(10)
        .execute()
    )
    for row in res.data or []:
        if row.get("status") == SESSION_STATUS_COMPLETED:
            row["session_data"] = row.get("session_data") or {}
            return row
    return None


def get_sessions_by_status(chat_id, status, limit=10):
    if not supabase:
        return []

    order_field = "updated_at"
    if status == SESSION_STATUS_COMPLETED:
        order_field = "completed_at"
    elif status == SESSION_STATUS_ARCHIVED:
        order_field = "archived_at"

    res = (
        supabase.table("invoice_sessions")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("status", status)
        .order(order_field, desc=True)
        .limit(limit)
        .execute()
    )

    sessions = res.data or []
    for row in sessions:
        row["session_data"] = row.get("session_data") or {}
    return sessions


def get_session_mode(session):
    session_data = session.get("session_data") or {}
    return session_data.get("mode") or session.get("mode") or MODE_GUIDED


def get_session_invoice_value(session, field_name, default_value="no data"):
    invoice = get_session_invoice(session)
    value = invoice.get(field_name)
    if is_blank(value):
        return default_value
    return safe_text_value(value)


def format_session_timestamp(value):
    if is_blank(value):
        return "no data"
    text_value = str(value)
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text_value


def format_session_list_timestamp(value):
    if is_blank(value):
        return "no data"
    text_value = str(value)
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        return parsed.strftime("%d %b %Y %H:%M")
    except Exception:
        return text_value


def truncate_with_ellipsis(value, max_length):
    text_value = safe_text_value(value)
    if len(text_value) <= max_length:
        return text_value
    if max_length <= 3:
        return text_value[:max_length]
    return text_value[: max_length - 3].rstrip() + "..."


def format_session_list_item(session, index):
    invoice = get_session_invoice(session)
    attn = truncate_with_ellipsis(invoice.get("attn") or "no data", 10)
    tel = safe_text_value(invoice.get("tel")) or "no data"
    timestamp = format_session_list_timestamp(
        session.get("completed_at") or session.get("archived_at") or session.get("updated_at")
    )
    return f"{index:>2}  {escape(attn):<10}  {escape(tel):<10}  {escape(timestamp)}"


def format_session_summary(session):
    invoice = get_session_invoice(session)
    lines = [build_review_summary(invoice), ""]
    if session.get("status") == SESSION_STATUS_COMPLETED:
        lines.append(f"<b>Completed at: {escape(format_session_timestamp(session.get('completed_at')))}</b>")
        lines.append(f"<b>Generated invoice id: {escape(str(session.get('generated_invoice_id') or 'no data'))}</b>")
    elif session.get("status") == SESSION_STATUS_ARCHIVED:
        lines.append(f"<b>Archived at: {escape(format_session_timestamp(session.get('archived_at')))}</b>")
    # lines.append(f"Session id: {escape(str(session.get('id') or 'no data'))}") # No use for user. Only useful in tracing through DB
    return "\n".join(lines)


def get_session_by_status_index(chat_id, status, index):
    sessions = get_sessions_by_status(chat_id, status, limit=10)
    if index < 0 or index >= len(sessions):
        return None
    return sessions[index]


def clone_session_for_review(source_session, status=SESSION_STATUS_REVIEW, state=BOT_STATE_REVIEW):
    if not supabase or not source_session:
        return None

    invoice = copy.deepcopy(get_session_invoice(source_session))
    return create_invoice_session(
        source_session["chat_id"],
        get_session_mode(source_session),
        invoice=invoice,
        queue=[],
        status=status,
        state=state,
    )


def restore_archived_session_for_edit(session):
    if not supabase or not session:
        return None

    invoice = copy.deepcopy(get_session_invoice(session))
    queue = build_json_followup_queue(invoice)
    session_data = dict(session.get("session_data") or {})
    session_data["mode"] = get_session_mode(session)
    session_data["invoice"] = ensure_invoice_shape(invoice)
    session_data["queue"] = queue
    return persist_session(
        session,
        session_data=session_data,
        status=SESSION_STATUS_COLLECTING,
        state=BOT_STATE_COLLECTING,
        archived_at=None,
    )


def create_invoice_session(chat_id, mode, invoice=None, queue=None, status=SESSION_STATUS_COLLECTING, state=BOT_STATE_COLLECTING):
    if not supabase:
        return None

    session_data = {
        "mode": mode,
        "invoice": ensure_invoice_shape(invoice or default_invoice_payload()),
        "queue": queue or [],
    }
    response = supabase.table("invoice_sessions").insert(
        {
            "chat_id": chat_id,
            "mode": mode,
            "state": state,
            "status": status,
            "session_data": session_data,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    ).execute()
    if response.data:
        row = response.data[0]
        row["session_data"] = row.get("session_data") or session_data
        return row
    return None


def persist_session(session, **updates):
    if not supabase or not session:
        return session

    payload = dict(updates)
    payload["updated_at"] = now_iso()
    try:
        supabase.table("invoice_sessions").update(payload).eq("id", session["id"]).execute()
    except Exception as e:
        logger.error("Session update error: %s", e)
        return session

    if "session_data" in payload:
        session["session_data"] = payload["session_data"]
    session.update(payload)
    return session


def get_session_invoice(session):
    session_data = session.get("session_data") or {}
    return ensure_invoice_shape(session_data.get("invoice") or default_invoice_payload())


def set_session_invoice(session, invoice, queue=None, status=None, state=None):
    session_data = dict(session.get("session_data") or {})
    session_data["invoice"] = ensure_invoice_shape(invoice)
    if queue is not None:
        session_data["queue"] = queue
    updates = {"session_data": session_data}
    if status is not None:
        updates["status"] = status
    if state is not None:
        updates["state"] = state
    return persist_session(session, **updates)


def set_session_completed(session, generated_invoice_id=None):
    session_data = dict(session.get("session_data") or {})
    session_data["generated_invoice_id"] = generated_invoice_id
    session_data["generated_at"] = now_iso()
    return persist_session(
        session,
        session_data=session_data,
        status=SESSION_STATUS_COMPLETED,
        state=BOT_STATE_IDLE,
        completed_at=now_iso(),
        archived_at=None,
        generated_invoice_id=generated_invoice_id,
    )


def set_session_archived(session):
    session_data = dict(session.get("session_data") or {})
    session_data["archived_at"] = now_iso()
    return persist_session(
        session,
        session_data=session_data,
        status=SESSION_STATUS_ARCHIVED,
        state=BOT_STATE_IDLE,
        archived_at=now_iso(),
    )


def delete_session(session):
    if not supabase or not session:
        return False

    try:
        supabase.table("invoice_sessions").delete().eq("id", session["id"]).execute()
        return True
    except Exception as e:
        logger.error("Session delete error: %s", e)
        return False


def extract_edit_command(text):
    if not text:
        return None

    item_match = re.match(r"^item\s*(\d+)\s+([a-z_ ]+)[:=]\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
    if item_match:
        return {
            "scope": "item",
            "index": int(item_match.group(1)) - 1,
            "field": normalize_key(item_match.group(2)).replace(" ", "_"),
            "value": item_match.group(3).strip(),
        }

    field_match = re.match(r"^([a-z_ ]+)[:=]\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
    if field_match:
        return {
            "scope": "invoice",
            "field": normalize_key(field_match.group(1)).replace(" ", "_"),
            "value": field_match.group(2).strip(),
        }

    return None


def apply_edit_to_invoice(invoice, edit):
    invoice = ensure_invoice_shape(invoice)
    if edit["scope"] == "invoice":
        field = edit["field"]
        value = edit["value"]
        if field in GUIDED_EDITABLE_FIELDS:
            if field == "invoice_date" and value == ".":
                value = datetime.utcnow().strftime("%Y-%m-%d")
            if field == "delivery_address" and value == ".":
                value = invoice.get("billing_address", "")
            invoice[field] = value
    elif edit["scope"] == "item":
        index = edit["index"]
        field = edit["field"]
        value = edit["value"]
        if index >= 0:
            while len(invoice["items"]) <= index:
                invoice["items"].append({})
            if field in ITEM_EDITABLE_FIELDS:
                if field in {"material", "size", "remarks"} and value == ".":
                    value = ""
                invoice["items"][index][field] = value
    return invoice


def get_session_queue(session):
    session_data = session.get("session_data") or {}
    queue = session_data.get("queue") or []
    return queue if isinstance(queue, list) else []


def load_sample_invoice():
    sample_path = Path(__file__).resolve().parent.parent / "assets" / "sample_invoice.json"
    with sample_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def reserve_invoice_record(chat_id, attention):
    if not supabase:
        return None
    response = supabase.table("generated_invoices").insert(
        {
            "chat_id": chat_id,
            "attention": attention,
            "status": "generating",
        }
    ).execute()
    return response.data[0] if response.data else None


def finalize_invoice_record(record_id, file_name, public_url):
    if not supabase or not record_id:
        return
    supabase.table("generated_invoices").update(
        {
            "file_path": file_name,
            "public_url": public_url,
            "status": "completed",
        }
    ).eq("id", record_id).execute()


def generate_and_store_invoice(invoice_data, chat_id=None, username=None, source="local", notify_telegram=False):
    invoice_data = ensure_invoice_shape(invoice_data)
    attention = invoice_data.get("attn", "unknown")
    record = reserve_invoice_record(chat_id, attention)
    invoice_id = str(record["id"]) if record and record.get("id") is not None else "00000"

    invoice_data = dict(invoice_data)
    invoice_data["invoice_no"] = invoice_id

    attn_slug = "".join(ch for ch in str(attention) if ch.isalnum())
    timestamp = datetime.now().strftime("%d%m%y-%H%M")
    file_name = f"invoice-{attn_slug}-{timestamp}.pdf"
    output_path = os.path.join("/tmp", file_name) if os.name != "nt" else file_name

    generate_invoice_pdf(invoice_data, output_path)

    if not os.path.exists(output_path):
        raise FileNotFoundError(f"PDF generation completed but file not found: {output_path}")
    logger.info("Generated PDF saved to %s", output_path)

    public_url = ""
    if supabase:
        try:
            with open(output_path, "rb") as f:
                supabase.storage.from_("invoices").upload(file_name, f)
                logger.info("Uploaded %s to Supabase Storage.", file_name)
            public_url = supabase.storage.from_("invoices").get_public_url(file_name)
            finalize_invoice_record(record["id"] if record else None, file_name, public_url)
        except Exception as e:
            logger.error("Supabase Storage Upload Error: %s", e)

    log_event_to_db(
        chat_id,
        username or source,
        f"{source}_invoice_generated",
        f"Generated {file_name}",
        invoice_data,
        "success",
    )

    if notify_telegram and chat_id:
        send_telegram_document(chat_id, output_path)
        if public_url:
            ALL_STORED_PDF = os.getenv("ALL_STORED_PDF") or "Missing Env Variable"
            send_telegram_message(chat_id, f"All Stored link: {ALL_STORED_PDF}")

    return {
        "invoice_no": invoice_id,
        "file_name": file_name,
        "output_path": output_path,
        "public_url": public_url,
        "record_id": record["id"] if record else None,
    }


def build_review_summary(invoice):
    invoice = ensure_invoice_shape(invoice)

    def format_cell(value, width):
        text = safe_text_value(value)
        if len(text) > width:
            text = text[: max(0, width - 3)] + "..."
        return escape(text)

    lines = [
        "Invoice draft ready. Review the details below:",
        "",
        f"Attn: {escape(str(invoice.get('attn', '')))}",
        f"Tel: {escape(str(invoice.get('tel', '')))}",
        f"Invoice date: {escape(str(invoice.get('invoice_date', '')))}",
        f"Billing address: {escape(str(invoice.get('billing_address', '')))}",
        f"Delivery address: {escape(str(invoice.get('delivery_address', '')))}",
        "",
        "Items:",
        "<pre>",
        f"{'#':>2}  {'DESCRIPTION':<24}  {'MATERIAL':<24}  {'SIZE':<22}  {'QTY':>5}  {'PRICE':<12}  {'REMARKS':<20}",
        "-" * 125,
    ]
    for idx, item in enumerate(invoice.get("items", []), start=1):
        lines.append(
            f"{idx:>2}  {format_cell(item.get('description', ''), 24):<24}  {format_cell(item.get('material', ''), 24):<24}  {format_cell(item.get('size', ''), 22):<22}  {format_cell(item.get('qty', ''), 5):>5}  {format_cell(item.get('unit_price', ''), 12):<12}  {format_cell(item.get('remarks', ''), 20):<20}"
        )
    lines.append("</pre>")
    return "\n".join(lines)
