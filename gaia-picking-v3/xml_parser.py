import xml.etree.ElementTree as ET
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

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
    ref            = find_by_field(root, "VKHREF")

    customer_raw   = find_by_objname(root, "Text7")
    customer_lines = [l.strip() for l in customer_raw.split("\n") if l.strip()]
    customer       = customer_lines[0] if customer_lines else customer_raw

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
        logger.warning("Could not extract order or lines from XML")
        return None

    return {
        "order_id":       order_num,
        "customer":       customer,
        "ref":            ref,
        "sale_date":      sale_date,
        "departure_date": departure_date,
        "handled_by":     handled_by,
        "delivery_terms": delivery_terms,
        "carrier":        carrier,
        "lines":          lines,
        "status":         "pending",
        "picker":         None,
        "assigned_at":    None,
        "completed_at":   None,
        "created_at":     datetime.now().isoformat(),
        "has_update":     False,
        "update_diff":    "",
        "pending_lines":  [],
    }


def compute_diff(existing, incoming):
    ex_keys = {f"{l['product']}|{l['lot']}|{l['qty']}" for l in existing["lines"]}
    in_keys = {f"{l['product']}|{l['lot']}|{l['qty']}" for l in incoming["lines"]}

    added   = [l for l in incoming["lines"] if f"{l['product']}|{l['lot']}|{l['qty']}" not in ex_keys]
    removed = [l for l in existing["lines"] if f"{l['product']}|{l['lot']}|{l['qty']}" not in in_keys]

    parts = []
    if added:
        parts.append(f"+ {len(added)} new line(s): " + ", ".join(l["product"] for l in added))
    if removed:
        parts.append(f"- {len(removed)} removed: " + ", ".join(l["product"] for l in removed))

    for field, label in [("customer","Customer"),("departure_date","Departure"),("delivery_terms","Terms")]:
        if existing.get(field) != incoming.get(field):
            parts.append(f"{label}: {existing.get(field)} → {incoming.get(field)}")

    return " | ".join(parts) if parts else "Minor update received", added
