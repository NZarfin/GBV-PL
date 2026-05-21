import os
import re
import logging
from collections import OrderedDict
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

import config
from database import (
    init_db, load_orders, save_order,
    get_pickers, get_managers, get_all_users, get_user_by_id,
    create_picker, create_manager, update_user, delete_user,
    verify_manager, verify_picker,
)
from xml_parser import parse_picking_xml, compute_diff
from imap_poller import start_poller
from email_service import send_completion_email
from auth import require_manager, require_auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=8)

orders = {}

# ── Picking sort ──────────────────────────────────────────────────────────────

_WARM = {'basil', 'basilicum', 'basiliek', 'basilic'}

def _qty_num(line):
    try:
        return float(re.sub(r'[^\d.]', '', str(line.get('qty', 0))) or 0)
    except ValueError:
        return 0

def _is_warm(line):
    return any(w in line.get('product', '').lower() for w in _WARM)

def _sort_lines_for_picking(lines):
    groups = OrderedDict()
    for i, line in enumerate(lines):
        key = (line.get('transit', ''), line.get('product_group', ''))
        if key not in groups:
            groups[key] = []
        groups[key].append((i, line))

    for key in groups:
        groups[key].sort(key=lambda il: (not _is_warm(il[1]), -_qty_num(il[1])))

    transits = OrderedDict()
    for (transit, pg), items in groups.items():
        if transit not in transits:
            transits[transit] = []
        transits[transit].append(((transit, pg), items))

    for t in transits:
        transits[t].sort(key=lambda x: (
            not any(_is_warm(il[1]) for il in x[1]),
            -max((_qty_num(il[1]) for il in x[1]), default=0),
        ))

    result = []
    for group_list in transits.values():
        for _, items in group_list:
            result.extend(items)
    return result


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role = request.form.get("role")
        if role == "manager":
            user = verify_manager(
                request.form.get("email", ""),
                request.form.get("password", ""),
            )
            if user:
                session.permanent = True
                session.update({"user_id": user["id"], "role": "manager", "name": user["name"]})
                return redirect(request.args.get("next") or "/")
            return render_template("login.html", error="Invalid email or password",
                                   pickers=get_pickers(), active_tab="manager")
        elif role == "picker":
            try:
                picker_id = int(request.form.get("picker_id", 0))
            except ValueError:
                picker_id = 0
            user = verify_picker(picker_id, request.form.get("pin", ""))
            if user:
                session.permanent = True
                session.update({"user_id": user["id"], "role": "picker", "name": user["name"]})
                return redirect(request.args.get("next") or "/")
            return render_template("login.html", error="Incorrect PIN — try again",
                                   pickers=get_pickers(), active_tab="picker")
    return render_template("login.html", pickers=get_pickers(), active_tab="manager")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── Dashboard (manager) ───────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    if session.get("role") == "picker":
        uid = session.get("user_id")
        my_orders = [o for o in orders.values() if (o.get("picker") or {}).get("id") == uid]
        my_orders = sorted(my_orders, key=lambda o: o.get("departure_date", ""))
        return render_template("picker_home.html", orders=my_orders, user_name=session.get("name"))

    def _priority(o):
        if o.get("has_update"):           return 0
        if o["status"] == "partial":      return 1
        if o["status"] == "pending":      return 2
        if o["status"] in ("assigned", "in_progress"): return 3
        return 4

    order_list = sorted(
        orders.values(),
        key=lambda o: (_priority(o), o.get("departure_date", ""), o.get("created_at", "")),
    )
    needs_attention = [o for o in order_list if _priority(o) <= 1]
    return render_template(
        "dashboard.html",
        orders=order_list,
        needs_attention=needs_attention,
        pickers=get_pickers(),
        managers=get_managers(),
        user_name=session.get("name"),
        today=_today(),
        tomorrow=_tomorrow(),
    )


# ── Order actions ─────────────────────────────────────────────────────────────

@app.route("/assign/<order_id>", methods=["POST"])
@require_manager
def assign(order_id):
    if order_id not in orders:
        return "Not found", 404
    data = request.json
    try:
        picker_id = int(data.get("picker_id", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid picker_id"}), 400

    picker = get_user_by_id(picker_id)
    if not picker or picker["role"] != "picker":
        return jsonify({"error": "picker not found"}), 400

    orders[order_id].update({
        "picker":      {"id": picker["id"], "name": picker["name"]},
        "status":      "assigned",
        "assigned_at": datetime.now().isoformat(),
    })
    save_order(orders[order_id])
    pick_url = f"{config.BASE_URL}/pick/{order_id}"
    return jsonify({"pick_url": pick_url, "picker_name": picker["name"]})


@app.route("/pick/<order_id>")
@require_auth
def pick(order_id):
    if order_id not in orders:
        return "Order not found", 404
    order = orders[order_id]

    if session.get("role") == "picker":
        assigned_id = (order.get("picker") or {}).get("id")
        if assigned_id != session.get("user_id"):
            return render_template("not_assigned.html", order_id=order_id), 403

    if order["status"] in ("assigned", "pending"):
        orders[order_id]["status"] = "in_progress"
        save_order(orders[order_id])

    lines_with_idx = _sort_lines_for_picking(order["lines"])
    return render_template("pick.html", order=order, lines_with_idx=lines_with_idx, managers=get_managers())


@app.route("/update_line/<order_id>/<int:line_idx>", methods=["POST"])
@require_auth
def update_line(order_id, line_idx):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    data = request.json
    line = orders[order_id]["lines"][line_idx]
    line["status"]    = data.get("status",    line["status"])
    line["short_qty"] = data.get("short_qty", line["short_qty"])
    line["note"]      = data.get("note",      line["note"])
    save_order(orders[order_id])
    return jsonify({"ok": True})


@app.route("/complete/<order_id>", methods=["POST"])
@require_auth
def complete(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    data = request.json
    try:
        manager_id = int(data.get("manager_id", 0))
    except (TypeError, ValueError):
        manager_id = 0

    order   = orders[order_id]
    lines   = order["lines"]
    picked  = sum(1 for l in lines if l["status"] == "picked")
    short   = sum(1 for l in lines if l["status"] == "short")
    missing = sum(1 for l in lines if l["status"] == "missing")
    later   = sum(1 for l in lines if l["status"] == "later")
    total   = len(lines)

    final_status = "completed" if short == 0 and missing == 0 and later == 0 else "partial"
    orders[order_id]["status"]       = final_status
    orders[order_id]["completed_at"] = datetime.now().isoformat()
    save_order(orders[order_id])

    manager = get_user_by_id(manager_id)
    if manager and manager.get("email"):
        send_completion_email(orders[order_id], manager["email"])

    return jsonify({"status": final_status, "picked": picked, "short": short, "missing": missing, "later": later, "total": total})


@app.route("/reopen/<order_id>", methods=["POST"])
@require_manager
def reopen(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    orders[order_id]["status"]       = "in_progress"
    orders[order_id]["completed_at"] = None
    save_order(orders[order_id])
    return jsonify({"ok": True})


@app.route("/accept_update/<order_id>", methods=["POST"])
@require_manager
def accept_update(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    new_lines = orders[order_id].get("pending_lines", [])
    if new_lines:
        orders[order_id]["lines"].extend(new_lines)
    orders[order_id].update({"has_update": False, "update_diff": "", "pending_lines": []})
    if orders[order_id]["status"] in ("completed", "partial"):
        orders[order_id]["status"] = "in_progress"
    save_order(orders[order_id])
    return jsonify({"ok": True})


@app.route("/dismiss_update/<order_id>", methods=["POST"])
@require_manager
def dismiss_update(order_id):
    if order_id not in orders:
        return jsonify({"error": "not found"}), 404
    orders[order_id].update({"has_update": False, "update_diff": "", "pending_lines": []})
    save_order(orders[order_id])
    return jsonify({"ok": True})


# ── Admin: user management ────────────────────────────────────────────────────

@app.route("/admin/users", methods=["GET"])
@require_manager
def admin_users_list():
    return jsonify(get_all_users())


@app.route("/admin/users", methods=["POST"])
@require_manager
def admin_users_create():
    data = request.json
    role = data.get("role")
    try:
        if role == "picker":
            uid = create_picker(data["name"], data["pin"])
        elif role == "manager":
            uid = create_manager(data["name"], data["email"], data["password"])
        else:
            return jsonify({"error": "invalid role"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"id": uid})


@app.route("/admin/users/<int:user_id>", methods=["PUT"])
@require_manager
def admin_users_update(user_id):
    data = request.json
    try:
        update_user(user_id, data.get("name"), data.get("email"), data.get("secret"), data.get("role"))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/admin/users/<int:user_id>", methods=["DELETE"])
@require_manager
def admin_users_delete(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"error": "cannot delete yourself"}), 400
    delete_user(user_id)
    return jsonify({"ok": True})


# ── API / debug ───────────────────────────────────────────────────────────────

@app.route("/api/orders")
@require_manager
def api_orders():
    return jsonify(list(orders.values()))


@app.route("/debug/imap")
@require_manager
def debug_imap():
    import imaplib, email as email_lib
    import config
    result = {"host": config.IMAP_HOST, "port": config.IMAP_PORT,
              "user": config.IMAP_USER, "pass_set": bool(config.IMAP_PASS),
              "steps": []}
    def log(msg):
        result["steps"].append(msg)
        logger.info(msg)
    if not config.IMAP_PASS:
        log("IMAP_PASS is empty — cannot connect")
        return jsonify(result)
    try:
        log("Connecting…")
        mail = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
        log("Logging in…")
        mail.login(config.IMAP_USER, config.IMAP_PASS)
        log("Login OK")
        mail.select("INBOX")
        _, unseen = mail.search(None, "UNSEEN")
        _, all_ids = mail.search(None, "ALL")
        unseen_ids = (unseen[0] or b"").split()
        all_count  = len((all_ids[0] or b"").split())
        log(f"INBOX: {all_count} total, {len(unseen_ids)} unseen")
        result["unseen"] = len(unseen_ids)
        result["total"]  = all_count
        emails = []
        for mid in unseen_ids[-10:]:  # last 10 unseen
            _, msg_data = mail.fetch(mid, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            parts = []
            for part in msg.walk():
                ct = part.get_content_type()
                fn = part.get_filename() or ""
                if fn or ct not in ("multipart/mixed", "multipart/alternative"):
                    parts.append({"ct": ct, "filename": fn})
            emails.append({"subject": msg.get("Subject",""), "from": msg.get("From",""), "parts": parts})
        result["emails"] = emails
        # also trigger a full re-poll of ALL mail (not just unseen)
        search = request.args.get("search", "UNSEEN")
        from imap_poller import _poll_once
        mail.logout()
        log(f"Triggering poll with filter={search}…")
        _poll_once(search)
        log("Poll complete — check dashboard for new orders")
    except Exception as e:
        log(f"ERROR: {e}")
        result["error"] = str(e)
    return jsonify(result)


@app.route("/test/load")
@require_manager
def test_load():
    sample = os.path.join(os.path.dirname(__file__), "sample.xml")
    if not os.path.exists(sample):
        return "sample.xml not found", 404
    with open(sample, "rb") as f:
        order = parse_picking_xml(f.read())
    if not order:
        return "Failed to parse sample.xml", 500
    orders[order["order_id"]] = order
    save_order(order)
    return redirect("/")


@app.route("/test/load-all", methods=["POST"])
@require_manager
def test_load_all():
    test_dir = os.path.join(os.path.dirname(__file__), "test-data")
    if not os.path.isdir(test_dir):
        return jsonify({"error": "test-data directory not found"}), 404
    loaded = []
    for fname in sorted(os.listdir(test_dir)):
        if not fname.endswith(".xml"):
            continue
        with open(os.path.join(test_dir, fname), "rb") as f:
            order = parse_picking_xml(f.read())
        if not order:
            continue
        oid = order["order_id"]
        if oid in orders and fname.endswith("UPDATE.xml"):
            diff_str, new_lines = __import__("xml_parser").compute_diff(orders[oid], order)
            if new_lines or diff_str != "Minor update received":
                orders[oid]["has_update"]    = True
                orders[oid]["update_diff"]   = diff_str
                orders[oid]["pending_lines"] = new_lines
                save_order(orders[oid])
                loaded.append(f"update:{oid}")
        else:
            orders[oid] = order
            save_order(order)
            loaded.append(f"new:{oid}")
    return jsonify({"loaded": loaded})


# ── Health check ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"ok": True})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today():
    return date.today().strftime("%d-%m-%Y")

def _tomorrow():
    return (date.today() + timedelta(days=1)).strftime("%d-%m-%Y")


# ── Startup ───────────────────────────────────────────────────────────────────

init_db()
orders.update(load_orders())
logger.info(f"Loaded {len(orders)} order(s) from database")
start_poller(orders, save_order)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
