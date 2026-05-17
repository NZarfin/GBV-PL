#!/usr/bin/env python3
"""Generate 5 realistic test picking list XML files."""
import os

OUT = os.path.join(os.path.dirname(__file__), "test-data")
os.makedirs(OUT, exist_ok=True)

NS = "urn:crystal-reports:schemas"

def field(fname, ftype, fvalue, objname, raw=None):
    raw = raw or fvalue
    return f"""<FormattedReportObject xsi:type="CTFormattedField" Type="{ftype}" FieldName="{fname}"><ObjectName>{objname}</ObjectName>
<FormattedValue>{fvalue}</FormattedValue><Value>{raw}</Value>
</FormattedReportObject>"""

def text_obj(objname, value):
    return f"""<FormattedReportObject xsi:type="CTFormattedText"><ObjectName>{objname}</ObjectName>
<TextValue>{value}</TextValue>
</FormattedReportObject>"""

def date_field(fname, objname, fvalue):
    return f"""<FormattedReportObject xsi:type="CTFormattedField" Type="xsd:date" FieldName="{fname}"><ObjectName>{objname}</ObjectName>
<FormattedValue>{fvalue}</FormattedValue>
</FormattedReportObject>"""

def line_xml(product, size, qty, unit, lot, grower, arr_date, pallet, country):
    grower_v = grower or ""
    arr_v    = arr_date or ""
    return f"""<FormattedAreaPair Level="6" Type="Details">
<FormattedArea Type="Details">
<FormattedSections>
<FormattedSection SectionNumber="0">
<FormattedReportObjects>
{field('{EENH_VRD.EHDAFKRT}','xsd:string',unit,'EHDAFKRT1')}
{field('{ARTVARMT.AVMACODE}','xsd:string',size,'AVMACODE2')}
{field('{VRKD.VKDAANBS}','xsd:float',str(qty),'VKDAANBS1',str(float(qty)))}
{field('{ART.ARTOMS}','xsd:string',product,'ARTOMS3')}
{text_obj('Text20', lot)}
{field('{@xGrower}','xsd:string',grower_v,'xGrower1')}
{date_field('{INKH.IKHADAT}','IKHADAT1',arr_v)}
{field('{@xPallet}','xsd:string',pallet,'xPallet1')}
{field('{@xLandLeverancier}','xsd:string',country,'xLandLeverancier1')}
</FormattedReportObjects>
</FormattedSection>
<FormattedSection SectionNumber="1"><FormattedReportObjects></FormattedReportObjects></FormattedSection>
<FormattedSection SectionNumber="2"><FormattedReportObjects></FormattedReportObjects></FormattedSection>
<FormattedSection SectionNumber="3"><FormattedReportObjects></FormattedReportObjects></FormattedSection>
</FormattedSections>
</FormattedArea>
</FormattedAreaPair>"""

def group3_xml(product_group, lines):
    lines_xml = "\n".join(line_xml(*l) for l in lines)
    return f"""<FormattedAreaPair Level="3" Type="Group">
<FormattedArea Type="Header">
<FormattedSections><FormattedSection SectionNumber="0">
<FormattedReportObjects>
{field('GroupName ({{ARTGRP.AGPOMS}})','xsd:string',product_group,'GroupNameAGPOMS1')}
</FormattedReportObjects>
</FormattedSection></FormattedSections>
</FormattedArea>
<FormattedAreaPair Level="4" Type="Group">
<FormattedAreaPair Level="5" Type="Group">
{lines_xml}
</FormattedAreaPair>
</FormattedAreaPair>
</FormattedAreaPair>"""

def group2_xml(transit, groups):
    groups_xml = "\n".join(group3_xml(pg, lines) for pg, lines in groups)
    return f"""<FormattedAreaPair Level="2" Type="Group">
<FormattedArea Type="Header">
<FormattedSections><FormattedSection SectionNumber="0">
<FormattedReportObjects>
{field('GroupName ({{@Awz_Rol}})','xsd:string',transit,'GroupNameAwzRol1')}
</FormattedReportObjects>
</FormattedSection></FormattedSections>
</FormattedArea>
{groups_xml}
<FormattedArea Type="Footer"><FormattedSections><FormattedSection SectionNumber="0">
<FormattedReportObjects></FormattedReportObjects></FormattedSection></FormattedSections></FormattedArea>
</FormattedAreaPair>"""

def build_order(order_num, customer, sale_date, departure_date, handler, carrier, terms, transit_groups):
    order_num_fmt = f"{order_num:,}".replace(",", ".")  # e.g. 50101 → "50.101" (European thousand sep)
    groups_xml = "\n".join(group2_xml(t, gs) for t, gs in transit_groups)
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<FormattedReport xmlns='urn:crystal-reports:schemas' xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'>
<FormattedAreaPair Level="0" Type="Report">
<FormattedAreaPair Level="1" Type="Group">
<FormattedArea Type="Header">
<FormattedSections>
<FormattedSection SectionNumber="0">
<FormattedReportObjects>
{text_obj('Text7', customer)}
{field('{{VRKH.VKHORDNR}}','xsd:float',order_num_fmt,'VKHORDNR1',f'{order_num}.00')}
{date_field('{{VRKH.VKHDAT}}','VKHDAT1',sale_date)}
{field('{{VRKH.VKHBHUSR}}','xsd:string',handler,'VKHBHUSR1')}
{field('{{REL_TRNS.RELNAAM1}}','xsd:string',carrier,'RELNAAM12')}
{date_field('{{VRKH.VKHVDAT}}','VKHVDAT1',departure_date)}
{field('{{LEVC.LVCOMS}}','xsd:string',terms,'LVCOMS1')}
</FormattedReportObjects>
</FormattedSection>
<FormattedSection SectionNumber="2"><FormattedReportObjects></FormattedReportObjects></FormattedSection>
</FormattedSections>
</FormattedArea>
{groups_xml}
<FormattedArea Type="Footer"><FormattedSections><FormattedSection SectionNumber="0">
<FormattedReportObjects></FormattedReportObjects></FormattedSection></FormattedSections></FormattedArea>
</FormattedAreaPair>
<FormattedArea Type="Footer"><FormattedSections><FormattedSection SectionNumber="0">
<FormattedReportObjects></FormattedReportObjects></FormattedSection></FormattedSections></FormattedArea>
</FormattedAreaPair>
</FormattedReport>"""

# ── Order definitions ──────────────────────────────────────────────────────────
# line tuple: (product, size, qty, unit, lot, grower, arr_date, pallet, country)

orders = [
    # 1. Greenfield Supermarkets UK — big mixed order
    dict(
        order_num=50101, customer="Greenfield Supermarkets\nLondon, UK",
        sale_date="14-05-2026", departure_date="19-05-2026",
        handler="NADAV", carrier="DHL Freight", terms="Delivered Duty Paid",
        transit_groups=[
            ("", [
                ("BAGS", [
                    ("Basilicum 100 g Flowpack Bags",  "20X100G", 8, "Cl", "-.-.26.041", "Van den Berg", "",           "Block", "NL"),
                    ("Chervil 100 g Flowpack Bags",    "20X100G", 5, "Cl", "-.-.26.038", "De Groot",     "",           "Block", "NL"),
                    ("Coriander 100 g Flowpack Bags",  "20X100G", 6, "Cl", "-.-.26.019", "",             "",           "Block", "MA"),
                    ("Dill 100 g Flowpack Bags",       "20X100G", 3, "Cl", "-.-.26.033", "Sijm",         "",           "Block", "NL"),
                ]),
                ("FLOWPACK", [
                    ("Bay Leaves 100 g Flowpack",      "20X100G", 2, "Cl", "-.-.26.022", "",             "",           "Block", "NL"),
                    ("Flat Parsley 100 g Flowpack",    "20X100G", 4, "Cl", "-.-.26.015", "Atlas Farms",  "",           "Block", "MA"),
                ]),
            ]),
        ],
    ),

    # 2. Bio Planet Belgium — with transit (arr_date) items
    dict(
        order_num=50102, customer="Bio Planet Belgium\nBrussels, BE",
        sale_date="15-05-2026", departure_date="20-05-2026",
        handler="ALAN", carrier="Ewals Cargo", terms="Ex Works",
        transit_groups=[
            ("Transit", [
                ("BAGS", [
                    ("Tarragon 100 g Flowpack Bags",   "20X100G", 3, "Cl", "-.-.26.051", "Hortibel",    "22-05-2026", "Block", "BE"),
                    ("Lemon Thyme 100 g Flowpack Bags","20X100G", 2, "Cl", "-.-.26.052", "",             "21-05-2026", "Euro",  "ES"),
                ]),
            ]),
            ("", [
                ("BAGS", [
                    ("Chervil 100 g Flowpack Bags",    "20X100G", 6, "Cl", "-.-.26.038", "De Groot",    "",           "Block", "NL"),
                    ("Basilicum 100 g Flowpack Bags",  "20X100G", 4, "Cl", "-.-.26.041", "Van den Berg","",           "Block", "NL"),
                ]),
                ("FLOWPACK", [
                    ("Rosemary 100 g Flowpack",        "20X100G", 5, "Cl", "-.-.26.027", "Sol Natural", "",           "Block", "ES"),
                    ("Thyme 100 g Flowpack",           "20X100G", 3, "Cl", "-.-.26.028", "Sol Natural", "",           "Block", "ES"),
                ]),
            ]),
        ],
    ),

    # 3. Delhaize Netherlands — order with items arriving soon
    dict(
        order_num=50103, customer="Delhaize Nederland\nAmsterdam, NL",
        sale_date="15-05-2026", departure_date="18-05-2026",
        handler="NADAV", carrier="", terms="Free Carrier",
        transit_groups=[
            ("Transit", [
                ("BAGS", [
                    ("Oregano 100 g Flowpack Bags",    "20X100G", 4, "Cl", "-.-.26.060", "",             "19-05-2026", "Euro",  "TR"),
                    ("Sage 100 g Flowpack Bags",       "20X100G", 2, "Cl", "-.-.26.061", "",             "19-05-2026", "Euro",  "TR"),
                ]),
            ]),
            ("", [
                ("BAGS", [
                    ("Basilicum 100 g Flowpack Bags",  "20X100G", 10, "Cl","-.-.26.041", "Van den Berg","",           "Block", "NL"),
                    ("Flat Parsley 100 g Flowpack Bags","20X100G", 7, "Cl","-.-.26.015", "Atlas Farms", "",           "Block", "MA"),
                    ("Coriander 100 g Flowpack Bags",  "20X100G", 5, "Cl","-.-.26.019", "",             "",           "Block", "MA"),
                ]),
                ("FLOWPACK", [
                    ("Mint 100 g Flowpack",            "20X100G", 3, "Cl", "-.-.26.044", "Meulenbroek", "",           "Block", "NL"),
                ]),
            ]),
        ],
    ),

    # 4. Order 50101 UPDATE — same order number, 2 extra lines added
    dict(
        order_num=50101, customer="Greenfield Supermarkets\nLondon, UK",
        sale_date="14-05-2026", departure_date="19-05-2026",
        handler="NADAV", carrier="DHL Freight", terms="Delivered Duty Paid",
        transit_groups=[
            ("", [
                ("BAGS", [
                    ("Basilicum 100 g Flowpack Bags",  "20X100G", 8,  "Cl", "-.-.26.041", "Van den Berg", "",           "Block", "NL"),
                    ("Chervil 100 g Flowpack Bags",    "20X100G", 5,  "Cl", "-.-.26.038", "De Groot",     "",           "Block", "NL"),
                    ("Coriander 100 g Flowpack Bags",  "20X100G", 6,  "Cl", "-.-.26.019", "",             "",           "Block", "MA"),
                    ("Dill 100 g Flowpack Bags",       "20X100G", 3,  "Cl", "-.-.26.033", "Sijm",         "",           "Block", "NL"),
                    # 2 new lines added in update:
                    ("Chives 100 g Flowpack Bags",     "20X100G", 4,  "Cl", "-.-.26.055", "De Groot",     "",           "Block", "NL"),
                    ("Tarragon 100 g Flowpack Bags",   "20X100G", 2,  "Cl", "-.-.26.051", "Hortibel",     "20-05-2026", "Block", "BE"),
                ]),
                ("FLOWPACK", [
                    ("Bay Leaves 100 g Flowpack",      "20X100G", 2,  "Cl", "-.-.26.022", "",             "",           "Block", "NL"),
                    ("Flat Parsley 100 g Flowpack",    "20X100G", 4,  "Cl", "-.-.26.015", "Atlas Farms",  "",           "Block", "MA"),
                ]),
            ]),
        ],
    ),

    # 5. Albert Heijn — large order multiple groups
    dict(
        order_num=50104, customer="Albert Heijn BV\nZaandam, NL",
        sale_date="16-05-2026", departure_date="22-05-2026",
        handler="ALAN", carrier="Albert Heijn Logistics", terms="Delivered Duty Paid",
        transit_groups=[
            ("Transit", [
                ("BAGS", [
                    ("Lemon Grass 100 g Flowpack Bags","20X100G", 6, "Cl", "-.-.26.070", "",             "23-05-2026", "Euro",  "TH"),
                ]),
            ]),
            ("", [
                ("BAGS", [
                    ("Basilicum 100 g Flowpack Bags",  "20X100G", 20, "Cl","-.-.26.041", "Van den Berg","",            "Block", "NL"),
                    ("Chervil 100 g Flowpack Bags",    "20X100G", 15, "Cl","-.-.26.038", "De Groot",    "",            "Block", "NL"),
                    ("Coriander 100 g Flowpack Bags",  "20X100G", 12, "Cl","-.-.26.019", "",            "",            "Block", "MA"),
                    ("Flat Parsley 100 g Flowpack Bags","20X100G",10, "Cl","-.-.26.015", "Atlas Farms", "",            "Block", "MA"),
                    ("Dill 100 g Flowpack Bags",       "20X100G", 8,  "Cl","-.-.26.033", "Sijm",        "",            "Block", "NL"),
                    ("Chives 100 g Flowpack Bags",     "20X100G", 6,  "Cl","-.-.26.055", "De Groot",    "",            "Block", "NL"),
                ]),
                ("FLOWPACK", [
                    ("Bay Leaves 100 g Flowpack",      "20X100G", 8,  "Cl","-.-.26.022", "",            "",            "Block", "NL"),
                    ("Thyme 100 g Flowpack",           "20X100G", 5,  "Cl","-.-.26.028", "Sol Natural", "",            "Block", "ES"),
                    ("Rosemary 100 g Flowpack",        "20X100G", 4,  "Cl","-.-.26.027", "Sol Natural", "",            "Block", "ES"),
                ]),
                ("POT HERBS", [
                    ("Basilicum Pot 12 cm",            "6X12CM",  15, "St","-.-.26.080", "Van den Berg","",            "Euro",  "NL"),
                    ("Mint Pot 12 cm",                 "6X12CM",  10, "St","-.-.26.081", "Meulenbroek", "",            "Euro",  "NL"),
                ]),
            ]),
        ],
    ),
]

filenames = [
    "order-50101-greenfield.xml",
    "order-50102-bioplanet.xml",
    "order-50103-delhaize.xml",
    "order-50101-greenfield-UPDATE.xml",
    "order-50104-albertheijn.xml",
]

for fname, o in zip(filenames, orders):
    xml = build_order(
        o["order_num"], o["customer"], o["sale_date"], o["departure_date"],
        o["handler"], o["carrier"], o["terms"], o["transit_groups"],
    )
    path = os.path.join(OUT, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"Written: {fname}")

print("Done — 5 test XML files in test-data/")
