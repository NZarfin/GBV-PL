import os
import imaplib
import email
import xml.etree.ElementTree as ET
import threading
import time
import logging
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify
from urllib.parse import quote
import json
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
IMAP_HOST     = os.getenv("IMAP_HOST",     "imap.strato.de")
IMAP_PORT     = int(os.getenv("IMAP_PORT", 993))
IMAP_USER     = os.getenv("IMAP_USER",     "Pickinglists@gaiaherbs.nl")
IMAP_PASS     = os.getenv("IMAP_PASS",     "Gaiabv1122!")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
BASE_URL      = os.getenv("BASE_URL",      "http://localhost:64182")
DATABASE_URL  = os.getenv("DATABASE_URL",  "")

# ── In-memory store ───────────────────────────────────────────────────────────
orders = {}   # order_id -> order dict


# -- Database helpers
def _get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set - running without persistence")
        return
    try:
        conn = _get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id   TEXT PRIMARY KEY,
                data       JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database ready")
    except Exception as e:
        logger.error(f"DB init error: {e}")

def load_all_orders():
    if not DATABASE_URL:
        return
    try:
        conn = _get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT order_id, data FROM orders")
        for row in cur.fetchall():
            orders[row["order_id"]] = row["data"]
        cur.close()
        conn.close()
        logger.info(f"Loaded {len(orders)} order(s) from database")
    except Exception as e:
        logger.error(f"DB load error: {e}")

def save_order(order_id):
    if not DATABASE_URL or order_id not in orders:
        return
    try:
        conn = _get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO orders (order_id, data, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (order_id) DO UPDATE
                SET data = EXCLUDED.data, updated_at = NOW()
        """, (order_id, json.dumps(orders[order_id])))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"DB save error for {order_id}: {e}")

# ── People config — fill in phones when ready ─────────────────────────────────
PICKERS = [
    {"name": "Picker 1", "phone": ""},
    {"name": "Picker 2", "phone": ""},
    {"name": "Picker 3", "phone": ""},
]
MANAGERS = [
    {"name": "Manager 1", "phone": ""},
    {"name": "Manager 2", "phone": ""},
]

# ── XML Parser ────────────────────────────────────────────────────────────────
NS = "{urn:crystal-reports:schemas}"

def parse_picking_xml(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.error(f"XML parse error: {e}")
        return None

    def get_fv(obj):
        fv = obj.find(f"{NS}FormattedValue")
        return fv.text.strip() if fv is not None and fv.text else ""

    def get_tv(obj):
        tv = obj.find(f"{NS}TextValue")
        return tv.text.strip() if tv is not None and tv.text else ""

    def get_on(obj):
        on = obj.find(f"{NS}ObjectName")
        return on.text.strip() if on is not None and on.text else ""

    def find_by_field(node, field_name):
        for obj in node.iter(f"{NS}FormattedReportObject"):
            if field_name in obj.get("FieldName", ""):
                fv = obj.find(f"{NS}FormattedValue")
                return fv.text.strip() if fv is not None and fv.text else ""
        return ""

    def find_by_objname(node, obj_name):
        for obj in node.iter(f"{NS}FormattedReportObject"):
            on = obj.find(f"{NS}ObjectName")
            if on is not None and on.text and on.text.strip() == obj_name:
                tv = obj.find(f"{NS}TextValue")
                if tv is not None and tv.text:
                    return tv.text.strip()
                fv = obj.find(f"{NS}FormattedValue")
                return fv.text.strip() if fv is not None and fv.text else ""
        return ""

    # ── Header ────────────────────────────────────────────────────────────────
    order_num_raw = find_by_field(root, "VKHORDNR")
    try:
        order_num = str(int(float(order_num_raw.replace(".", "").replace(",", ""))))
    except Exception:
        order_num = order_num_raw.replace(".", "").replace(",", "")

    sale_date      = find_by_field(root, "VKHDAT")
    departure_date = find_by_field(root, "VKHVDAT")
    handled_by     = find_by_field(root, "VKHBHUSR")
    delivery_terms = find_by_field(root, "LVCOMS")
    carrier        = find_by_field(root, "RELNAAM1")

    customer_raw   = find_by_objname(root, "Text7")
    customer_lines = [l.strip() for l in customer_raw.split("\n") if l.strip()]
    customer       = customer_lines[0] if customer_lines else customer_raw

    # ── Lines ─────────────────────────────────────────────────────────────────
    lines = []
    seen  = set()

    for l2 in root.findall(f".//{NS}FormattedAreaPair[@Level='2']"):
        transit = ""
        for h in l2.findall(f"{NS}FormattedArea[@Type='Header']//{NS}FormattedReportObject"):
            if "Awz_Rol" in h.get("FieldName", ""):
                transit = get_fv(h)

        for l3 in l2.findall(f".//{NS}FormattedAreaPair[@Level='3']"):
            product_group = ""
            for h in l3.findall(f"{NS}FormattedArea[@Type='Header']//{NS}FormattedReportObject"):
                if "AGPOMS" in h.get("FieldName", ""):
                    product_group = get_fv(h)

            for det in l3.findall(f".//{NS}FormattedArea[@Type='Details']"):
                for sec in det.findall(f"{NS}FormattedSections/{NS}FormattedSection[@SectionNumber='0']"):
                    product = size = qty = unit = pallet = country = grower = lot = arr_date = ""
                    for obj in sec.findall(f"{NS}FormattedReportObjects/{NS}FormattedReportObject"):
                        fn = obj.get("FieldName", "")
                        on = get_on(obj)
                        fv = get_fv(obj)
                        tv = get_tv(obj)
                        if "ART.ARTOMS"        in fn: product  = fv
                        if "ARTVARMT.AVMACODE" in fn: size     = fv
                        if "VRKD.VKDAANBS"     in fn: qty      = fv
                        if "EENH_VRD.EHDAFKRT" in fn: unit     = fv
                        if "xPallet"           in fn: pallet   = fv
                        if "xLandLeverancier"  in fn: country  = fv
                        if "xGrower"           in fn: grower   = fv
                        if "INKH.IKHADAT"      in fn: arr_date = fv
                        if on == "Text20":             lot     = tv

                    if product and qty:
                        key = f"{product}|{qty}|{lot}"
                        if key not in seen:
                            seen.add(key)
                            lines.append({
                                "product":       product,
                                "size":          size,
                                "qty":           qty,
                                "unit":          unit,
                                "pallet":        pallet,
                                "country":       country,
                                "grower":        grower,
                                "lot":           lot,
                                "arr_date":      arr_date,
                                "transit":       transit,
                                "product_group": product_group,
                                "status":        "pending",
                                "short_qty":     "",
                                "note":          "",
                            })

    if not order_num or not lines:
        logger.warning("Could not extract order or lines")
        return None

    return {
        "order_id":        order_num,
        "customer":        customer,
        "sale_date":       sale_date,
        "departure_date":  departure_date,
        "handled_by":      handled_by,
        "delivery_terms":  delivery_terms,
        "carrier":         carrier,
        "lines":           lines,
        "status":          "pending",
        "picker":          None,
        "assigned_at":     None,
        "completed_at":    None,
        "created_at":      datetime.now().isoformat(),
        "has_update":      False,
        "update_diff":     "",
        "pending_lines":   [],   # new lines waiting for manager approval
    }

# ── Update diff detector ──────────────────────────────────────────────────────
def compute_diff(existing, incoming):
    """Compare two orders, return human-readable diff string and list of new lines."""
    ex_keys = {f"{l['product']}|{l['lot']}|{l['qty']}" for l in existing["lines"]}
    in_keys = {f"{l['product']}|{l['lot']}|{l['qty']}" for l in incoming["lines"]}

    added   = [l for l in incoming["lines"] if f"{l['product']}|{l['lot']}|{l['qty']}" not in ex_keys]
    removed = [l for l in existing["lines"] if f"{l['product']}|{l['lot']}|{l['qty']}" not in in_keys]

    parts = []
    if added:
        parts.append(f"➕ {len(added)} new line(s): " + ", ".join(l['product'] for l in added))
    if removed:
        parts.append(f"➖ {len(removed)} removed: " + ", ".join(l['product'] for l in removed))

    # Check header changes
    for field, label in [("customer","Customer"),("departure_date","Departure"),("delivery_terms","Terms")]:
        if existing.get(field) != incoming.get(field):
            parts.append(f"🔄 {label}: {existing.get(field)} → {incoming.get(field)}")

    diff_str = " | ".join(parts) if parts else "Minor update received"
    return diff_str, added

# ── IMAP Poller ───────────────────────────────────────────────────────────────
def poll_inbox():
    while True:
        try:
            logger.info("Polling inbox…")
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
                    if ct in ("application/xml","text/xml") or fn.lower().endswith(".xml"):
                        xml_bytes = part.get_payload(decode=True)
                        incoming  = parse_picking_xml(xml_bytes)
                        if not incoming:
                            continue
                        oid = incoming["order_id"]
                        if oid not in orders:
                            orders[oid] = incoming
                            logger.info(f"New order #{oid} — {len(incoming['lines'])} lines")
                        else:
                            # Order already exists — detect changes
                            diff_str, new_lines = compute_diff(orders[oid], incoming)
                            if new_lines or diff_str != "Minor update received":
                                orders[oid]["has_update"]    = True
                                orders[oid]["update_diff"]   = diff_str
                                orders[oid]["pending_lines"] = new_lines
                                logger.info(f"Update detected for #{oid}: {diff_str}")
            mail.logout()
        except Exception as e:
            logger.error(f"IMAP error: {e}")
        time.sleep(POLL_INTERVAL)

# ── Template helpers ──────────────────────────────────────────────────────────
def today_str():
    return date.today().strftime("%d-%m-%Y")

def tomorrow_str():
    return (date.today() + timedelta(days=1)).strftime("%d-%m-%Y")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    order_list = sorted(orders.values(), key=lambda o: (o.get("departure_date",""), o["created_at"]))
    return render_template("dashboard.html",
                           orders=order_list,
                           pickers=PICKERS,
                           today=today_str(),
                           tomorrow=tomorrow_str())


@app.route("/assign/<order_id>", methods=["POST"])
def assign(order_id):
    if order_id not in orders:
        return "Not found", 404
    data       = request.json
    picker_idx = int(data.get("picker_idx", 0))
    picker     = PICKERS[picker_idx]

    orders[order_id].update({
        "picker":      picker,
        "status":      "assigned",
        "assigned_at": datetime.now().isoformat(),
    })
    save_order(order_id)

    pick_url = f"{BASE_URL}/pick/{order_id}"
    order    = orders[order_id]
    msg = (
        f"Hi {picker['name']}! 👋\n\n"
        f"New picking list ready:\n"
        f"📦 Order #{order['order_id']}\n"
        f"🏢 {order['customer']}\n"
        f"📅 Departure: {order['departure_date']}\n"
        f"📋 {len(order['lines'])} items\n\n"
        f"Open your list 👇\n{pick_url}"
    )
    wa_url = ""
    if picker.get("phone"):
        phone  = picker["phone"].replace("+","").replace(" ","")
        wa_url = f"https://wa.me/{phone}?text={quote(msg)}"
    return jsonify({"wa_url": wa_url, "pick_url": pick_url, "msg": msg})


@app.route("/pick/<order_id>")
def pick(order_id):
    if order_id not in orders:
        return "Order not found", 404
    order = orders[order_id]
    if order["status"] in ("assigned", "pending"):
        orders[order_id]["status"] = "in_progress"
        save_order(order_id)
    return render_template("pick.html", order=order, managers=MANAGERS, base_url=BASE_URL)


@app.route("/update_line/<order_id>/<int:line_idx>", methods=["POST"])
def update_line(order_id, line_idx):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    data = request.json
    line = orders[order_id]["lines"][line_idx]
    line["status"]    = data.get("status",    line["status"])
    line["short_qty"] = data.get("short_qty", line["short_qty"])
    line["note"]      = data.get("note",      line["note"])
    save_order(order_id)
    return jsonify({"ok": True})


@app.route("/complete/<order_id>", methods=["POST"])
def complete(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    data        = request.json
    manager_idx = int(data.get("manager_idx", 0))
    order       = orders[order_id]
    lines       = order["lines"]
    total       = len(lines)
    picked      = sum(1 for l in lines if l["status"] == "picked")
    short       = sum(1 for l in lines if l["status"] == "short")
    missing     = sum(1 for l in lines if l["status"] == "missing")

    final_status = "completed" if (short == 0 and missing == 0) else "partial"
    orders[order_id]["status"]       = final_status
    orders[order_id]["completed_at"] = datetime.now().isoformat()
    save_order(order_id)

    manager = MANAGERS[manager_idx]
    picker  = order.get("picker") or {}

    lines_summary = ""
    for l in lines:
        if   l["status"] == "picked":  icon = "✅"; detail = f"x{l['qty']} {l['unit']}"
        elif l["status"] == "short":   icon = "⚠️"; detail = f"x{l['short_qty']}/{l['qty']} SHORT"
        elif l["status"] == "missing": icon = "❌"; detail = f"x{l['qty']} MISSING"
        else:                          icon = "⬜"; detail = f"x{l['qty']}"
        note_str = f" 📝 {l['note']}" if l["note"] else ""
        lot_str  = f" [lot {l['lot']}]" if l["lot"] else ""
        lines_summary += f"{icon} {l['product']}{lot_str} — {detail}{note_str}\n"

    msg = (
        f"{'✅' if final_status=='completed' else '⚠️'} Order #{order['order_id']} — "
        f"{'All picked!' if final_status=='completed' else 'Completed with exceptions'}\n\n"
        f"🏢 {order['customer']}\n"
        f"👤 {picker.get('name','Unknown')}\n"
        f"🕐 {datetime.now().strftime('%H:%M %d-%m-%Y')}\n\n"
        f"📊 {picked}/{total} picked"
        + (f" | ⚠️ {short} short" if short else "")
        + (f" | ❌ {missing} missing" if missing else "")
        + f"\n\n{lines_summary}"
    )

    wa_url = ""
    if manager.get("phone"):
        phone  = manager["phone"].replace("+","").replace(" ","")
        wa_url = f"https://wa.me/{phone}?text={quote(msg)}"
    return jsonify({"wa_url": wa_url, "msg": msg, "status": final_status})


@app.route("/reopen/<order_id>", methods=["POST"])
def reopen(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    orders[order_id]["status"]       = "in_progress"
    orders[order_id]["completed_at"] = None
    save_order(order_id)
    return jsonify({"ok": True})


@app.route("/accept_update/<order_id>", methods=["POST"])
def accept_update(order_id):
    """Manager accepts the update — new lines are added to the order"""
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    order        = orders[order_id]
    new_lines    = order.get("pending_lines", [])
    if new_lines:
        orders[order_id]["lines"].extend(new_lines)
    orders[order_id]["has_update"]    = False
    orders[order_id]["update_diff"]   = ""
    orders[order_id]["pending_lines"] = []
    # If completed, reopen so picker can pick the new items
    if orders[order_id]["status"] in ("completed", "partial"):
        orders[order_id]["status"] = "in_progress"
    save_order(order_id)
    return jsonify({"ok": True})


@app.route("/dismiss_update/<order_id>", methods=["POST"])
def dismiss_update(order_id):
    """Manager dismisses the update — keep original"""
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    orders[order_id]["has_update"]    = False
    orders[order_id]["update_diff"]   = ""
    orders[order_id]["pending_lines"] = []
    save_order(order_id)
    return jsonify({"ok": True})


@app.route("/api/orders")
def api_orders():
    return jsonify(list(orders.values()))


@app.route("/test/load")
def test_load():
    sample = os.path.join(os.path.dirname(__file__), "sample.xml")
    if os.path.exists(sample):
        with open(sample, "rb") as f:
            order = parse_picking_xml(f.read())
        if order:
            orders[order["order_id"]] = order
            save_order(order["order_id"])
            return (f"✅ Loaded #{order['order_id']} — {len(order['lines'])} lines<br><br>"
                    + "<br>".join(
                        f"• {l['product']} | qty:{l['qty']} {l['unit']} | "
                        f"lot:<b>{l['lot'] or '—'}</b> | grower:{l['grower'] or '—'} | "
                        f"country:{l['country']} | transit:{l['transit']} | group:{l['product_group']}"
                        for l in order["lines"]
                    ))
    return "sample.xml not found", 404


init_db()
load_all_orders()

if __name__ == "__main__":
    t = threading.Thread(target=poll_inbox, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=64182, debug=True)
