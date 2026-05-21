import imaplib
import email as email_lib
import threading
import time
import logging
from collections import deque
from datetime import datetime
from config import IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS, POLL_INTERVAL
from xml_parser import parse_picking_xml, compute_diff

logger = logging.getLogger(__name__)

_orders    = None
_save_cb   = None

# Rolling log of last 100 poll events — readable from the dashboard
poll_log: deque = deque(maxlen=100)

def _log(level, msg, **extra):
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "msg": msg, **extra}
    poll_log.appendleft(entry)
    getattr(logger, level)(msg)


def start_poller(orders, save_order_cb):
    global _orders, _save_cb
    _orders  = orders
    _save_cb = save_order_cb
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info("IMAP poller started")


def _loop():
    while True:
        try:
            _poll_once()
        except Exception as e:
            _log("error", f"Poller error: {e}")
        time.sleep(POLL_INTERVAL)


def _poll_once(search_filter="UNSEEN"):
    if not IMAP_PASS:
        _log("warning", "IMAP_PASS not set — skipping poll")
        return
    _log("info", f"Polling inbox ({search_filter})…")
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
    except Exception as e:
        _log("error", f"IMAP connect/login failed: {e}")
        return
    mail.select("INBOX")
    _, msg_ids = mail.search(None, search_filter)
    ids = (msg_ids[0] or b"").split()
    _log("info", f"Inbox: {len(ids)} message(s) matching '{search_filter}'")
    found_xml = 0
    for mid in ids:
        _, msg_data = mail.fetch(mid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email_lib.message_from_bytes(raw)
        subject  = msg.get("Subject", "(no subject)")
        sender   = msg.get("From", "")
        for part in msg.walk():
            ct = part.get_content_type()
            fn = part.get_filename() or ""
            is_xml = (
                ct in ("application/xml", "text/xml", "application/octet-stream")
                or fn.lower().endswith(".xml")
            )
            if is_xml and fn:
                xml_bytes = part.get_payload(decode=True)
                if xml_bytes:
                    found_xml += 1
                    incoming = parse_picking_xml(xml_bytes)
                    if incoming:
                        _log("info", f"Email parsed OK → order #{incoming['order_id']}",
                             subject=subject, sender=sender, filename=fn,
                             order_id=incoming["order_id"], customer=incoming.get("customer",""))
                        _process(incoming)
                    else:
                        _log("warning", f"XML attached but could not parse as picking list",
                             subject=subject, sender=sender, filename=fn)
                else:
                    _log("warning", f"Empty attachment {fn!r}", subject=subject, sender=sender)
        else:
            # only log emails with no XML found
            if not any(
                (p.get_filename() or "").lower().endswith(".xml") or
                p.get_content_type() in ("application/xml","text/xml")
                for p in msg.walk()
            ):
                _log("info", f"Email with no XML attachment — skipped",
                     subject=subject, sender=sender)
    _log("info", f"Poll done — {found_xml} XML attachment(s) processed")
    mail.logout()


def _process(incoming):
    oid = incoming["order_id"]
    if oid not in _orders:
        _orders[oid] = incoming
        _save_cb(incoming)
        _log("info", f"New order #{oid} added — {len(incoming['lines'])} lines", order_id=oid)
    else:
        diff_str, new_lines = compute_diff(_orders[oid], incoming)
        if new_lines or diff_str != "Minor update received":
            _orders[oid]["has_update"]    = True
            _orders[oid]["update_diff"]   = diff_str
            _orders[oid]["pending_lines"] = new_lines
            _save_cb(_orders[oid])
            _log("info", f"Order #{oid} updated: {diff_str}", order_id=oid)
            _notify_managers(incoming, diff_str)
        else:
            _log("info", f"Order #{oid} re-received, no changes", order_id=oid)


def _notify_managers(order, diff_str):
    try:
        from database import get_managers
        from email_service import send_update_email
        for mgr in get_managers():
            if mgr.get("email"):
                send_update_email(order, mgr["email"], diff_str)
    except Exception as e:
        logger.error(f"Update email failed: {e}")
