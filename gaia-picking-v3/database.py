import sqlite3
import json
import hashlib
import logging
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                email         TEXT    UNIQUE,
                password_hash TEXT,
                pin_hash      TEXT,
                role          TEXT    NOT NULL CHECK(role IN ('picker','manager')),
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS orders (
                id         TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                status     TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cnt = conn.execute("SELECT COUNT(*) FROM users WHERE role='manager'").fetchone()[0]
        if cnt == 0:
            conn.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
                ("Nadav", "nadav@gaiaherbs.nl", _hash("admin123"), "manager")
            )
            logger.info("Created default manager: nadav@gaiaherbs.nl / admin123 — CHANGE THIS PASSWORD")


# ── Orders ────────────────────────────────────────────────────────────────────

def load_orders():
    with get_db() as conn:
        rows = conn.execute("SELECT id, data FROM orders").fetchall()
    result = {}
    for row in rows:
        try:
            result[row["id"]] = json.loads(row["data"])
        except Exception:
            pass
    return result


def save_order(order):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO orders (id, data, status, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 data=excluded.data, status=excluded.status,
                 updated_at=CURRENT_TIMESTAMP""",
            (order["order_id"], json.dumps(order), order["status"])
        )


# ── Users ─────────────────────────────────────────────────────────────────────

def get_all_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, email, role, created_at FROM users ORDER BY role, name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def get_pickers():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM users WHERE role='picker' ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_managers():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, email FROM users WHERE role='manager' ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def create_picker(name, pin):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, pin_hash, role) VALUES (?,?,?)",
            (name, _hash(pin), "picker")
        )
        return cur.lastrowid


def create_manager(name, email, password):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
            (name, email.lower(), _hash(password), "manager")
        )
        return cur.lastrowid


def update_user(user_id, name, email, new_secret, role):
    with get_db() as conn:
        if new_secret:
            col = "pin_hash" if role == "picker" else "password_hash"
            conn.execute(
                f"UPDATE users SET name=?, email=?, {col}=? WHERE id=?",
                (name, email, _hash(new_secret), user_id)
            )
        else:
            conn.execute(
                "UPDATE users SET name=?, email=? WHERE id=?",
                (name, email, user_id)
            )


def delete_user(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def verify_manager(email, password):
    user = get_user_by_email(email)
    if not user or user["role"] != "manager":
        return None
    return user if user["password_hash"] == _hash(password) else None


def verify_picker(user_id, pin):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id=? AND role='picker' AND pin_hash=?",
            (user_id, _hash(pin))
        ).fetchone()
    return dict(row) if row else None
