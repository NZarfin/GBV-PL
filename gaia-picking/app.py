import os
import imaplib
import email
import xml.etree.ElementTree as ET
import json
import hashlib
import threading
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
from urllib.parse import quote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
IMAP_HOST     = os.getenv("IMAP_HOST", "imap.strato.de")
IMAP_PORT     = int(os.getenv("IMAP_PORT", 993))
IMAP_USER     = os.getenv("IMAP_USER", "Pickinglists@gaiaherbs.nl")
IMAP_PASS     = os.getenv("IMAP_PASS", "Gaiabv1122!")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))   # seconds
BASE_URL       = os.getenv("BASE_URL", "http://localhost:5000")

# ── In-memory store (replace with SQLite later if needed) ────────────────────
orders = {}   # order_id -> order dict

# ── Pickers (add WhatsApp numbers when ready) ────────────────────────────────
PICKERS = [
    {"name": "Picker 1", "phone": ""},
    {"name": "Picker 2", "phone": ""},
    {"name": "Picker 3", "phone": ""},
]

# ── Managers (add WhatsApp numbers when ready) ───────────────────────────────
MANAGERS = [
    {"name": "Manager 1", "phone": ""},
    {"name": "Manager 2", "phone": ""},
]

# ── XML Parser ───────────────────────────────────────────────────────────────
def parse_picking_xml(xml_bytes):
    ns = {"cr": "urn:crystal-reports:schemas"}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.error(f"XML parse error: {e}")
        return None

    def find_field(node, obj_name):
        for obj in node.iter("{urn:crystal-reports:schemas}FormattedReportObject"):
            name_el = obj.find("{urn:crystal-reports:schemas}ObjectName")
            if name_el is not None and name_el.text == obj_name:
                fv = obj.find("{urn:crystal-reports:schemas}FormattedValue")
                return fv.text.strip() if fv is not None and fv.text else ""
        return ""

    def find_field_by_attr(node, field_name):
        for obj in node.iter("{urn:crystal-reports:schemas}FormattedReportObject"):
            fn = obj.get("FieldName", "")
            if field_name in fn:
                fv = obj.find("{urn:crystal-reports:schemas}FormattedValue")
                return fv.text.strip() if fv is not None and fv.text else ""
        return ""

    # Header
    order_num_raw = find_field_by_attr(root, "VKHORDNR")
    try:
        # Crystal Reports formats 47718 as "47.718" — remove the dot
        order_num = str(int(float(order_num_raw.replace(".", "").replace(",", ""))))
    except Exception:
        order_num = order_num_raw.replace(".", "").replace(",", "")

    sale_date      = find_field_by_attr(root, "VKHDAT")
    departure_date = find_field_by_attr(root, "VKHVDAT")
    handled_by     = find_field_by_attr(root, "VKHBHUSR")
    delivery_terms = find_field_by_attr(root, "LVCOMS")

    # Customer from Text7 — multiline address block, take first non-empty line
    customer_raw = find_field(root, "Text7")
    customer_lines = [l.strip() for l in customer_raw.split("\n") if l.strip()]
    customer = customer_lines[0] if customer_lines else customer_raw

    # Line items — collect all Details sections with ARTOMS
    lines = []
    seen = set()
    for details_area in root.iter("{urn:crystal-reports:schemas}FormattedArea"):
        if details_area.get("Type") != "Details":
            continue
        for section in details_area.iter("{urn:crystal-reports:schemas}FormattedSection"):
            product = ""
            size    = ""
            qty     = ""
            unit    = ""
            pallet  = ""
            country = ""
            grower  = ""
            lot     = ""

            for obj in section.findall("{urn:crystal-reports:schemas}FormattedReportObjects/{urn:crystal-reports:schemas}FormattedReportObject"):
                fn  = obj.get("FieldName", "")
                fv_el = obj.find("{urn:crystal-reports:schemas}FormattedValue")
                fv  = fv_el.text.strip() if fv_el is not None and fv_el.text else ""
                on_el = obj.find("{urn:crystal-reports:schemas}ObjectName")
                on  = on_el.text if on_el is not None else ""

                if "ART.ARTOMS"    in fn: product = fv
                if "AVMACODE"      in fn: size    = fv
                if "VKDAANBS"      in fn: qty     = fv
                if "EHDAFKRT"      in fn: unit    = fv
                if "xPallet"       in fn: pallet  = fv
                if "xLandLeveran"  in fn: country = fv
                if "xGrower"       in fn: grower  = fv
                if on == "Text20":        lot     = fv

            if product and qty:
                key = f"{product}|{qty}|{lot}"
                if key not in seen:
                    seen.add(key)
                    lines.append({
                        "product": product,
                        "size":    size,
                        "qty":     qty,
                        "unit":    unit,
                        "pallet":  pallet,
                        "country": country,
                        "grower":  grower,
                        "lot":     lot,
                        "picked":  False,
                    })

    if not order_num or not lines:
        logger.warning("Could not extract order number or lines from XML")
        return None

    return {
        "order_id":      order_num,
        "customer":      customer,
        "sale_date":     sale_date,
        "departure_date": departure_date,
        "handled_by":    handled_by,
        "delivery_terms": delivery_terms,
        "lines":         lines,
        "status":        "pending",      # pending | assigned | in_progress | completed
        "picker":        None,
        "assigned_at":   None,
        "completed_at":  None,
        "created_at":    datetime.now().isoformat(),
    }

# ── IMAP Poller ──────────────────────────────────────────────────────────────
def poll_inbox():
    while True:
        try:
            logger.info("Polling inbox...")
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(IMAP_USER, IMAP_PASS)
            mail.select("INBOX")

            _, msg_ids = mail.search(None, "UNSEEN")
            for mid in msg_ids[0].split():
                _, msg_data = mail.fetch(mid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                for part in msg.walk():
                    ct = part.get_content_type()
                    fn = part.get_filename() or ""
                    if ct in ("application/xml", "text/xml") or fn.lower().endswith(".xml"):
                        xml_bytes = part.get_payload(decode=True)
                        order = parse_picking_xml(xml_bytes)
                        if order:
                            oid = order["order_id"]
                            if oid not in orders:
                                orders[oid] = order
                                logger.info(f"New order loaded: #{oid} — {len(order['lines'])} lines")
                            else:
                                logger.info(f"Order #{oid} already exists, skipping")

            mail.logout()
        except Exception as e:
            logger.error(f"IMAP error: {e}")

        time.sleep(POLL_INTERVAL)

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    order_list = sorted(orders.values(), key=lambda o: o["created_at"], reverse=True)
    return render_template("dashboard.html", orders=order_list, pickers=PICKERS)


@app.route("/assign/<order_id>", methods=["POST"])
def assign(order_id):
    if order_id not in orders:
        return "Order not found", 404
    data = request.json
    picker_idx = int(data.get("picker_idx", 0))
    picker = PICKERS[picker_idx]

    orders[order_id]["picker"]      = picker
    orders[order_id]["status"]      = "assigned"
    orders[order_id]["assigned_at"] = datetime.now().isoformat()

    pick_url  = f"{BASE_URL}/pick/{order_id}"
    order     = orders[order_id]

    msg = (
        f"Hi {picker['name']}! 👋\n\n"
        f"You have a new picking list:\n"
        f"📦 Order #{order['order_id']}\n"
        f"🏢 {order['customer']}\n"
        f"📅 {order['departure_date']}\n"
        f"📋 {len(order['lines'])} items\n\n"
        f"Open your list here:\n{pick_url}"
    )

    wa_url = ""
    if picker.get("phone"):
        phone = picker["phone"].replace("+", "").replace(" ", "")
        wa_url = f"https://wa.me/{phone}?text={quote(msg)}"

    return jsonify({"wa_url": wa_url, "pick_url": pick_url, "msg": msg})


@app.route("/pick/<order_id>")
def pick(order_id):
    if order_id not in orders:
        return "Order not found", 404
    order = orders[order_id]
    if order["status"] == "assigned":
        orders[order_id]["status"] = "in_progress"
    managers = MANAGERS
    return render_template("pick.html", order=order, managers=managers, base_url=BASE_URL)


@app.route("/complete/<order_id>", methods=["POST"])
def complete(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404

    data    = request.json
    checked = data.get("checked", [])   # list of line indices that were picked
    manager_idx = int(data.get("manager_idx", 0))

    for i, line in enumerate(orders[order_id]["lines"]):
        line["picked"] = (i in checked)

    orders[order_id]["status"]       = "completed"
    orders[order_id]["completed_at"] = datetime.now().isoformat()

    order   = orders[order_id]
    manager = MANAGERS[manager_idx]
    picker  = order.get("picker", {}) or {}

    total   = len(order["lines"])
    picked  = sum(1 for l in order["lines"] if l["picked"])
    missing = total - picked

    lines_summary = ""
    for i, l in enumerate(order["lines"]):
        tick = "✅" if l["picked"] else "❌"
        lines_summary += f"{tick} {l['product']} x{l['qty']}\n"

    msg = (
        f"✅ Picking list completed!\n\n"
        f"📦 Order #{order['order_id']}\n"
        f"🏢 {order['customer']}\n"
        f"👤 Picker: {picker.get('name', 'Unknown')}\n"
        f"🕐 {datetime.now().strftime('%H:%M %d-%m-%Y')}\n\n"
        f"📋 {picked}/{total} items picked"
        + (f"\n⚠️ {missing} item(s) missing!" if missing > 0 else " — All good! 🎉")
        + f"\n\n{lines_summary}"
    )

    wa_url = ""
    if manager.get("phone"):
        phone = manager["phone"].replace("+", "").replace(" ", "")
        wa_url = f"https://wa.me/{phone}?text={quote(msg)}"

    return jsonify({"wa_url": wa_url, "msg": msg})


@app.route("/api/orders")
def api_orders():
    return jsonify(list(orders.values()))


@app.route("/test/load")
def test_load():
    """Load the sample XML for testing"""
    sample_path = os.path.join(os.path.dirname(__file__), "sample.xml")
    if os.path.exists(sample_path):
        with open(sample_path, "rb") as f:
            order = parse_picking_xml(f.read())
        if order:
            orders[order["order_id"]] = order
            return f"Loaded order #{order['order_id']} with {len(order['lines'])} lines"
    return "Sample XML not found", 404


if __name__ == "__main__":
    t = threading.Thread(target=poll_inbox, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
