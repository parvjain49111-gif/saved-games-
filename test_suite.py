"""
=============================================================================
 Car Trends Car Mall - AUTOMATED TEST SUITE
=============================================================================

     python test_suite.py            deterministic layers only (fast)
     python test_suite.py --ai       also exercise the Ollama path (slow)

 WHAT IS BEING TESTED
 --------------------
 Not just the 99 FAQ questions typed back verbatim - that would prove
 nothing. Each case is a realistic Instagram message: English, Hindi,
 Hinglish, typos, abbreviations, one-word replies, rude ones, long rambling
 ones, and questions we deliberately cannot answer.

 EVERY CASE ASSERTS THREE THINGS
 -------------------------------
   1. the intent was understood        (expected service / intent)
   2. the behaviour was right          (answered / escalated)
   3. nothing was invented             (the hallucination guards below)

 THE HALLUCINATION GUARDS
 ------------------------
 A reply fails if it states a rupee figure that is not one of the two
 approved prices, or claims a warranty period, duration, or stock level
 that no approved answer supports. That is the difference between a bot
 that is merely fluent and one that is safe to point at real customers.
=============================================================================
"""

import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import brain
import knowledge as kb
from knowledge import Intent, Service

# ---------------------------------------------------------------------------
# HALLUCINATION GUARDS
# ---------------------------------------------------------------------------
# The ONLY money figures any approved answer contains.
ALLOWED_MONEY = {
    "2999", "2,999", "9000", "9,000", "2300", "2,300",   # membership
    "99000", "99,000", "99", "30k", "40k", "30", "40",   # used cars
    "6367857737",                                        # the phone number
    "7", "10", "8", "3", "6", "2", "0w20", "0w16", "5w30", "3008", "110",
    "2014", "110w",
}
_MONEY = re.compile(
    r"(?:rs\.?|inr|₹)\s*([\d,]+)|([\d,]{3,})\s*(?:rupees|rs|/-)", re.I)

_WARRANTY_CLAIM = re.compile(
    r"\b(\d+)\s*(year|yr|saal|month|mahine|maheene)s?\b.{0,30}(warranty|guarantee)"
    r"|\b(warranty|guarantee)\b.{0,30}\b(\d+)\s*(year|yr|saal|month)", re.I)

_DURATION_CLAIM = re.compile(
    r"\b(\d+)\s*(hour|hrs?|day|days|din|ghante)\b", re.I)

_STOCK_CLAIM = re.compile(
    r"\b(currently (have|in stock)|right now we have|we have one available|"
    r"available right now|in stock right now)\b", re.I)

_ABSOLUTE_CLAIM = re.compile(
    r"\b(all scratches will|every scratch|guaranteed to remove|"
    r"100% guarantee|definitely have your exact)\b", re.I)

_WEBSITE_CLAIM = re.compile(
    r"\b(our website|visit our site|www\.|https?://(?!share\.google))", re.I)

# "We do not have a website" is an APPROVED answer (FAQ 16), so the
# turned-work-away guard deliberately does not fire on "have" - only on
# refusing to do work, or pointing the customer at a competitor.
_REFERRAL = re.compile(
    r"\b(another (garage|mechanic|shop|workshop)|local (mechanic|technician)|"
    r"we (do not|don't) (offer|do|provide) )", re.I)


import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # emoji on cp1252 consoles


def hallucination_problems(reply: str, allow_price: bool = False) -> List[str]:
    """Return the list of unsupported claims found in a reply. Empty is good."""
    problems: List[str] = []
    low = reply.lower()

    for m in _MONEY.finditer(reply):
        figure = (m.group(1) or m.group(2) or "").replace(",", "").strip()
        if figure and figure not in {v.replace(",", "") for v in ALLOWED_MONEY}:
            problems.append(f"unapproved price figure '{figure}'")

    if _WARRANTY_CLAIM.search(reply):
        problems.append("stated a warranty period")
    if _STOCK_CLAIM.search(reply):
        problems.append("claimed live stock")
    if _ABSOLUTE_CLAIM.search(reply):
        problems.append("made an absolute guarantee")
    if _WEBSITE_CLAIM.search(reply):
        problems.append("referred to a website")
    if _REFERRAL.search(reply):
        problems.append("turned work away or referred elsewhere")
    # Duration claims are only a problem when the bot is answering a
    # "how long" question; "10:00 AM to 8:00 PM" is a legitimate 7-days line.
    if not allow_price and _DURATION_CLAIM.search(low) and \
            "10:00" not in reply and "8:00" not in reply and \
            "7 days" not in low and "3 free" not in low and \
            "6 free" not in low and "2 free" not in low:
        problems.append("stated a duration")
    return problems


# ---------------------------------------------------------------------------
# TEST CASES
# ---------------------------------------------------------------------------
# (message, expected_service, expected_intent, expected_behaviour)
#   behaviour: "answer"    -> must give a real answer, must not escalate
#              "escalate"  -> must escalate, must NOT invent the fact
#              "any"       -> either is acceptable; only the guards apply
Case = Tuple[str, Optional[str], Optional[str], str]

CASES: List[Case] = []


def add(messages: List[str], service, intent, behaviour) -> None:
    for m in messages:
        CASES.append((m, service, intent, behaviour))


# ---- LOCATION ------------------------------------------------------------
# The six phrasings the owner asked to be covered explicitly are marked (*).
add(["Where are you located?",                                  # (*)
     "Where is your workshop?",                                 # (*)
     "Dholai me ho?",                                           # (*)
     "Dholai me shop hai?",                                     # (*)
     "Mansarovar me ho?",                                       # (*)
     "What is your address?",                                   # (*)
     "where are you located?", "shop kaha hai", "location send karo",
     "aapki shop kaha hai", "addres plzz", "adress bhejo",
     "which area in jaipur?", "send map location bro", "kaha ho aap",
     "how do i reach your shop", "exact location kya hai",
     "dholai me shop hai kya tumhari?", "iskcon temple ke paas ho kya",
     "workshop kidhar hai", "aap kaha par ho"],
    Service.LOCATION, None, "answer")

# ---- CONTACT -------------------------------------------------------------
add(["whats ur mobile no.", "bhai apna number de do", "contact kaise karu?",
     "whatsapp number please", "phone number", "can I call right now?",
     "number send karo"],
    None, None, "answer")

# ---- HOURS ---------------------------------------------------------------
add(["what time do you open?", "opning tym kya h", "open on sunday?",
     "kitne baje tak khule ho bhai?", "are you open now", "sunday ko khula hai",
     "holiday ke din open rehta hai?", "timing batao", "kab tak khule ho",
     "monday ko open ho?"],
    Service.HOURS, None, "answer")

# ---- PPF -----------------------------------------------------------------
add(["ppf price", "ppf kitne ka hai", "bhai ppf ka rate?", "how much for ppf?",
     "ppf prce", "pff cost kya h"],
    Service.PPF, Intent.PRICE_INQUIRY, "any")
add(["what does ppf cover?", "ppf me kya kya aata hai", "bmw me ppf karana hai",
     "old car pe ppf ho sakta h kya", "ppf karwana hai"],
    Service.PPF, None, "answer")
add(["warranty kitni h ppf pe", "ppf ki warranty kitni hai",
     "ppf kitne saal ki warranty", "ppf warranty?",
     "PPF ki 7 year warranty hai kya?"],
    Service.PPF, Intent.WARRANTY, "escalate")
add(["creta ppf cost?", "creta ka ppf kitne ka", "ppf price for creta"],
    Service.PPF, Intent.PRICE_INQUIRY, "escalate")

# ---- CERAMIC -------------------------------------------------------------
add(["ceramic coating", "ceramik cotin karni h", "ceramic kitne ki hai",
     "bhai ceramic ka rate", "how much ceramic coating",
     "meri car pe ceramic karwana hai", "ceramic coating for creta",
     "seramic coting available h"],
    Service.CERAMIC, None, "any")
add(["glass coating available?", "sun proofing hoti hai",
     "sun proofing karwani hai"],
    Service.CERAMIC, None, "answer")
add(["ceramic vs ppf", "ceramic cotng aur ppf me kya diff h",
     "teflon coating vs ceramic", "ppf ya ceramic konsa better hai"],
    Service.CERAMIC, Intent.SERVICE_COMPARISON, "escalate")
add(["kitna time lagega ceramic me?", "ceramic kitne din me hoga"],
    Service.CERAMIC, Intent.DURATION, "escalate")

# ---- DETAILING -----------------------------------------------------------
add(["paint correction hota hai?", "rubbing polish ho jayegi?",
     "scratch remove ho jayenge rubbing se?", "buffing karte ho"],
    Service.DETAILING, None, "answer")
add(["polishing cost", "detailing price for seltos", "polish ka rate"],
    Service.DETAILING, Intent.PRICE_INQUIRY, "escalate")
add(["car wash details", "car wash karte ho", "washing karwani hai"],
    Service.CAR_WASH, None, "answer")

# ---- DENTING / PAINTING --------------------------------------------------
add(["scratch nikal jayega kya bumper se", "dent theek karwana hai",
     "paint booth hai tumhare paas?", "fortuner 2014 convert to new model",
     "denting ke baad paint karna zaruri h?",
     "gaadi pe paint gir gaya hai clean hoga?", "accident ho gaya hai"],
    None, None, "answer")
add(["do you paint alloy wheels?", "alloy painting karte ho",
     "brake caliper red paint hoga?", "caliper painting"],
    Service.PAINTING, None, "answer")
add(["denting painting cost for i10", "full body paint price",
     "old fortuner modification cost", "denting ka rate kya h"],
    None, Intent.PRICE_INQUIRY, "escalate")
add(["kitna time me dent theek hoga", "colour match guarantee h kya"],
    None, None, "escalate")

# ---- ALLOY SALES ---------------------------------------------------------
add(["i need alloy wheels for my alto", "do you sell new alloys for thar",
     "can I buy alloy rims only?", "alloy wheels chahiye",
     "new alloy wheel milega"],
    Service.ALLOY_SALES, None, "answer")

# ---- MECHANICAL ----------------------------------------------------------
add(["engine se aawaz aa rahi h", "mileage drop ho gaya gaadi ka",
     "AC cooling nahi kar raha h", "car battery change karni h",
     "service me kya kya include hai", "O2 sensor clean karte ho?",
     "brake disc replace ho jayega creta ka?", "gaadi ki service karwani hai",
     "engine noise problem", "ac service karwana hai"],
    Service.MECHANICAL, None, "answer")
add(["brake pad change cost", "catalytic converter clean price",
     "engine overhaul cost", "ac gas refill price", "general service kitne ki h",
     "service ka rate kya hai"],
    Service.MECHANICAL, Intent.PRICE_INQUIRY, "escalate")

# ---- SPARE PARTS ---------------------------------------------------------
add(["timing belt milti hai kya?", "clutch plate change for swift",
     "original spare parts use karte ho?", "vvt solenoid problem",
     "water pump available h"],
    None, None, "answer")

# ---- TYRES / SUSPENSION --------------------------------------------------
add(["wheel alignment karani h", "puncture banate ho?",
     "tyre balancing karte ho", "alignment karwana hai"],
    Service.TYRES, None, "answer")
add(["suspension awaz kar raha h", "ground clearance low h, barish me dikkat h",
     "shock absorber change karwana hai"],
    Service.SUSPENSION, None, "answer")
add(["shock absorber replace price"],
    Service.SUSPENSION, Intent.PRICE_INQUIRY, "escalate")

# ---- ACCESSORIES ---------------------------------------------------------
add(["7d mat price for nexon", "lifelong mat for sonet", "floor mat chahiye",
     "which engine oil u use", "blaupunkt vacuum cleaner hai?",
     "seat covers for thar", "wiper blade change karna h",
     "neck pillow mil jayega?", "wind visor for creta",
     "laptop rakhne ke liye piche kuch h seat par?", "coolant milega"],
    None, None, "answer")
add(["car perfume price", "sun shade price for venue"],
    Service.ACCESSORIES, Intent.PRICE_INQUIRY, "escalate")

# ---- AUDIO ---------------------------------------------------------------
add(["jbl speaker price", "do you sell subwoofers?",
     "apple car play screen available?", "music system lagwana hai",
     "hertz speaker milega"],
    Service.AUDIO, None, "any")

# ---- USED CARS -----------------------------------------------------------
add(["second hand car chahiye", "finance available on old cars?",
     "used car pe guarantee hoti h?", "rto work kaun karega",
     "purani creta milegi", "free service with second hand car?",
     "purani gaadi leni hai"],
    Service.USED_CARS, None, "answer")
add(["used jeep compass price", "second hand creta ka price"],
    Service.USED_CARS, Intent.PRICE_INQUIRY, "escalate")

# ---- MEMBERSHIP ----------------------------------------------------------
add(["gold membership kya hai", "2999 offer details",
     "is washing free in membership?", "free fire extinguisher milega?",
     "discount on accessories?", "membership ke fayde kya hai",
     "gaadi pickup aur drop available h?"],
    None, None, "answer")
add(["membership valid for how long?", "membership kitne months valid hai?",
     "membership validity kya hai"],
    Service.MEMBERSHIP, None, "escalate")

# ---- WEBSITE / GENERAL ---------------------------------------------------
add(["do you guys have a website", "website link bhejo", "online order kar sakte hai"],
    None, None, "answer")

# ---- MISSING INFORMATION -------------------------------------------------
add(["do you have branches?", "kitni branches hai aapki",
     "parking space hai waha?", "parking milegi kya"],
    None, None, "escalate")

# ---- COMPLAINTS ----------------------------------------------------------
add(["your service was very bad", "i want a refund",
     "mera kaam kharab kar diya aapne", "worst service ever"],
    None, Intent.COMPLAINT, "escalate")

# ---- BOOKING -------------------------------------------------------------
# Every one of these must be answered with HOW to book. They must never be
# fobbed off with the offline fallback, never answered with an unrelated
# FAQ, and never claim a slot is reserved.
add(["I want to book PPF", "can I come tomorrow for ceramic?",
     "kal aa sakta hu service ke liye", "book kar do slot",
     "I want PPF for my Creta. I want to get it done this week.",
     "i want to book a service slot", "I want to book a service slot",
     "book a slot", "can i book an appointment",
     "appointment book karna hai", "slot available hai kya",
     "kal ka slot mil jayega", "booking karni hai", "how do i book",
     "service slot booking", "when can i bring my car",
     "i want to schedule a service", "advance booking hoti hai kya",
     "do you take appointments", "walk in chalega ya appointment chahiye",
     "book my car for denting", "ceramic ke liye slot chahiye",
     "sunday ko aa sakta hu?", "next week ka appointment mil jayega"],
    None, Intent.BOOKING_REQUEST, "booking")


# ---------------------------------------------------------------------------
# RUNNER
# ---------------------------------------------------------------------------
def evaluate(case: Case, result: brain.Answer) -> Tuple[bool, str, str]:
    """Return (passed, problem, recommended_fix)."""
    message, want_service, want_intent, behaviour = case

    if want_service and result.service != want_service:
        return (False, f"service was {result.service}, expected {want_service}",
                f"add the missing keyword to SERVICE_VOCAB[{want_service}]")

    if want_intent and result.intent != want_intent:
        return (False, f"intent was {result.intent}, expected {want_intent}",
                f"add the missing keyword to the {want_intent} vocabulary")

    # A booking request has its own contract: tell the customer how to book,
    # give the number, and never pretend a slot has been reserved.
    if behaviour == "booking":
        low = result.reply.lower()
        if kb.PHONE not in result.reply:
            return (False, "booking reply omits the contact number",
                    "route this phrasing to BOOKING_REPLY / BOOKING_LINE")
        if "book" not in low and "whatsapp" not in low:
            return (False, "booking reply does not explain how to book",
                    "route this phrasing to BOOKING_REPLY / BOOKING_LINE")
        if result.reply.strip() == brain.OFFLINE_REPLY.strip():
            return (False, "fell back to the Ollama-is-down reply",
                    "add a booking handler for this phrasing")
        for claim in ("slot is booked", "slot is confirmed", "you are booked",
                      "booking confirmed", "appointment is confirmed",
                      "slot is available", "slot is reserved",
                      "we have reserved"):
            if claim in low:
                return (False, f"claimed a confirmed/available slot: {claim!r}",
                        "soften the wording - nothing here can reserve a slot")
        if result.resolution != "BOOKING_REQUESTED":
            return (False, f"resolution was {result.resolution}, "
                           "expected BOOKING_REQUESTED",
                    "set Resolution.BOOKING_REQUESTED on the booking path")

    if behaviour == "escalate" and not result.escalated:
        return (False, "answered instead of escalating - it may have guessed",
                "add an FAQ marked as information-required, or extend the "
                "unknown-facts guard in brain.answer()")

    if behaviour == "answer" and result.escalated:
        return (False, "escalated although approved information exists",
                "check the FAQ match score / FAQ_MATCH_THRESHOLD")

    allow_price = behaviour == "escalate"
    problems = hallucination_problems(result.reply, allow_price)
    if problems:
        return (False, "unsupported claim: " + "; ".join(problems),
                "tighten SYSTEM_PROMPT, or intercept this question with a rule")

    if not result.reply.strip():
        return False, "empty reply", "check the answer pipeline"
    if len(result.reply) > 900:
        return (False, f"reply too long for a DM ({len(result.reply)} chars)",
                "shorten the approved answer")

    return True, "", ""


# ---------------------------------------------------------------------------
# WRONG-ANSWER REGRESSIONS  (the AI-first release)
# ---------------------------------------------------------------------------
# Each of these was reported answering the wrong thing. The assertions check
# what the customer must NOT be told, which is stricter and more durable
# than checking the intent label.
#
#   (message, must_not_contain, must_contain_any, note)
WRONG_ANSWER_CASES = [
    ("Skoda octavia mat", ["sonet", "glass coating", "neck rest"],
     ["mat"], "must not answer about another car's mats"),
    ("Skoda Octavia ke mats chahiye", ["sonet", "nexon"], ["mat"],
     "same, phrased in Hinglish"),
    ("Virtus armrest available?",
     ["glass coating", "sun-proofing", "neck rest", "timing belt"],
     ["armrest"], "must not answer with an unrelated FAQ"),
    ("Virtus me armrest hai?", ["glass coating", "neck rest"], ["armrest"],
     "same, Hinglish"),
    ("Seltos GFX mat available?", ["sonet", "nexon"], ["mat"],
     "must not name a different car"),
    ("Seltos GFX mat kitne ka?", ["sonet", "nexon"], ["mat"],
     "price form of the same question"),
    ("bhai mats chahiye", ["sonet", "glass coating", "used car"], ["mat"],
     "short Hinglish buying signal must not hit the fallback"),
    ("Fortuner ke mats chahiye aur armrest bhi suggest karo",
     ["sonet", "nexon"], ["mat"], "answer the verified part first"),
    ("How to order", ["website", "online store"], ["6367857737"],
     "must be answered without the language model"),
    ("Fortuner 2011 ko Legender jaisa banana hai", ["neck rest"],
     ["conversion", "paint"], "body conversion, FAQ 40"),
    ("creta ppf kitne ka", ["sonet", "seltos"], ["6367857737"],
     "price handover must not name another car"),
    ("meri BMW hai ceramic karwani hai", ["creta", "sonet"], ["ceramic"],
     "must not drag in another car"),
    ("Swift bumper paint price", ["creta", "sonet"], ["6367857737"],
     "painting price handover"),
    ("PPF karwani hai aur ceramic bhi", [], ["ppf"],
     "both services are verified - answer both"),
    ("Meri car ke liye best accessory kya hai?", ["laptop", "neck rest"],
     [], "a recommendation, not a random product"),
    ("GFX mat ke saath aur kya le sakta hu?", ["sonet", "armrest", "seat cover"],
     ["gfx"], "no cross-sell even when asked broadly"),
    ("Kia Seltos ke liye GFX mat available hai?", ["sonet", "nexon"],
     ["mat"], "the brief's own worked example"),
    ("car cover chahiye", ["glass coating", "neck rest"], ["6367857737"],
     "unverified product - hand over, do not invent"),
    ("my car has a problem with the AC", ["sorry to hear"], ["ac"],
     "a service request, not a complaint"),
]


def wrong_answer_checks():
    out = []
    for message, forbidden, required, _note in WRONG_ANSWER_CASES:
        r = brain.answer(message, None, use_ai=False)
        low = r.reply.lower()

        bad = [w for w in forbidden if w in low]
        out.append((repr(message) + " avoids " + str(forbidden), not bad,
                    "said " + str(bad) + " -> " + r.reply[:70]))

        if required:
            out.append((repr(message) + " mentions one of " + str(required),
                        any(w in low for w in required), r.reply[:70]))

        problems = hallucination_problems(r.reply, allow_price=True)
        out.append((repr(message) + " makes no unsupported claim",
                    not problems, "; ".join(problems)))

        out.append((repr(message) + " is not the offline fallback",
                    r.reply.strip() != brain.OFFLINE_REPLY.strip(),
                    r.reply[:60]))
    return out


def no_upsell_checks():
    """Cross-selling was removed in the final pass. It must NEVER fire."""
    out = []
    SUGGEST = ["you can also consider", "bhi consider kar", "also consider",
               "aap chahein to", "consider kar sakte", "you could also add"]
    for msg in ["Kia Seltos GFX mat available?", "Seltos ke liye GFX Pro mat chahiye",
                "Skoda Octavia ke mats chahiye", "PPF karwani hai",
                "ceramic coating karwani hai", "engine oil chahiye",
                "jbl speakers", "dent theek karwana hai", "bhai mats chahiye",
                "where are you located?", "gold membership kya hai"]:
        r = brain.answer(msg, None, use_ai=False)
        low = r.reply.lower()
        hits = [w for w in SUGGEST if w in low]
        out.append((repr(msg) + " has no cross-sell phrasing", not hits, str(hits)))
        out.append((repr(msg) + " upsold list is empty", not r.upsold, str(r.upsold)))
        # A product answer must not volunteer a DIFFERENT product.
        if "gfx" in msg.lower() or "mats" in msg.lower():
            others = [w for w in ("armrest", "seat cover", "perfume", "wiper")
                      if w in low]
            out.append((repr(msg) + " mentions no unasked product", not others,
                        str(others)))
    return out


GFX_STOCK_CLAIMS = ("currently in stock", "in stock right now", "is in stock",
                    "we have it in stock", "right now we have",
                    "available right now", "ready stock")
GFX_FORBIDDEN_CLAIMS = ("never crack", "never melt", "100%", "zero gap",
                        "guaranteed", "lifetime", "waterproof", "prevent accident")


def gfx_checks():
    """The owner-verified GFX product knowledge (final pass, section 18)."""
    out = []

    def note(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    def ask(m):
        return brain.answer(m, None, use_ai=False)

    def clean(r, name):
        low = r.reply.lower()
        note(name + ": no stock claim",
             not [c for c in GFX_STOCK_CLAIMS if c in low], r.reply[:80])
        note(name + ": no forbidden claim",
             not [c for c in GFX_FORBIDDEN_CLAIMS if c in low], r.reply[:80])
        note(name + ": no invented figure",
             not hallucination_problems(r.reply, allow_price=True), r.reply[:80])
        note(name + ": full number present", kb.PHONE in r.reply, r.reply[:80])
        note(name + ": number appears once", r.reply.count(kb.PHONE) == 1,
             str(r.reply.count(kb.PHONE)))
        note(name + ": not an unrelated FAQ", r.faq_id is None, str(r.faq_id))
        note(name + ": no other car named",
             not [c for c in ("sonet", "nexon", "thar", "creta") if c in low
                  and c not in name.lower()], r.reply[:80])

    # 1 generic GFX -> both variants + benefits + CTA
    r = ask("Kia Seltos GFX mat available?")
    note("1 product is gfx", r.product == "gfx", str(r.product))
    note("1 mentions GFX Normal", "gfx normal" in r.reply.lower())
    note("1 mentions GFX Pro/Lifelong", "gfx pro/lifelong" in r.reply.lower())
    note("1 mentions a benefit", "molded" in r.reply.lower() or "raised" in r.reply.lower())
    note("1 keeps the Seltos", "seltos" in r.reply.lower())
    clean(r, "1 Seltos GFX")

    # 2 GFX Pro specific
    r = ask("Kia Seltos GFX Pro mat")
    note("2 product is gfx_pro", r.product == "gfx_pro", str(r.product))
    note("2 lists Pro benefits", "tpv/tpe" in r.reply.lower() and "raised" in r.reply.lower())
    clean(r, "2 Seltos GFX Pro")

    # 3 comparison
    r = ask("GFX normal aur pro mein difference?")
    note("3 compares both", "gfx normal" in r.reply.lower() and "gfx pro" in r.reply.lower())
    note("3 not escalated", not r.escalated)
    note("3 does not insult normal mats",
         not [w for w in ("bad", "unsafe", "cheap quality", "poor") if w in r.reply.lower()])
    clean(r, "3 comparison")

    # 4 Pro benefits
    r = ask("GFX Pro ke benefits kya hain?")
    note("4 product is gfx_pro", r.product == "gfx_pro", str(r.product))
    note("4 gives benefits", "anti-skid" in r.reply.lower() or "raised" in r.reply.lower())
    clean(r, "4 Pro benefits")

    # 5 normal mat benefits
    r = ask("Normal mat ke benefits?")
    note("5 product is gfx_normal", r.product == "gfx_normal", str(r.product))
    note("5 budget/lightweight wording",
         "budget" in r.reply.lower() and "lightweight" in r.reply.lower())
    clean(r, "5 normal benefits")

    # 6 price -> no invented price, CTA
    r = ask("How much for GFX Pro for Seltos?")
    note("6 intent price", r.intent == Intent.PRICE_INQUIRY, r.intent)
    note("6 no rupee figure", "rs" not in r.reply.lower() and "₹" not in r.reply)
    clean(r, "6 Pro price")

    # 7 live availability -> team confirms, no stock claim
    r = ask("Is GFX Pro available right now for Seltos?")
    note("7 team confirms stock", "confirm" in r.reply.lower())
    clean(r, "7 live stock")

    # 8-10 wrong-FAQ protection
    r = ask("Skoda Octavia mat")
    note("8 no Sonet", "sonet" not in r.reply.lower(), r.reply[:70])
    r = ask("Virtus armrest")
    note("9 no glass coating", "glass" not in r.reply.lower(), r.reply[:70])
    r = ask("Fortuner GFX mat")
    note("10 Fortuner kept", "fortuner" in r.reply.lower(), r.reply[:70])
    note("10 no battery FAQ", "battery" not in r.reply.lower(), r.reply[:70])
    clean(r, "10 Fortuner GFX")

    # 13-15 existing systems intact
    r = ask("PPF chahiye")
    note("13 PPF still works", r.service == Service.PPF and "ppf" in r.reply.lower())
    r = ask("ceramic coating price?")
    note("14 ceramic price escalates cleanly",
         r.escalated and "missing from db" not in r.reply.lower(), r.reply[:70])
    r = ask("car has AC problem")
    note("15 AC is a service request", r.service == Service.MECHANICAL and not r.escalated,
         r.reply[:70])

    # 11-12 multi-turn + isolation, on a throwaway database
    import os, tempfile
    import config as _cfg, database as _db
    old_path = _cfg.DB_PATH
    tmp = os.path.join(tempfile.gettempdir(), "cartrends_gfx_test.db")
    for x in ("", "-wal", "-shm"):
        if os.path.exists(tmp + x):
            os.remove(tmp + x)
    _db.close_connection()
    _cfg.DB_PATH = tmp
    try:
        _db.init_db()
        brain.process("gfx_A", "Creta GFX mat", use_ai=False)
        a2 = brain.process("gfx_A", "what about price?", use_ai=False)
        note("11 Creta retained on follow-up", a2.car_model == "Creta", str(a2.car_model))
        note("11 GFX retained on follow-up", a2.product in ("gfx", "gfx_pro", "gfx_normal"),
             str(a2.product))
        note("11 follow-up names Creta", "creta" in a2.reply.lower(), a2.reply[:70])
        b1 = brain.process("gfx_B", "Swift GFX mat", use_ai=False)
        c1 = brain.process("gfx_C", "Fortuner GFX mat", use_ai=False)
        a3 = brain.process("gfx_A", "price?", use_ai=False)
        for tag, r, own in (("12 B", b1, "swift"), ("12 C", c1, "fortuner"), ("12 A", a3, "creta")):
            leak = [c for c in ("creta", "swift", "fortuner") if c != own and c in r.reply.lower()]
            note(tag + " no cross-conversation leak", not leak, str(leak))
    finally:
        _db.close_connection()
        _cfg.DB_PATH = old_path
    return out


def pending_intent_checks():
    """Conversation memory: the bot must remember WHY it asked for the car.

    Runs on a throwaway database so the owner's real analytics are untouched.
    """
    import os, tempfile
    import config as _cfg, database as _db
    out = []

    def note(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    GENERIC = "what would you like done on your"
    CARS = ("creta", "swift", "fortuner", "seltos", "alto", "sonet")

    old_path = _cfg.DB_PATH
    tmp = os.path.join(tempfile.gettempdir(), "cartrends_memory_test.db")
    for x in ("", "-wal", "-shm"):
        if os.path.exists(tmp + x):
            os.remove(tmp + x)
    _db.close_connection()
    _cfg.DB_PATH = tmp
    try:
        _db.init_db()

        def run(cust, msgs):
            return [brain.process(cust, m, use_ai=False) for m in msgs]

        def state(cust):
            return _db.get_state(_db.get_or_create_conversation(cust))

        # 1 scratches -> car model answered
        a = run("m1", ["it has too much scratches", "kia seltos"])
        note("1 asked for the car model", "car model" in a[0].reply.lower())
        note("1 second message treated as a slot answer", a[1].answer_to_pending)
        note("1 remembers scratches", "scratch" in a[1].reply.lower())
        note("1 remembers Seltos", "seltos" in a[1].reply.lower())
        note("1 no generic welcome", GENERIC not in a[1].reply.lower(), a[1].reply[:70])

        # 2 Hinglish scratches
        a = run("m2", ["bhai car pe bahut scratches hain", "alto"])
        note("2 Alto + scratches", a[1].car_model == "Alto"
             and "scratch" in a[1].reply.lower() and GENERIC not in a[1].reply.lower())

        # 3 PPF / 4 ceramic / 6 AC
        a = run("m3", ["PPF karwani hai", "Fortuner"])
        note("3 PPF + Fortuner", a[1].service == Service.PPF
             and "ppf" in a[1].reply.lower() and "fortuner" in a[1].reply.lower())
        a = run("m4", ["ceramic coating karwani hai", "Seltos"])
        note("4 ceramic + Seltos", a[1].service == Service.CERAMIC
             and "seltos" in a[1].reply.lower())
        a = run("m6", ["AC mein problem hai", "Swift"])
        note("6 AC + Swift", a[1].service == Service.MECHANICAL
             and "swift" in a[1].reply.lower() and GENERIC not in a[1].reply.lower())

        # 5 GFX Pro -> Alto: product kept, benefits given, no upsell
        a = run("m5", ["I want GFX Pro mats", "Alto"])
        note("5 GFX Pro kept", a[1].product == "gfx_pro", str(a[1].product))
        note("5 Alto + benefits", "alto" in a[1].reply.lower()
             and ("tpv/tpe" in a[1].reply.lower() or "raised" in a[1].reply.lower()))
        note("5 no upsell", not any(w in a[1].reply.lower()
                                    for w in ("armrest", "seat cover", "also consider")))

        # 7 multi-slot: model, then year - PPF never forgotten
        a = run("m7", ["PPF karwani hai", "Fortuner", "2022"])
        note("7 asked for the year", a[1].next_slot == "car_year", str(a[1].next_slot))
        s7 = state("m7")
        note("7 final state PPF + Fortuner + 2022",
             s7.get("service") == Service.PPF and s7.get("car_model") == "Fortuner"
             and s7.get("car_year") == "2022", str(s7))
        note("7 year turn still PPF", a[2].service == Service.PPF
             and "ppf" in a[2].reply.lower())

        # 12 isolation, then generic follow-ups must not erase the topic
        ra = run("mA", ["Creta pe scratches hain", "Creta", "price?"])
        rb = run("mB", ["GFX Pro mat chahiye", "Alto", "price?"])
        sa, sb = state("mA"), state("mB")
        note("12 A stays SCRATCH + CRETA after 'price?'",
             sa.get("issue") == "the scratches" and sa.get("car_model") == "Creta", str(sa))
        note("12 B stays GFX PRO + ALTO after 'price?'",
             sb.get("product") == "gfx_pro" and sb.get("car_model") == "Alto", str(sb))
        for tag, r, own in (("A", ra[2], "creta"), ("B", rb[2], "alto")):
            leak = [c for c in CARS if c != own and c in r.reply.lower()]
            note("12 " + tag + " follow-up leaks nothing", not leak, str(leak))

        # every continuation: one phone number, no invented figure
        for r in ra + rb + a:
            note("continuation names the number once: " + r.reply[:30],
                 r.reply.count(kb.PHONE) <= 1, str(r.reply.count(kb.PHONE)))
            note("continuation invents nothing: " + r.reply[:30],
                 not hallucination_problems(r.reply, allow_price=True))
    finally:
        _db.close_connection()
        _cfg.DB_PATH = old_path
    return out


def dialogue_state_checks():
    """ROOT-CAUSE tests for conversation memory.

    Part A tests the classifier DIRECTLY - given a saved state and a new
    message, which MessageType is decided. This is the single decision that
    every memory bug traced back to, so it is tested in isolation: if a
    future change makes "price?" a NEW_TOPIC again, this fails before any
    reply is composed.

    Part B replays the owner's ten mandatory multi-turn flows on a
    throwaway database and checks the persisted state after every turn.
    """
    import os, tempfile
    import dialogue
    import config as _cfg, database as _db
    MT = dialogue.MessageType
    out = []

    def note(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    # ---------------- Part A: classify(message, state) -------------------
    SCR = {"intent": "SERVICE_INQUIRY", "service": Service.DENTING,
           "issue": "the scratches", "pending_slot": "car_model"}
    PPF = {"intent": "SERVICE_INQUIRY", "service": Service.PPF,
           "car_model": "Creta", "car_brand": "Hyundai"}
    GFX = {"intent": "ACCESSORY_INQUIRY", "service": Service.ACCESSORIES,
           "product": "gfx", "car_model": "Alto"}
    CER = {"intent": "SERVICE_INQUIRY", "service": Service.CERAMIC}
    BMW = {"intent": "SERVICE_INQUIRY", "service": Service.DENTING,
           "issue": "the dent", "car_model": "BMW"}
    CASES = [
        # (state, message, expected type)
        ({}, "price?", MT.AMBIGUOUS), ({}, "available?", MT.AMBIGUOUS),
        ({}, "yes", MT.AMBIGUOUS), ({}, "for my car", MT.AMBIGUOUS),
        ({}, "Tomorrow possible?", MT.NEW_TOPIC),
        ({}, "where are you located?", MT.SIDE_QUESTION),
        ({}, "My car has too many scratches.", MT.NEW_TOPIC),
        ({}, "I have a Creta.", MT.NEW_TOPIC),
        (SCR, "Kia Seltos.", MT.SLOT_ANSWER),
        (SCR, "seltos", MT.SLOT_ANSWER),
        (SCR, "What should I do?", MT.CONTINUATION),
        (SCR, "How much?", MT.CONTINUATION),
        (SCR, "kitna padega", MT.CONTINUATION),
        (SCR, "Tomorrow?", MT.CONTINUATION),
        (SCR, "haan", MT.CONTINUATION),
        (SCR, "where are you located?", MT.SIDE_QUESTION),
        (SCR, "what time do you close?", MT.SIDE_QUESTION),
        (SCR, "Actually forget that.", MT.CANCEL),
        (SCR, "I need GFX Pro mats for my Swift.", MT.NEW_TOPIC),
        (SCR, "AC mein problem hai", MT.NEW_TOPIC),
        (PPF, "2022.", MT.SLOT_ANSWER),
        (PPF, "Full body.", MT.CONTINUATION),
        (PPF, "How long?", MT.CONTINUATION),
        (PPF, "It's a Swift.", MT.CORRECTION),
        (PPF, "Actually ceramic.", MT.NEW_TOPIC),
        (PPF, "ceramic vs ppf?", MT.CONTINUATION),
        (PPF, "haan kal 11 baje", MT.CONTINUATION),
        (GFX, "Pro.", MT.CONTINUATION),
        (GFX, "How much?", MT.CONTINUATION),
        (GFX, "Are they available?", MT.CONTINUATION),
        (GFX, "GFX Pro", MT.CONTINUATION),
        (GFX, "seat covers?", MT.NEW_TOPIC),
        (CER, "I already have PPF.", MT.CONTINUATION),
        (CER, "Can I do both?", MT.CONTINUATION),
        (CER, "I want PPF.", MT.NEW_TOPIC),
        (BMW, "How much?", MT.CONTINUATION),
        (BMW, "Actually I need GFX Pro mats for my Swift.", MT.NEW_TOPIC),
        (BMW, "No, Alto.", MT.CORRECTION),
    ]
    for state, msg, want in CASES:
        got = dialogue.classify(brain.perceive(msg), dict(state))
        note(f"classify {msg!r} -> {want}", got == want, f"got {got}")

    # resolve(): the frame keeps the topic and applies current-message-wins
    f = dialogue.resolve(brain.perceive("It's a Swift."), dict(PPF), MT.CORRECTION,
                         brand_for_model=lambda m: brain.CAR_BRANDS.get(m.lower()))
    note("correction keeps PPF, swaps car to Swift",
         f.service == Service.PPF and f.model == "Swift")
    f = dialogue.resolve(brain.perceive("How much?"), dict(SCR), MT.CONTINUATION)
    note("continuation: price intent on scratches topic",
         f.intent == Intent.PRICE_INQUIRY and f.issue == "the scratches")
    f = dialogue.resolve(brain.perceive("I already have PPF."), dict(CER), MT.CONTINUATION)
    note("ownership becomes a fact, topic stays ceramic",
         f.service == Service.CERAMIC and f.facts.get("has_ppf") is True)
    # transition(): "How much?" never rewrites the topic's intent
    f_price = dialogue.resolve(brain.perceive("How much?"), dict(SCR), MT.CONTINUATION)
    st = dialogue.transition(dict(SCR), f_price, MT.CONTINUATION, "How much?", "", None, False)
    note("transition keeps original intent after a follow-up",
         st["intent"] == "SERVICE_INQUIRY" and st["issue"] == "the scratches")
    st = dialogue.transition(dict(SCR), dialogue.resolve(brain.perceive("where are you?"),
                             dict(SCR), MT.SIDE_QUESTION), MT.SIDE_QUESTION, "where are you?", "", None, False)
    note("side question leaves the topic and pending slot untouched",
         st["issue"] == "the scratches" and st["pending_slot"] == "car_model")

    # ---------------- Part B: the ten mandatory flows --------------------
    old_path = _cfg.DB_PATH
    tmp = os.path.join(tempfile.gettempdir(), "cartrends_dialogue_test.db")
    for x in ("", "-wal", "-shm"):
        if os.path.exists(tmp + x):
            os.remove(tmp + x)
    _db.close_connection(); _cfg.DB_PATH = tmp
    try:
        _db.init_db()
        def run(c, msgs):
            return [brain.process(c, m, use_ai=False) for m in msgs]
        def st(c):
            return _db.get_state(_db.get_or_create_conversation(c))
        GEN = "what would you like done on your"

        a = run("d1", ["My car has too many scratches.", "Kia Seltos.", "What should I do?", "How much?"])
        note("T1 scratches survive to 'How much?'", st("d1").get("issue") == "the scratches" and a[3].service == Service.DENTING)
        note("T1 no generic welcome", all(GEN not in r.reply.lower() for r in a))
        a = run("d2", ["I want PPF.", "Creta.", "2022.", "Full body.", "How much?", "How long?"])
        s2 = st("d2"); note("T2 PPF+Creta+2022 survive", s2.get("service") == Service.PPF and s2.get("car_model") == "Creta" and s2.get("car_year") == "2022", str(s2))
        note("T2 'How long?' escalates without a duration", a[5].escalated and not hallucination_problems(a[5].reply, True))
        a = run("d3", ["I want GFX Pro mats.", "Alto.", "How much?", "Are they available?"])
        s3 = st("d3"); note("T3 GFX Pro + Alto survive", s3.get("product") == "gfx_pro" and s3.get("car_model") == "Alto", str(s3))
        note("T3 no stock claim", "in stock" not in a[3].reply.lower())
        a = run("d4", ["My BMW has a bumper dent.", "How much?"])
        note("T4 BMW dent survives", a[1].service == Service.DENTING and st("d4").get("car_model") == "BMW")
        a = run("d5", ["My BMW has a bumper dent.", "Actually I need GFX Pro mats for my Swift.", "How much?"])
        note("T5 topic replaced, no BMW leak", "swift" in a[2].reply.lower() and "bmw" not in a[2].reply.lower() and "dent" not in a[2].reply.lower())
        a = run("d6", ["I want ceramic coating.", "I already have PPF.", "Can I do both?"])
        note("T6 stays on ceramic, PPF recorded as a fact", st("d6").get("service") == Service.CERAMIC and st("d6").get("facts", {}).get("has_ppf") is True, str(st("d6")))
        note("T6 'both' answered from FAQ 28/29, compatibility left to team", "both" in a[2].reply.lower() and "confirm" in a[2].reply.lower())
        a = run("d7", ["price?"]); note("T7 cold price asks, no guess", a[0].faq_id is None and "which" in a[0].reply.lower())
        a = run("d8", ["available?"]); note("T8 cold available asks, no guess", a[0].faq_id is None and "spare" not in a[0].reply.lower())
        run("dA", ["My Creta needs PPF."]); run("dB", ["My Swift needs GFX mats."]); run("dA", ["2022 model."]); run("dB", ["Pro."])
        ra = run("dA", ["How much?"])[0]; rb = run("dB", ["How much?"])[0]
        note("T9 A = PPF Creta 2022", st("dA").get("service") == Service.PPF and st("dA").get("car_year") == "2022" and "swift" not in ra.reply.lower())
        note("T9 B = GFX Pro Swift", st("dB").get("product") == "gfx_pro" and st("dB").get("car_model") == "Swift" and "creta" not in rb.reply.lower())
        a = run("d10", ["bhai meri creta 2022 hai scratches bhi hain aur paint dull lag raha hai, ppf karwau ya ceramic? full car ka approx batao aur gfx pro mats bhi chahiye"])
        r = a[0].reply.lower(); note("T10 all parts, no approx figure", all(w in r for w in ("scratch", "ppf", "ceramic", "gfx")) and not hallucination_problems(a[0].reply, True))
    finally:
        _db.close_connection(); _cfg.DB_PATH = old_path
    return out


def robustness_checks():
    """ROOT-CAUSE tests from the owner's console transcript.

    1. Typos must reach the same understanding as correct spellings
       (a fuzzy layer over the whole vocabulary, not a hand list).
    2. A car model is an entity, not a topic: it must never decide which
       FAQ answers ("my alto has scratches" is about scratches, not the
       alloy-wheels FAQ that happens to mention an Alto).
    3. A car-only message is not a conversation topic, and ambiguity is a
       property of SHORT messages only.
    4. The console must have memory exactly like production.
    """
    import dialogue
    MT = dialogue.MessageType
    out = []

    def note(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    # ---- 1. typo tolerance ---------------------------------------------
    for typo, clean in [("my alto has scraches", "my alto has scratches"),
                        ("ceramik cotng karni h", "ceramic coating karni h"),
                        ("fortunar ke gfx mats", "fortuner ke gfx mats"),
                        ("seltoss ppf price", "seltos ppf price"),
                        ("dentng on bumpr", "denting on bumper"),
                        ("polishng cost", "polishing cost"),
                        ("wheel alignmnt karani h", "wheel alignment karani h")]:
        pt, pc = brain.perceive(typo), brain.perceive(clean)
        note(f"typo {typo!r} understood like {clean!r}",
             (pt.service, pt.issue, pt.model, pt.intent) == (pc.service, pc.issue, pc.model, pc.intent),
             f"{(pt.service, pt.issue, pt.model, pt.intent)} vs {(pc.service, pc.issue, pc.model, pc.intent)}")
    for word in ["creta", "bahut", "problem", "laptop", "alto", "kuch", "piche"]:
        note(f"known word {word!r} is never 'corrected'", brain.normalise(word) == word,
             brain.normalise(word))

    # ---- 2. car is an entity, not a topic ------------------------------
    for msg, right, wrong in [("my alto has scratches", 36, 37),
                              ("my alto has scraches", 36, 37),
                              ("creta pe scratches hain", 36, 20)]:
        a = brain.answer(msg, None, use_ai=False)
        note(f"{msg!r} -> FAQ {right}, not {wrong}", a.faq_id == right, f"got {a.faq_id}")
    note("car tokens excluded from similarity",
         "alto" not in brain.tokens("my alto has scratches") and
         "creta" not in brain.tokens("creta ppf cost"))
    for msg, fid in [("i need alloy wheels for my alto", 37), ("wind visor for creta", 81),
                     ("seat covers for thar", 75), ("lifelong mat for sonet", 86),
                     ("fortuner 2014 convert to new model", 40), ("bmw me ppf karana hai", 28)]:
        a = brain.answer(msg, None, use_ai=False)
        note(f"car-specific FAQ {fid} still reachable via its topic words", a.faq_id == fid, f"got {a.faq_id}")

    # ---- 3. car-only is not a topic; ambiguity is short-message-only ----
    st = dialogue.transition({}, dialogue.resolve(brain.perceive("I have an Alto."), {}, MT.NEW_TOPIC),
                             MT.NEW_TOPIC, "I have an Alto.", "", None, False)
    note("car-only message stores car but no topic intent",
         st.get("car_model") == "Alto" and not st.get("intent"), str(st))
    note("'price?' after car-only is AMBIGUOUS, not a continuation of nothing",
         dialogue.classify(brain.perceive("price?"), st) == MT.AMBIGUOUS)
    note("long unknown sentence is NEW_TOPIC (reaches FAQ), not AMBIGUOUS",
         dialogue.classify(brain.perceive("laptop rakhne ke liye piche kuch h seat par?"), {}) == MT.NEW_TOPIC)
    note("FAQ 79 still answers the laptop question",
         brain.answer("laptop rakhne ke liye piche kuch h seat par?", None, use_ai=False).faq_id == 79)
    for short in ["price?", "yes", "for my car", "same for mine?", "how much will it cost?"]:
        note(f"cold {short!r} is AMBIGUOUS", dialogue.classify(brain.perceive(short), {}) == MT.AMBIGUOUS)

    # ---- 4. the owner's console transcript, with memory ----------------
    import os, tempfile
    import config as _cfg, database as _db
    old_path = _cfg.DB_PATH
    tmp = os.path.join(tempfile.gettempdir(), "cartrends_console_memory_test.db")
    for x in ("", "-wal", "-shm"):
        if os.path.exists(tmp + x):
            os.remove(tmp + x)
    _db.close_connection(); _cfg.DB_PATH = tmp
    try:
        _db.init_db()
        r = [brain.process("cons", m, use_ai=False) for m in
             ["my alto has scraches", "i want mats", "alto", "mats"]]
        GEN = "what would you like done on your"
        note("console flow: turn 1 is about scratches on the Alto",
             r[0].service == Service.DENTING and r[0].car_model == "Alto" and "alloy" not in r[0].reply.lower(), r[0].reply[:70])
        note("console flow: 'alto' after 'i want mats' continues mats",
             r[2].message_type in (MT.SLOT_ANSWER, MT.CONTINUATION, MT.CORRECTION) and GEN not in r[2].reply.lower(), r[2].reply[:70])
        note("console flow: no generic welcome anywhere", all(GEN not in x.reply.lower() for x in r))
    finally:
        _db.close_connection(); _cfg.DB_PATH = old_path
    return out


def adversarial_checks():
    """Classes found by the adversarial stress workflow, pinned as root causes.

    negation        - a term the customer rules OUT must not win
    duration/hours  - "kitna time lagega" is duration, not opening hours
    slot proposal   - a day inside an hours question, or "ok" before an
                      aside, is not a proposed appointment
    verified prices - membership / used-car price questions answer from
                      their approved answers instead of escalating
    fuzzy guard     - Hinglish verbs are never "corrected" into products
    """
    import dialogue
    MT = dialogue.MessageType
    out = []

    def note(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    # ---- negation
    for msg, want_service, want_model, want_product in [
        ("actually ceramic nahi, PPF karwana hai", Service.PPF, None, None),
        ("ppf nahi ceramic karwani hai", Service.CERAMIC, None, None),
        ("sorry its not fortuner, its innova crysta", None, "Innova", None),
        ("nahi, mera Polo hai virtus nahi", None, "Polo", None),
        ("no wait, not normal, pro wala chahiye", None, None, "gfx_pro"),
        ("mats chahiye but normal wale nahi gfx pro wale", None, None, "gfx_pro"),
    ]:
        pp = brain.perceive(msg)
        if want_service:
            note(f"negation: {msg!r} -> service {want_service}", pp.service == want_service, str(pp.service))
        if want_model:
            note(f"negation: {msg!r} -> car {want_model}", pp.model == want_model, str(pp.model))
        if want_product:
            note(f"negation: {msg!r} -> product {want_product}",
                 pp.product == want_product or pp.gfx_refinement == want_product,
                 f"{pp.product}/{pp.gfx_refinement}")
    note("negation: correction classified as CORRECTION",
         dialogue.classify(brain.perceive("sorry its not fortuner, its innova crysta"),
                           {"intent": "SERVICE_INQUIRY", "service": Service.PPF, "car_model": "Fortuner"}) == MT.CORRECTION)
    note("negation: 'ceramic nahi, PPF' while on ceramic is NEW_TOPIC (PPF)",
         dialogue.classify(brain.perceive("actually ceramic nahi, PPF karwana hai"),
                           {"intent": "SERVICE_INQUIRY", "service": Service.CERAMIC}) == MT.NEW_TOPIC)

    # ---- duration vs hours
    for msg in ["kitna time lagega bhai", "how much time will it take?", "kitne din lagenge"]:
        pp = brain.perceive(msg)
        note(f"duration: {msg!r} is DURATION, not hours", pp.intent == Intent.DURATION and pp.service != Service.HOURS,
             f"{pp.service}/{pp.intent}")
    for msg in ["are you open on Sunday?", "sunday ko opn ho?", "Sunday ko khule rehte ho?", "aapki timing kya hai"]:
        pp = brain.perceive(msg)
        note(f"hours: {msg!r} is an hours question", pp.intent == Intent.HOURS, f"{pp.service}/{pp.intent}")

    # ---- slot proposal vs side question (with a topic active)
    TOPIC = {"intent": "SERVICE_INQUIRY", "service": Service.DENTING, "issue": "the scratches", "car_model": "Seltos"}
    for msg, want in [("are you open on Sunday?", MT.SIDE_QUESTION), ("ok adress bhejo", MT.SIDE_QUESTION),
                      ("Sunday ko khule rehte ho?", MT.SIDE_QUESTION), ("location?", MT.SIDE_QUESTION),
                      ("haan kal 11 baje", MT.CONTINUATION), ("ok", MT.CONTINUATION), ("kal aa jau?", MT.CONTINUATION)]:
        got = dialogue.classify(brain.perceive(msg), dict(TOPIC))
        note(f"slot-proposal: {msg!r} -> {want}", got == want, got)

    # ---- verified prices
    for msg, figure in [("gold membership kya hai aur kitne ki hai?", "2,999"), ("Membership ka price kya hai?", "2,999"),
                        ("bhai purani car chahiye, budget kam hai, kitne se start hai?", "99,000")]:
        a = brain.answer(msg, None, use_ai=False)
        note(f"verified price answered: {msg!r}", figure in a.reply and not a.escalated, a.reply[:80])
    a = brain.answer("ppf kitne ka hai", None, use_ai=False)
    note("unverified price still not invented", not any(ch.isdigit() for ch in a.reply.replace("6367857737", "")), a.reply[:80])

    # ---- fuzzy guards
    for word in ["milenge", "kharcha", "jayenge", "aayega", "bhejo", "paise"]:
        note(f"fuzzy leaves {word!r} alone", brain.normalise(word) == word, brain.normalise(word))
    pp = brain.perceive("kitna kharcha aayega?")
    note("'kitna kharcha aayega' is a price question with no invented issue", pp.intent == Intent.PRICE_INQUIRY and pp.issue is None,
         f"{pp.norm} {pp.intent} {pp.issue}")
    pp = brain.perceive("ok and seat kavar bhi milenge creta k")
    note("'seat kavar milenge' stays seat covers, not mileage", pp.product == "seat_covers" and pp.service != Service.MECHANICAL,
         f"{pp.norm} {pp.service} {pp.product}")

    # ---- the same classes end-to-end, with memory (found by the harness)
    note("remembered text never keeps another car's sentence",
         brain._drop_other_car_sentences("We do PPF. To book your Fortuner in, call us.", "Innova") == "We do PPF.",
         brain._drop_other_car_sentences("We do PPF. To book your Fortuner in, call us.", "Innova"))
    note("'haan kal 11 baje' is Hinglish", brain._hinglish(brain.normalise("haan kal 11 baje")))
    import os, tempfile
    import config as _cfg, database as _db
    old_path = _cfg.DB_PATH
    tmp = os.path.join(tempfile.gettempdir(), "cartrends_adversarial_e2e.db")
    for x in ("", "-wal", "-shm"):
        if os.path.exists(tmp + x):
            os.remove(tmp + x)
    _db.close_connection(); _cfg.DB_PATH = tmp
    try:
        _db.init_db()
        def run(cust, msgs):
            return [brain.process(cust, m, use_ai=False) for m in msgs]
        r = run("neg", ["ceramic karwani hai creta pe", "actually ceramic nahi, PPF karwana hai", "kitna kharcha aayega?"])
        note("e2e negation: 'ceramic nahi, PPF' answers PPF only",
             r[1].service == Service.PPF and "dono hum karte" not in r[1].reply and "both ppf" not in r[1].reply.lower(), r[1].reply[:80])
        note("e2e negation: 'kitna kharcha aayega' continues the PPF topic", r[2].message_type == MT.CONTINUATION and r[2].service == Service.PPF,
             f"{r[2].message_type} {r[2].service}")
        r = run("corr", ["fortuner pe ppf karwana hai", "sorry its not fortuner, its innova crysta"])
        note("e2e correction: reply names Innova and never Fortuner",
             r[1].car_model == "Innova" and "fortuner" not in r[1].reply.lower(), r[1].reply[:100])
        r = run("gfx", ["i want mats for creta", "no wait, not normal, pro wala chahiye"])
        note("e2e refinement: 'pro wala' after plain mats becomes GFX Pro", r[1].product == "gfx_pro", f"{r[1].product} {r[1].reply[:60]}")
        r = run("side", ["Seltos pe scratches hain", "kitna time lagega bhai", "are you open on Sunday?", "ok adress bhejo", "haan kal 11 baje"])
        note("e2e duration: 'kitna time lagega' is not an hours reply",
             r[1].message_type == MT.CONTINUATION and "10:00 AM" not in r[1].reply, f"{r[1].message_type} {r[1].reply[:60]}")
        note("e2e aside: 'are you open on Sunday?' gets hours, not a slot note",
             r[2].message_type == MT.SIDE_QUESTION and "10:00 AM" in r[2].reply and "noted" not in r[2].reply.lower() and "note kar" not in r[2].reply, r[2].reply[:80])
        note("e2e aside: 'ok adress bhejo' gets the Dholai address", r[3].message_type == MT.SIDE_QUESTION and "Dholai" in r[3].reply, r[3].reply[:60])
        note("e2e slot: 'haan kal 11 baje' is acknowledged in Hinglish, never confirmed",
             "note kar liya" in r[4].reply and "confirm ho gaya" not in r[4].reply and r[4].car_model == "Seltos", r[4].reply[:80])
        r = run("switch", ["fortuner ke liye gfx mats chahiye", "bhai thar ki ceramic coating karwani hai", "kitne ka padega?"])
        note("e2e switch: price question rides on the NEW topic (ceramic, Thar)",
             r[2].service == Service.CERAMIC and r[2].car_model == "Thar" and r[2].escalated, f"{r[2].service} {r[2].car_model}")
        note("e2e switch: approved answer never re-asks for a car already named",
             "send your car model" not in r[1].reply.lower() and "reply with your car model" not in r[1].reply.lower(), r[1].reply[:90])
        r = run("used", ["purani car chahiye budget kam hai", "finance milega?", "swift mil jayegi purani?"])
        note("e2e used model: a specific used model is escalated for that model, not the finance answer",
             r[2].escalated and "Swift" in r[2].reply and "30k" not in r[2].reply, r[2].reply[:90])
        r = run("book", ["thar pe ppf karwana hai", "ok toh PPF ke liye kal aa jau?"])
        note("e2e booking follow-up: slot noted for the Thar, PPF intro not repeated, nothing confirmed",
             r[1].message_type == MT.CONTINUATION and "note kar liya" in r[1].reply and "bonnet" not in r[1].reply
             and "confirm ho gaya" not in r[1].reply and r[1].car_model == "Thar", r[1].reply[:90])
        r = run("memb", ["gold membrship kya hota h", "kitne ki h"])
        note("e2e verified price: 'kitne ki h' restates Rs 2,999 instead of escalating",
             "2,999" in r[1].reply and not r[1].escalated, r[1].reply[:60])
        # ---- classes from the second harness pass
        r = run("opn", ["seltos pe ceramic karwani hai", "sunday ko opn ho?"])
        note("e2e short typo: 'sunday ko opn ho?' is an hours aside, not a slot proposal",
             r[1].message_type == MT.SIDE_QUESTION and "10:00 AM" in r[1].reply and "note kar" not in r[1].reply, f"{r[1].message_type} {r[1].reply[:60]}")
        r = run("gen", ["sunna hai aapke yaha detailing pe 50% off chal raha hai? sach hai kya", "toh discount kitna milega verna pe?"])
        note("e2e offers: '50% off' is never confirmed and never gets the generic reply",
             "50" not in r[0].reply and "Thanks for messaging" not in r[0].reply and "photo" not in r[0].reply.lower(), r[0].reply[:90])
        note("e2e offers: the follow-up never inherits a generic reply as its topic text",
             "Thanks for messaging" not in r[1].reply and "thanks for messaging" not in r[1].reply, r[1].reply[:90])
        r = run("alloy", ["i20 pe alloy wheels lagwane hai", "16 inch me kya kya hai?"])
        note("e2e availability on a topic: '16 inch me kya kya hai?' hands over instead of the generic reply",
             "Thanks for messaging" not in r[1].reply and (r[1].escalated or "team" in r[1].reply.lower()), r[1].reply[:90])
        r = run("years", ["fortuner pe ceramic karwana hai", "and how many years does the ceramic last?"])
        note("e2e durability: 'how many years does it last' escalates as warranty-class",
             r[1].intent == Intent.WARRANTY and r[1].escalated, f"{r[1].intent} {r[1].reply[:60]}")
        a = brain.answer("scorpio ka full paint job, kal subah dedu toh sham tak guarantee ready ho jayegi?", None, use_ai=False)
        note("retrieval: 'full paint job' never pulls the alloy-painting answer",
             "alloy" not in a.reply.lower() and "caliper" not in a.reply.lower() and "ready" not in a.reply.lower(), a.reply[:90])
        a = brain.answer("PPF me kaunsa brand best hai? 3M ya Garware? aap kya lagate ho", None, use_ai=False)
        note("brand: a PPF brand question escalates and names no brand",
             a.escalated and "3m" not in a.reply.lower() and "garware" not in a.reply.lower(), a.reply[:90])
        a = brain.answer("coolant milega", None, use_ai=False)
        note("catalogue: a verified product's availability is answered, not escalated", not a.escalated and "coolant" in a.reply.lower(), a.reply[:80])
        r = run("intro", ["pff krwana h fortunar pe, warenty kitni milti h", "ok n prize?", "sry its scorpioo not fortunar", "full body pff hoga scorpioo ka na"])
        note("e2e empty topic text: 'full body PPF hoga na?' still gets the approved PPF line",
             "bonnet" in r[3].reply.lower() and "Scorpio" in r[3].reply and "fortuner" not in r[3].reply.lower(), r[3].reply[:90])
        a = brain.answer("aapki koi aur branch hai jaipur me? mansarovar side?", None, use_ai=False)
        note("branch question: verified address plus a hand-over, nothing claimed",
             "Dholai" in a.reply and "6367857737" in a.reply and "branch" in a.reply.lower() and "mansarovar" not in a.reply.lower(), a.reply[:120])
        # ---- architecture audit findings, pinned
        r = run("aud1", ["ppf karwana hai creta pe", "shocker?"])
        note("audit: a one-word NEW topic is never answered from the old topic",
             r[1].message_type == MT.NEW_TOPIC and "ppf" not in r[1].reply.lower() and r[1].service == Service.SUSPENSION, f"{r[1].message_type} {r[1].service} {r[1].reply[:70]}")
        r = run("aud2", ["ppf for my creta", "ok kal 11 baje, gfx pro bhi"])
        note("audit: a slot proposal that also names a product answers the product, state never becomes HOURS",
             "gfx" in r[1].reply.lower() and (_db.get_state(_db.get_or_create_conversation("aud2")) or {}).get("service") != Service.HOURS, r[1].reply[:80])
        r = run("aud3", ["book a ppf slot", "forget that", "I have a Swift"])
        note("audit: a cancelled booking is never resurrected from old messages",
             r[2].resolution != brain.Resolution.BOOKING_REQUESTED and "which day" not in r[2].reply.lower(), f"{r[2].resolution} {r[2].reply[:70]}")
        r = run("aud4", ["ppf for my creta", "used cars bhi hai kya?", "kitne se start hote hai?"])
        note("audit: a car remembered from another topic never withholds the verified used-car floor price",
             "99,000" in r[2].reply and not r[2].escalated, r[2].reply[:70])
        r = run("aud5", ["purani i10 chahiye", "kitne ki padegi"])
        note("audit: a car named inside the used-car topic is the one asked about",
             r[1].escalated and "i10" in r[1].reply.lower() and "99,000" not in r[1].reply, r[1].reply[:80])
        r = run("aud6", ["price?", "creta"])
        st = _db.get_state(_db.get_or_create_conversation("aud6")) or {}
        note("audit: 'price?' then 'creta' leaves a car, not a price topic", not st.get("intent") and st.get("car_model") == "Creta", str({k: st.get(k) for k in ("intent", "service", "car_model")}))
        r = run("aud7", ["door pe dent hai creta me", "ppf aur ceramic dono karte ho?"])
        note("audit: a two-service question in a denting topic is a new topic, not a dent reply",
             r[1].message_type == MT.NEW_TOPIC and "dent" not in r[1].reply.lower(), f"{r[1].message_type} {r[1].reply[:70]}")
        r = run("aud8", ["ceramic karwani hai creta pe", "sunday timing kya hai?"])
        note("audit: 'sunday timing kya hai?' inside a topic is an hours question, not a slot",
             r[1].message_type == MT.SIDE_QUESTION and "10:00 AM" in r[1].reply and "note kar" not in r[1].reply, f"{r[1].message_type} {r[1].reply[:70]}")
        kept = brain._drop_other_car_sentences("We sell used cars starting from Rs 99,000! We have models like Creta, Jeep Compass, i10, WagonR, and Alto. Visit our lot.", "Creta")
        note("audit: an approved sentence listing several cars survives for any customer", "WagonR" in kept and "99,000" in kept, kept[:80])
        note("audit: remembered text stays intact when no car is known", brain._drop_other_car_sentences("To book your Fortuner in, call us.", None) != "")
        import threading as _th
        errs = []
        def _go(m):
            try:
                brain.process("aud9", m, use_ai=False)
            except Exception as e:
                errs.append(repr(e))
        ths = [_th.Thread(target=_go, args=(m,)) for m in ("ppf karwana hai", "creta 2021")]
        [x.start() for x in ths]; [x.join() for x in ths]
        conn = _db.get_connection()
        n_conv = conn.execute("SELECT COUNT(*) FROM conversations WHERE customer_identifier = 'aud9'").fetchone()[0]
        st = _db.get_state(_db.get_or_create_conversation("aud9")) or {}
        note("audit: two concurrent DMs from one customer keep one conversation and both facts",
             not errs and n_conv == 1 and st.get("service") == Service.PPF and st.get("car_model") == "Creta", f"errs={errs} conv={n_conv} state={ {k: st.get(k) for k in ('service', 'car_model', 'car_year')} }")
        # ---- re-audit edge findings, pinned
        r = run("edge1", ["where are you located, i have a creta", "ppf karwani hai"])
        note("edge: a car named inside an aside is remembered", r[1].car_model == "Creta" and "which car" not in r[1].reply.lower(), f"{r[1].car_model} {r[1].reply[:60]}")
        r = run("edge2", ["i already have ppf, can i do ceramic on top?", "creta", "dono ek saath ho sakta hai?"])
        st = _db.get_state(_db.get_or_create_conversation("edge2")) or {}
        note("edge: an ownership fact in the opener is kept and the request is the other service",
             (st.get("facts") or {}).get("has_ppf") and st.get("service") == Service.CERAMIC, f"{st.get('facts')} {st.get('service')}")
        r = run("edge3", ["ppf?", "creta", "tuesday"])
        note("edge: a bare day inside a service topic is a slot proposal", "noted" in r[2].reply.lower() or "note kar" in r[2].reply, r[2].reply[:70])
        r = run("edge4", ["creta price?", "second hand car chahiye"])
        note("edge: the car named with the pending question is the used car asked about", r[1].escalated and "creta" in r[1].reply.lower() and "99,000" not in r[1].reply, r[1].reply[:80])
        note("edge: 'creta price?' does not ask which car", "which car" not in r[0].reply.lower() and "Creta" in r[0].reply, r[0].reply[:80])
        r = run("edge5", ["warranty?", "second hand car chahiye"])
        note("edge: a pending warranty question survives into a used-car topic", r[1].intent == Intent.WARRANTY, f"{r[1].intent} {r[1].reply[:60]}")
        r = run("edge6", ["ppf for my creta", "swift ka price?"])
        note("edge: a price question inside a car correction is handed over in words",
             r[1].car_model == "Swift" and ("exact price" in r[1].reply.lower() or "price team" in r[1].reply.lower()) and r[1].intent != Intent.BOOKING_REQUEST, f"{r[1].intent} {r[1].reply[:90]}")
        r = run("edge7", ["door pe dent hai", "it has 2 dents"])
        note("edge: 'it has 2 dents' is a dent report, not an affirmation", "preferred day" not in r[1].reply.lower(), r[1].reply[:70])
        r = run("edge8", ["book a slot", "creta"])
        note("edge: a bare booking topic never reads 'For this on your Creta'", "for this" not in r[1].reply.lower() and " this ke liye" not in r[1].reply.lower(), r[1].reply[:70])
        r = run("edge9", ["kal aa sakta hu, price kitna hoga aur kitne din lagenge?"])
        r = run("edge9", ["ppf karwana hai creta pe", "kal aa sakta hu, price kitna hoga aur kitne din lagenge?"])
        note("edge: a slot proposal carrying price and duration questions answers all three", "price" in r[1].reply.lower() and ("din" in r[1].reply.lower() or "time" in r[1].reply.lower()), r[1].reply[:90])
        note("edge: duplicate delivery rolls back and later writes still succeed",
             _db.event_already_seen("dup-x") is False and _db.event_already_seen("dup-x") is True and _db.event_already_seen("dup-y") is False)
        r = run("edge10", ["ceramic karwani hai", "creta", "warranty?"])
        st = _db.get_state(_db.get_or_create_conversation("edge10")) or {}
        note("edge: a warranty follow-up never stores a cross-topic answer as the ceramic topic's memory",
             "warranty" not in (st.get("core_reply") or "").lower() or "ceramic" in (st.get("core_reply") or "").lower(), (st.get("core_reply") or "")[:70])
        r = run("usedcorr", ["purani car chahiye", "baleno bhi dekh lo, 2020 wali"])
        note("e2e used model on a correction: Baleno is handed to the team, nothing claimed",
             "Baleno" in r[1].reply and r[1].escalated, r[1].reply[:90])
    finally:
        _db.close_connection(); _cfg.DB_PATH = old_path
    return out


def stock_claim_checks():
    """Offering a category must never become "it is in stock right now"."""
    out = []
    for msg in ["Seltos GFX mat available?", "virtus armrest available?",
                "car cover chahiye", "Skoda octavia mat",
                "do you have alloy wheels for alto", "roof rails available?",
                "dashcam available hai?", "tyres milenge?"]:
        r = brain.answer(msg, None, use_ai=False)
        low = r.reply.lower()
        claims = [c for c in ("currently in stock", "in stock right now",
                              "we have it in stock", "right now we have",
                              "available right now")
                  if c in low]
        out.append((repr(msg) + " claims no live stock", not claims,
                    str(claims)))
        out.append((repr(msg) + " hands over properly",
                    kb.PHONE in r.reply or "team" in low, r.reply[:60]))
    return out


# ---------------------------------------------------------------------------
# ADDRESS CONSISTENCY - runs before the conversational cases
# ---------------------------------------------------------------------------
# The owner has confirmed Dholai as the single current address. These checks
# make sure NOTHING a customer can see says otherwise - not an FAQ answer,
# not a scripted reply, not the system prompt.
LOCATION_QUESTIONS = [
    "Where are you located?", "Where is your workshop?", "Dholai me ho?",
    "Dholai me shop hai?", "Mansarovar me ho?", "What is your address?",
    "shop kaha hai", "addres plzz", "which area in jaipur?",
    "send map location bro", "kaha ho aap", "location send karo",
    "aapki shop kaha hai", "dholai me shop hai kya tumhari?",
    "workshop kidhar hai", "exact location kya hai",
]


def address_consistency_checks() -> List[Tuple[str, bool, str]]:
    """Every location surface must agree on the one verified address."""
    out: List[Tuple[str, bool, str]] = []
    former = kb.BUSINESS["former_area"].lower()

    def note(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    note("BUSINESS address names Dholai",
         "dholai" in kb.BUSINESS["address"].lower())
    note("BUSINESS address carries the pincode",
         "302020" in kb.BUSINESS["address"])
    note("BUSINESS address does not name the former area",
         former not in kb.BUSINESS["address"].lower())

    bad = [f["id"] for f in kb.APPROVED_FAQS
           if former in ((f["answer"] or "") + (f["note"] or "")).lower()]
    note("no FAQ answer names the former area", not bad, f"FAQs {bad}")

    for label, text in [("LOCATION_REPLY", brain.LOCATION_REPLY),
                        ("FORMER_AREA_REPLY", brain.FORMER_AREA_REPLY),
                        ("SYSTEM_PROMPT", brain.SYSTEM_PROMPT)]:
        note(f"{label} names Dholai", "dholai" in text.lower())
        note(f"{label} does not present the former area",
             former not in text.lower())

    # Every location answer a customer can actually receive must say Dholai.
    for q in LOCATION_QUESTIONS:
        r = brain.answer(q, None, use_ai=False)
        says_dholai = "dholai" in r.reply.lower()
        says_former = former in r.reply.lower()
        note(f"reply names Dholai: {q!r}", says_dholai, r.reply[:80])
        note(f"reply avoids former area: {q!r}", not says_former, r.reply[:80])
    return out


def misspelling_checks() -> List[Tuple[str, bool, str]]:
    """Real customer spellings that must reach the right product.

    9-11 Sep 2026, live: "i20 model mate milega" was answered with the SPARE
    PARTS reply and "floor mate milega" was escalated, because "mate" - how
    "mat" is very often typed - matched nothing. The phrase table maps it
    only where it can only mean the mat.
    """
    out: List[Tuple[str, bool, str]] = []

    def note(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    for q in ["Sir hyundai i20 active patrol 2019  model mate milega",
              "Hyundai i20 active patrol model 1019  floor mate milega",
              "car mate chahiye", "floor mates available", "gfx mate price",
              "7d mate chahiye"]:
        r = brain.answer(q, None, use_ai=False)
        note(f"'mate' spelling reaches the mats answer: {q!r}",
             r.product in ("floor_mats", "gfx", "gfx_pro", "gfx_normal"),
             f"product={r.product} reply={r.reply[:70]}")
        note(f"'mate' spelling never gets the spare-parts answer: {q!r}",
             "spare parts" not in r.reply.lower(), r.reply[:70])

    # In Gujarati "mate" means "for": "i20 mate seat cover" is about seat covers.
    r = brain.answer("i20 mate seat cover chahiye", None, use_ai=False)
    note("Gujarati 'mate' (for) is not rewritten into mats",
         r.product == "seat_covers", f"product={r.product}")
    note("a phrase never fires inside a longer word ('ultimate')",
         brain.normalise("ultimate milega").split()[0] == "ultimate",
         brain.normalise("ultimate milega"))
    note("existing phrase corrections still apply ('jaisa banana' -> convert)",
         "convert" in brain.normalise("fortuner ko legender jaisa banana hai"),
         brain.normalise("fortuner ko legender jaisa banana hai"))
    return out


def main(use_ai: bool = False) -> int:
    print("=" * 78)
    print(f" CAR TRENDS CHATBOT - AUTOMATED TEST SUITE   ({len(CASES)} cases)")
    print(f" language model: {'ENABLED' if use_ai else 'DISABLED (deterministic layers only)'}")
    print("=" * 78)

    passed = failed = 0
    failures: List[Dict[str, Any]] = []

    # --- address consistency first: a wrong address is the worst failure ---
    print("\n--- ADDRESS CONSISTENCY ---")
    addr_failed = 0
    for name, ok, detail in address_consistency_checks():
        if ok:
            passed += 1
        else:
            failed += 1
            addr_failed += 1
            print(f"  FAIL  {name}   [{detail}]")
            failures.append({"q": name, "expected": "Dholai address only",
                             "actual": detail, "reply": detail,
                             "problem": "location conflict",
                             "fix": "update the offending FAQ or reply"})
    print(f"  {'all address checks passed' if not addr_failed else str(addr_failed) + ' FAILED'}")

    for title, fn in [("WRONG-ANSWER REGRESSIONS", wrong_answer_checks),
                      ("NO UPSELLING", no_upsell_checks),
                      ("GFX PRODUCT KNOWLEDGE", gfx_checks),
                      ("PENDING INTENT / MEMORY", pending_intent_checks),
                      ("DIALOGUE STATE (root cause)", dialogue_state_checks),
                      ("ROBUSTNESS (typos / entities / console memory)", robustness_checks),
                      ("ADVERSARIAL CLASSES (negation / duration / slot / prices / fuzzy)", adversarial_checks),
                      ("STOCK CLAIMS", stock_claim_checks),
                      ("REAL CUSTOMER SPELLINGS", misspelling_checks)]:
        print("\n--- " + title + " ---")
        section_failed = 0
        for name, ok, detail in fn():
            if ok:
                passed += 1
            else:
                failed += 1
                section_failed += 1
                print("  FAIL  " + name + "   [" + str(detail) + "]")
                failures.append({"q": name, "expected": "correct answer",
                                 "actual": detail, "reply": str(detail),
                                 "problem": title.lower(),
                                 "fix": "see the retrieval guards in brain.py"})
        print("  " + ("all passed" if not section_failed
                      else str(section_failed) + " FAILED"))

    for case in CASES:
        message = case[0]
        try:
            result = brain.answer(message, None, use_ai=use_ai)
        except Exception as error:
            failed += 1
            failures.append({"q": message, "problem": f"EXCEPTION {error!r}",
                             "fix": "fix the crash", "reply": "",
                             "expected": case[1:], "actual": "-"})
            continue

        ok, problem, fix = evaluate(case, result)
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append({
                "q": message,
                "expected": f"service={case[1]} intent={case[2]} "
                            f"behaviour={case[3]}",
                "actual": f"service={result.service} intent={result.intent} "
                          f"source={result.source} escalated={result.escalated}",
                "reply": result.reply,
                "problem": problem,
                "fix": fix,
            })

    if failures:
        print(f"\n{'=' * 78}\n FAILURES ({len(failures)})\n{'=' * 78}")
        for f in failures:
            print(f"\nQUESTION  : {f['q']}")
            print(f"EXPECTED  : {f['expected']}")
            print(f"ACTUAL    : {f['actual']}")
            print(f"RESPONSE  : {f['reply'][:200]}")
            print(f"PROBLEM   : {f['problem']}")
            print(f"FIX       : {f['fix']}")

    total = passed + failed
    print(f"\n{'=' * 78}")
    print(f" Conversation cases : {len(CASES)}")
    print(f" Total assertions   : {total}")
    print(f" Passed      : {passed}")
    print(f" Failed      : {failed}")
    print(f" Skipped     : 0")
    rate = round(passed / total * 100, 1) if total else 0.0
    print(f" Pass rate   : {rate}%")
    print("=" * 78)
    return failed


if __name__ == "__main__":
    sys.exit(1 if main(use_ai="--ai" in sys.argv) else 0)
