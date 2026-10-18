"""
=============================================================================
 DATA REQUEST FORM - the questions customers ask that the bot cannot answer
=============================================================================

     python data_request.py            writes CarTrends-Data-Request-<date>.pdf

 A printable / fillable form for the employee who knows the prices. Every
 question on it is one the chatbot has had to answer with "our team will
 confirm": the 25 approved questions still marked as missing information in
 knowledge.py, plus what the weekly gap reports (12 and 18 Oct 2026) showed
 customers asking most. The numbers in brackets are FAQ ids in knowledge.py,
 so the answers can be put straight into the knowledge base.

 Plain fonts only (Helvetica): no rupee sign, no emoji - those render as
 black boxes in a PDF's built-in fonts, so prices are written "Rs".
=============================================================================
"""
import datetime
import os
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

TODAY = datetime.date.today()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   f"CarTrends-Data-Request-{TODAY:%Y-%m-%d}.pdf")

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#555555")
LINE = colors.HexColor("#9a9a9a")
BAND = colors.HexColor("#efefef")

styles = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18, leading=22,
                    alignment=TA_LEFT, textColor=INK, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, leading=16,
                    textColor=INK, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=13.5,
                      textColor=INK)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.5, leading=11, textColor=MUTED)
QUOTE = ParagraphStyle("quote", parent=BODY, fontSize=9, leading=12, textColor=MUTED,
                       leftIndent=8, fontName="Helvetica-Oblique")
Q = ParagraphStyle("q", parent=BODY, fontName="Helvetica-Bold", spaceBefore=6)

# Car sizes the price tables are organised by - the way customers name their
# car, with the models they actually asked about this month as examples.
SIZES = [
    ("Hatchback", "Alto, Swift, i10 Nios, Celerio, Tiago, Baleno"),
    ("Sedan", "Dzire, City, Ciaz, Verna, Aura, Virtus"),
    ("Compact SUV", "Punch, Nexon, Venue, Brezza, XUV 3XO, Sonet, Victoris"),
    ("SUV", "Creta, Harrier, Seltos, Hyryder, XUV700, Thar Roxx"),
    ("7-seater / big", "Ertiga, Innova, Fortuner, Bolero Neo, Scorpio, XUV 7XO"),
]


def answer_box(lines: int = 2) -> Table:
    """An empty ruled box to write in."""
    h = 7 * mm * lines
    t = Table([[""]], colWidths=[170 * mm], rowHeights=[h])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE),
                           ("BACKGROUND", (0, 0), (-1, -1), colors.white)]))
    return t


def price_table(columns) -> Table:
    """Car size rows x the given price columns, empty, to fill in."""
    head = [Paragraph("<b>Car size</b>", BODY)] + [Paragraph(f"<b>{c}</b>", BODY) for c in columns]
    rows = [head]
    for size, examples in SIZES:
        rows.append([Paragraph(f"<b>{size}</b><br/><font size=7.5 color='#555555'>{examples}</font>", BODY)]
                    + [""] * len(columns))
    first = 62 * mm
    rest = (170 * mm - first) / len(columns)
    t = Table(rows, colWidths=[first] + [rest] * len(columns),
              rowHeights=[9 * mm] + [None] * len(SIZES))
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
    ]))
    return t


def priced(label: str, columns, note_box: bool = False):
    """A question with its price table, kept on one page."""
    items = [Paragraph(label, Q), Spacer(1, 3), price_table(columns), Spacer(1, 3)]
    if note_box:
        items.append(answer_box(1))
    return KeepTogether(items)


def yes_no(label: str, extra: str = "") -> Paragraph:
    tail = f"&nbsp;&nbsp;&nbsp;{extra}" if extra else ""
    return Paragraph(f"{label}&nbsp;&nbsp;&nbsp;[&nbsp;&nbsp;] Yes&nbsp;&nbsp;&nbsp;[&nbsp;&nbsp;] No{tail}", BODY)


def question(text: str, why: str = "", quotes=(), lines: int = 2, faq=None):
    """One numbered question: the ask, why it matters, how customers put it, a box."""
    ref = f" <font size=7.5 color='#777777'>(FAQ {faq})</font>" if faq else ""
    items = [Paragraph(text + ref, Q)]
    if why:
        items.append(Paragraph(why, SMALL))
    for q in quotes:
        items.append(Paragraph(f'Customer wrote: "{q}"', QUOTE))
    items.append(Spacer(1, 2))
    items.append(answer_box(lines))
    return KeepTogether(items)


def heading(text: str, note: str = ""):
    items = [Paragraph(text, H2)]
    if note:
        items.append(Paragraph(note, SMALL))
    return KeepTogether(items + [Spacer(1, 2)])


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(20 * mm, 12 * mm, f"Car Trends Car Mall - chatbot data request - {TODAY:%d %b %Y}")
    canvas.drawRightString(190 * mm, 12 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build() -> str:
    doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=20 * mm,
                            title="Car Trends - chatbot data request",
                            author="Car Trends Car Mall")
    s = []

    # ---- cover / instructions ----------------------------------------
    s.append(Paragraph("Car Trends Car Mall - information the chatbot needs", H1))
    s.append(Paragraph(f"Data request, {TODAY:%d %B %Y}", SMALL))
    s.append(Spacer(1, 8))
    s.append(Paragraph(
        "Customers on Instagram ask our chatbot these questions every week. Right now it can "
        "only reply <i>\"our team will confirm\"</i>, because this information is not in it. "
        "Once you fill this in, the bot answers them itself - correctly, in seconds, at any hour.", BODY))
    s.append(Spacer(1, 6))
    s.append(Paragraph("<b>How to fill it in</b>", BODY))
    for rule in [
        "Write only what is <b>true today</b>. If you are not sure, leave it blank - a blank is "
        "far better than a guess, because the bot will repeat whatever is written here to every customer.",
        "For prices, write the <b>starting price in Rs</b> and what it depends on "
        "(e.g. \"from Rs 3,500, depends on variant\"). A range is fine (\"Rs 3,500 - 5,000\").",
        "Where there is a table, fill in the rows you can. One row filled is already useful.",
        "If something is <b>not offered</b>, write \"No\" - that is also an answer the bot needs.",
        "Hinglish is fine. The bot will say it properly in English and Hinglish.",
    ]:
        s.append(Paragraph(f"&bull;&nbsp; {rule}", BODY))
    s.append(Spacer(1, 8))
    s.append(Table([[Paragraph("Filled in by: ______________________________", BODY),
                     Paragraph("Date: ________________", BODY)]],
                   colWidths=[110 * mm, 60 * mm]))
    s.append(Spacer(1, 4))
    s.append(Paragraph(
        "<b>Most asked, by far:</b> floor-mat prices (13 customers in two weeks), car covers "
        "(4 this week), courier outside Jaipur (4), then PPF, ceramic, denting and service prices. "
        "Sections A and B alone would answer a third of everyone who writes in.", BODY))

    # ---- A. floor mats ------------------------------------------------
    s.append(heading("A. Floor mats",
                     "Asked 13 times in two weeks - Punch, Victoris, Ertiga, Dzire, Ciaz, Swift, Venue, "
                     "Bolero Neo, Creta, Nexon, Grand i10, Alto K10, Hyryder, Harrier, XUV 3XO, Thar Roxx."))
    s.append(priced("A1. Price of mats by car size (starting price, Rs). Add a note under the table if it "
                       "depends on something else, e.g. year or variant.", ["GFX Pro / Lifelong", "GFX Normal", "7D Lifelong / GF Lifelong"], note_box=True))
    s.append(question("A2. Warranty on the mats, if any (how long, what it covers).",
                      quotes=["GFX pro mats warranty?"], lines=1))
    s.append(question("A3. How long does fitting take, and does the customer wait or leave the car?", lines=1))
    s.append(question("A4. Do we send mats by courier outside Jaipur? Charges? Cash on delivery or advance?",
                      why="Asked 4 times this week alone - Ahmedabad, Hyderabad, Navi Mumbai, and a Swift "
                          "customer. The bot currently says only that the team will confirm.",
                      quotes=["Please let me know the price. Deliverable in ahmedabad?",
                              "Swift 2026 gfx mat how much price courier available"], lines=2))
    s.append(question("A5. Bucket-type / raised-edge mats - is that GFX Pro, or a separate product?",
                      quotes=["Required Floor mat ... Bucket type"], lines=1))

    # ---- B. car covers ------------------------------------------------
    s.append(heading("B. Car covers",
                     "New this week: 4 customers (XUV 7XO, Swift 2016, Innova old model, and the Reel "
                     "captioned Comment \"cover\"). The bot does not know whether we sell them at all."))
    s.append(yes_no("B1. Do we sell car body covers?"))
    s.append(Spacer(1, 4))
    s.append(question("B2. Types and brands (waterproof? with mirror pockets? elastic?)", lines=2))
    s.append(priced("B3. Price by car size (starting price, Rs).", ["Standard cover", "Premium / waterproof"]))
    s.append(question("B4. Courier outside Jaipur for covers? Charges?", lines=1))

    # ---- C. PPF ---------------------------------------------------------
    s.append(heading("C. PPF (paint protection film)",
                     "FAQs 20 and 23 are waiting; this is the service customers ask about second-most after mats."))
    s.append(priced("C1. Full-body PPF price by car size (starting price, Rs).", ["Full body", "Bonnet + bumper only"]))
    s.append(question("C2. PPF warranty - how many years, and what it covers (yellowing? peeling? scratches?).",
                      quotes=["warranty kitni h ppf pe"], lines=2, faq=23))
    s.append(question("C3. How many days does a full-body PPF take?", lines=1))
    s.append(question("C4. Which PPF brands/films do we use? (customers ask Garware / XPEL / Llumar)", lines=1))
    s.append(question("C5. What does PPF NOT cover - interior? glass? wheels?", lines=1))

    # ---- D. ceramic coating -------------------------------------------
    s.append(heading("D. Ceramic coating", "FAQs 25, 31 and 34 are waiting."))
    s.append(priced("D1. Ceramic coating price by car size (starting price, Rs).", ["Ceramic coating"]))
    s.append(question("D2. How many days does it take? And how long does the coating last?",
                      quotes=["kitna time lagega ceramic me?"], lines=1, faq=25))
    s.append(question("D3. In one or two lines: ceramic coating vs PPF - which for whom?",
                      quotes=["ceramic cotng aur ppf me kya diff h"], lines=2, faq=31))
    s.append(question("D4. In one line: teflon coating vs ceramic - do we do teflon at all?",
                      quotes=["teflon coating vs ceramic"], lines=1, faq=34))
    s.append(question("D5. Warranty on ceramic coating, if any.", lines=1))

    # ---- E. denting & painting ----------------------------------------
    s.append(heading("E. Denting and painting", "FAQs 39, 43, 44, 46 and 48 are waiting."))
    s.append(question("E1. Per-panel price for denting + painting (door / bumper / bonnet), starting Rs.",
                      quotes=["denting painting cost for i10"], lines=2, faq=39))
    s.append(priced("E2. Full-body repaint price by car size (starting price, Rs).", ["Full body repaint"]))
    s.append(question("E3. How many days for a single dent? For a full-body job?",
                      quotes=["kitna time me dent theek hoga"], lines=1, faq=44))
    s.append(yes_no("E4. Do we guarantee the colour match?", "(FAQ 46) - any condition? ______________________"))
    s.append(Spacer(1, 4))
    s.append(question("E5. Old-model-to-new-model conversion (e.g. old Fortuner look change) - do we do it? "
                      "Starting price?", quotes=["old fortuner modification cost"], lines=2, faq=48))

    # ---- F. service & mechanical --------------------------------------
    s.append(heading("F. Service and mechanical work", "FAQs 52, 58, 61, 64, 65 and 71 are waiting."))
    s.append(priced("F1. General service price by car size (labour, starting Rs) - and what it includes.", ["General service"], note_box=True))
    s.append(question("F2. AC gas refill - price?", quotes=["ac gas refill price"], lines=1, faq=64))
    s.append(question("F3. Brake pad change - labour, and a typical parts range?",
                      quotes=["brake pad change cost"], lines=1, faq=52))
    s.append(question("F4. Shock absorber replacement - labour, and a typical parts range?",
                      quotes=["shock absorber replace price"], lines=1, faq=71))
    s.append(question("F5. Engine overhaul - do we do it? Starting price?",
                      quotes=["engine overhaul cost"], lines=1, faq=61))
    s.append(question("F6. Catalytic converter cleaning - do we do it? Price?",
                      quotes=["catalytic converter clean price"], lines=1, faq=58))
    s.append(question("F7. Spare parts we can confirm we keep (the bot currently lists timing belts, water "
                      "pumps and clutch plates). Add others - mirrors were asked for this month.", lines=2))

    # ---- G. detailing & accessories -----------------------------------
    s.append(heading("G. Detailing and accessories", "FAQs 30, 32, 82, 85 and 87 are waiting."))
    s.append(question("G1. Polishing price (exterior) by car size.", quotes=["polishing cost"], lines=2, faq=30))
    s.append(question("G2. Full detailing (interior + exterior) price by car size.",
                      quotes=["detailing price for seltos"], lines=2, faq=32))
    s.append(question("G3. Car perfume - price range and brands.", quotes=["car perfume price"], lines=1, faq=82))
    s.append(question("G4. Sun shades - price, and are they car-specific?",
                      quotes=["sun shade price for venue"], lines=1, faq=85))
    s.append(question("G5. Is there any offer or discount on accessories right now? Until when?",
                      why="The bot must never invent an offer, so today it says nothing at all here.",
                      quotes=["discount on accessories?", "koi offer hai kya?"], lines=1, faq=87))
    s.append(Paragraph("G6. Which of these do we stock? Tick, and write a starting price if you can. "
                       "Customers asked for every one of these; the bot cannot confirm any of them yet.", Q))
    s.append(Spacer(1, 3))
    items = ["Armrest", "Android infotainment system", "Mud flaps", "Steering cover", "Dash camera",
             "Tyres", "Roof rails", "Side mirrors / ORVM", "Parcel tray", "Car cover"]
    rows = [[Paragraph("<b>Item</b>", BODY), Paragraph("<b>Stock it?</b>", BODY),
             Paragraph("<b>Starting price (Rs) / note</b>", BODY)]]
    for it in items:
        rows.append([Paragraph(it, BODY), Paragraph("[&nbsp;&nbsp;] Yes&nbsp;&nbsp;[&nbsp;&nbsp;] No", BODY), ""])
    t = Table(rows, colWidths=[60 * mm, 40 * mm, 70 * mm], rowHeights=[8 * mm] + [9 * mm] * len(items))
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, LINE),
                           ("BACKGROUND", (0, 0), (-1, 0), BAND),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 5)]))
    s.append(t)

    # ---- H. the shop ----------------------------------------------------
    s.append(heading("H. About the shop", "FAQs 11, 12, 57 and 92 are waiting."))
    s.append(yes_no("H1. Do we have any other branch?", "(FAQ 11) - where? ______________________________"))
    s.append(Spacer(1, 4))
    s.append(yes_no("H2. Is there parking for customers at Dholai?", "(FAQ 12)"))
    s.append(Spacer(1, 4))
    s.append(question("H3. Pick-up and drop of the car - available? Free or charged? Which areas of Jaipur?",
                      why="Asked in DMs and comments; the bot can only hand it to the team.",
                      quotes=["gaadi pickup aur drop available h?", "pick and drop milega?"], lines=2, faq=57))
    s.append(question("H4. Used cars: what should the bot say when someone asks the price of a specific "
                      "car (e.g. \"used Jeep Compass price\")? Today it says prices depend on current "
                      "stock and invites them to visit. Is that right, or should it ask them to WhatsApp?",
                      lines=2, faq=92))
    s.append(question("H5. Anything the bot should say to customers outside Jaipur (other than mats "
                      "courier): do we do any work for them, or only walk-in?",
                      quotes=["Any one in Navi Mumbai"], lines=2))

    s.append(Spacer(1, 10))
    s.append(Paragraph("Thank you. Return this to the owner; the answers go into the bot the same day.", SMALL))

    doc.build(s, onFirstPage=footer, onLaterPages=footer)
    return OUT


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("written:", build())
