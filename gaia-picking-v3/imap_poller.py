import imaplib
import email as email_lib
import threading
import time
import logging
from config import IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS, POLL_INTERVAL
from xml_parser import parse_picking_xml, compute_diff

logger = logging.getLogger(__name__)

_orders    = None
_save_cb   = None


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
            logger.error(f"Poller error: {e}")
        time.sleep(POLL_INTERVAL)


def _poll_once(search_filter="UNSEEN"):
    if not IMAP_PASS:
        logger.warning("IMAP_PASS not set — skipping poll")
        return
    logger.info(f"Polling inbox ({search_filter})…")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASS)
    mail.select("INBOX")
    _, msg_ids = mail.search(None, search_filter)
    ids = (msg_ids[0] or b"").split()
    logger.info(f"Found {len(ids)} message(s) matching {search_filter}")
    found_xml = 0
    for mid in ids:
        _, msg_data = mail.fetch(mid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email_lib.message_from_bytes(raw)
        subject = msg.get("Subject", "")
        logger.info(f"  Email: {subject!r}")
        for part in msg.walk():
            ct = part.get_content_type()
            fn = part.get_filename() or ""
            logger.info(f"    Part: {ct} filename={fn!r}")
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
                        _process(incoming)
                    else:
                        logger.warning(f"    Could not parse XML from {fn!r}")
    logger.info(f"Poll done — processed {found_xml} XML attachment(s)")
    mail.logout()


def _process(incoming):
    oid = incoming["order_id"]
    if oid not in _orders:
        _orders[oid] = incoming
        _save_cb(incoming)
        logger.info(f"New order #{oid} — {len(incoming['lines'])} lines")
    else:
        diff_str, new_lines = compute_diff(_orders[oid], incoming)
        if new_lines or diff_str != "Minor update received":
            _orders[oid]["has_update"]    = True
            _orders[oid]["update_diff"]   = diff_str
            _orders[oid]["pending_lines"] = new_lines
            _save_cb(_orders[oid])
            logger.info(f"Update #{oid}: {diff_str}")
            _notify_managers(incoming, diff_str)


def _notify_managers(order, diff_str):
    try:
        from database import get_managers
        from email_service import send_update_email
        for mgr in get_managers():
            if mgr.get("email"):
                send_update_email(order, mgr["email"], diff_str)
    except Exception as e:
        logger.error(f"Update email failed: {e}")
