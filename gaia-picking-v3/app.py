import os
import logging
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

    order_list = sorted(
        orders.values(),
        key=lambda o: (o.get("departure_date", ""), o.get("created_at", "")),
    )
    return render_template(
        "dashboard.html",
        orders=order_list,
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

    return render_template("pick.html", order=order, managers=get_managers())


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
    total   = len(lines)

    final_status = "completed" if short == 0 and missing == 0 else "partial"
    orders[order_id]["status"]       = final_status
    orders[order_id]["completed_at"] = datetime.now().isoformat()
    save_order(orders[order_id])

    manager = get_user_by_id(manager_id)
    if manager and manager.get("email"):
        send_completion_email(orders[order_id], manager["email"])

    return jsonify({"status": final_status, "picked": picked, "short": short, "missing": missing, "total": total})


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
    return (
        f"Loaded #{order['order_id']} — {len(order['lines'])} lines<br><br>"
        + "<br>".join(
            f"• {l['product']} | qty:{l['qty']} {l['unit']} | "
            f"lot:<b>{l['lot'] or '—'}</b> | grower:{l['grower'] or '—'}"
            for l in order["lines"]
        )
    )


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
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
