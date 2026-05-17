import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import logging
from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS

logger = logging.getLogger(__name__)


def _send(to, subject, html, text):
    if not SMTP_PASS:
        logger.warning("SMTP_PASS not configured — email skipped")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Gaia Herbs Warehouse <{SMTP_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [to], msg.as_string())
        logger.info(f"Email sent → {to}: {subject}")
    except Exception as e:
        logger.error(f"Email failed to {to}: {e}")


def send_completion_email(order, manager_email):
    lines   = order["lines"]
    total   = len(lines)
    picked  = sum(1 for l in lines if l["status"] == "picked")
    short   = sum(1 for l in lines if l["status"] == "short")
    missing = sum(1 for l in lines if l["status"] == "missing")
    later   = sum(1 for l in lines if l["status"] == "later")
    is_clean = short == 0 and missing == 0 and later == 0

    picker_name = (order.get("picker") or {}).get("name", "Unknown")
    status_label = "All Picked" if is_clean else "Completed with Exceptions"
    icon = "✅" if is_clean else "⚠️"
    accent = "#2e7d32" if is_clean else "#e65100"
    bg     = "#e8f5e9" if is_clean else "#fff8e1"

    rows_html = ""
    plain_lines = []
    for l in lines:
        if l["status"] == "picked":
            si, detail = "✅", f"{l['qty']} {l['unit']}"
        elif l["status"] == "short":
            si, detail = "⚠️", f"{l.get('short_qty','?')}/{l['qty']} SHORT"
        elif l["status"] == "missing":
            si, detail = "❌", f"{l['qty']} MISSING"
        elif l["status"] == "later":
            si, detail = "🕐", f"{l['qty']} LATER (to be picked)"
        else:
            si, detail = "⬜", str(l["qty"])
        lot_str  = f" [lot {l['lot']}]" if l.get("lot") else ""
        note_str = f"<br><small style='color:#888'>📝 {l['note']}</small>" if l.get("note") else ""
        rows_html += (
            f"<tr style='border-bottom:1px solid #eee'>"
            f"<td style='padding:8px 10px'>{si}</td>"
            f"<td style='padding:8px 10px'>{l['product']}{lot_str}</td>"
            f"<td style='padding:8px 10px'>{detail}{note_str}</td></tr>"
        )
        plain_lines.append(f"{si} {l['product']}{lot_str} — {detail}")

    summary_html = f"✅ {picked} picked"
    if short:   summary_html += f" &nbsp; ⚠️ {short} short"
    if missing: summary_html += f" &nbsp; ❌ {missing} missing"
    if later:   summary_html += f" &nbsp; 🕐 {later} later"

    html = f"""<html><body style="font-family:'Helvetica Neue',sans-serif;color:#1a1a1e;max-width:600px;margin:0 auto;padding:24px">
<div style="background:{bg};border-radius:12px;padding:20px 24px;margin-bottom:20px">
  <h2 style="margin:0 0 4px;color:{accent}">{icon} Order #{order['order_id']} — {status_label}</h2>
  <p style="margin:0;color:#555;font-size:15px">{order.get('customer','')}</p>
</div>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:14px">
  <tr><td style="color:#888;padding:5px 0;width:100px">Picker</td><td><strong>{picker_name}</strong></td></tr>
  <tr><td style="color:#888;padding:5px 0">Departure</td><td>{order.get('departure_date','—')}</td></tr>
  <tr><td style="color:#888;padding:5px 0">Time</td><td>{datetime.now().strftime('%H:%M  %d-%m-%Y')}</td></tr>
  <tr><td style="color:#888;padding:5px 0">Summary</td><td>{summary_html}</td></tr>
</table>
<table style="width:100%;border-collapse:collapse;font-size:13px">
  <thead><tr style="background:#f5f5f7">
    <th style="text-align:left;padding:8px 10px">Status</th>
    <th style="text-align:left;padding:8px 10px">Product</th>
    <th style="text-align:left;padding:8px 10px">Detail</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="color:#bbb;font-size:11px;margin-top:28px">Gaia Herbs Warehouse System</p>
</body></html>"""

    text = (
        f"Order #{order['order_id']} — {status_label}\n"
        f"{order.get('customer','')}\n"
        f"Picker: {picker_name}  |  Departure: {order.get('departure_date','—')}\n"
        f"Summary: {picked}/{total} picked, {short} short, {missing} missing, {later} later\n\n"
        + "\n".join(plain_lines)
    )

    subject = f"{icon} Order #{order['order_id']} — {status_label} | {order.get('customer','')}"
    _send(manager_email, subject, html, text)


def send_update_email(order, manager_email, diff_str):
    html = f"""<html><body style="font-family:'Helvetica Neue',sans-serif;color:#1a1a1e;max-width:600px;margin:0 auto;padding:24px">
<div style="background:#f3e8ff;border-radius:12px;padding:20px 24px;margin-bottom:20px">
  <h2 style="margin:0 0 4px;color:#6b21a8">⚡ Order #{order['order_id']} — Update Received</h2>
  <p style="margin:0;color:#555;font-size:15px">{order.get('customer','')}</p>
</div>
<p style="font-size:14px"><strong>Changes detected:</strong></p>
<p style="background:#fdf4ff;border-left:4px solid #c4a0f5;padding:12px 16px;border-radius:0 8px 8px 0;font-size:14px">{diff_str}</p>
<p style="font-size:14px;color:#555">Log in to the dashboard to accept or dismiss this update.</p>
<p style="color:#bbb;font-size:11px;margin-top:28px">Gaia Herbs Warehouse System</p>
</body></html>"""

    text = (
        f"Order #{order['order_id']} Updated — {order.get('customer','')}\n\n"
        f"Changes: {diff_str}\n\n"
        "Log in to the dashboard to accept or dismiss."
    )
    _send(manager_email, f"⚡ Order #{order['order_id']} Updated — {order.get('customer','')}", html, text)
