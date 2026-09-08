"""
=============================================================================
 Car Trends Car Mall - THE CHATBOT BRAIN
=============================================================================

 ONE BRAIN, TWO FRONT DOORS
 --------------------------
 Instagram (bot.py) and the local console (chat.py) both call answer() in
 this module. There is deliberately no separate "testing brain": what you
 try in the console is exactly what a customer receives.

 THE ANSWER PIPELINE
 -------------------
     customer message
        |
        v
     normalise  (lowercase, strip punctuation, expand Hinglish/typos)
        |
        v
     detect service + intent + buying intent + car model
        |
        v
     1. MANAGER_DATA        -> approved manager answer, if one exists
     2. APPROVED_FAQS       -> best semantic match among the 99 entries
          |- confirmed          -> use the approved answer
          |- missing info       -> ESCALATE + record a knowledge gap
     3. deterministic replies  -> location / hours / contact
     4. Ollama                 -> only for wording, never for facts
        |
        v
     reply + metadata (never shown to the customer)

 WHY A LANGUAGE MODEL IS NOT TRUSTED WITH FACTS
 ----------------------------------------------
 llama3.2:3b is small. Told "never invent hours" it still invents them. So
 every business fact is decided in Python before the model is consulted,
 and the model's only job is the leftovers - greetings and small talk.
=============================================================================
"""

import difflib
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
import database as db
import dialogue
import knowledge as kb
from dialogue import MessageType as MT
from knowledge import BUSINESS, PHONE, Intent, LeadStatus, Resolution, Service

# ===========================================================================
# TEXT NORMALISATION
# ===========================================================================
# Instagram DMs are short, misspelt and code-mixed. Everything below turns
# the many ways of writing one thing into a single canonical form, so the
# matcher does not need a rule per spelling.

# Deliberately conservative: only rewrites where the intent is unambiguous.
SYNONYMS: Dict[str, str] = {
    # --- PPF / ceramic spellings ---
    "ceramik": "ceramic", "ceramich": "ceramic", "seramic": "ceramic",
    "ceremic": "ceramic", "cermaic": "ceramic", "cotin": "coating",
    "coting": "coating", "cotng": "coating", "coatng": "coating",
    "pff": "ppf", "ppff": "ppf",
    # --- price words ---
    "prce": "price", "pric": "price", "prise": "price", "rt": "rate",
    "kimat": "price", "keemat": "price", "daam": "price", "rate": "price",
    "cost": "price", "charges": "price", "mrp": "price",
    "quotation": "price", "validity": "valid",
    # NOTE: kitna / kitne / kitni are deliberately NOT rewritten to "price".
    # They mean "how much/how many" generally, and "kitne din" (how many
    # days) and "kitne baje" (at what time) are duration and hours
    # questions. They are listed in PRICE_WORDS instead, which is checked
    # AFTER the duration and hours checks.
    # --- time words ---
    "tym": "time", "tym": "time", "opning": "opening", "opning": "opening",
    "khulta": "open", "khulte": "open", "khulti": "open", "khule": "open",
    "khula": "open", "band": "closed", "timing": "time", "timings": "time",
    "baje": "time",
    # --- general Hinglish ---
    "kaha": "where", "kahan": "where", "kidhar": "where", "pata": "address",
    "addres": "address", "adress": "address", "adres": "address",
    "opn": "open", "opne": "open", "clsd": "closed", "mch": "much",
    "seet": "seat", "wsh": "wash", "crta": "creta", "tyers": "tyres",
    "tyer": "tyre", "wagnr": "wagonr", "prise": "price", "pric": "price",
    "hw": "how", "wat": "what", "wht": "what", "whn": "when",
    "nmbr": "number", "mob": "mobile",
    "gaadi": "car", "gadi": "car", "gaddi": "car", "car's": "car",
    "krna": "karna", "krni": "karni", "chaiye": "chahiye", "chahie": "chahiye",
    "milega": "available", "milegi": "available", "milti": "available",
    "mil": "available", "hoga": "possible", "hogi": "possible",
    "krte": "karte", "kre": "kare",
    "acha": "good", "achha": "good",
    "plzz": "please", "plz": "please", "pls": "please",
    "ur": "your", "u": "you", "n": "and",
    "vaccum": "vacuum", "vaccume": "vacuum", "vacume": "vacuum",
    "aloy": "alloy", "alloys": "alloy", "rims": "rim",
    "puncher": "puncture", "punchar": "puncture",
    "denting": "dent", "dents": "dent", "denting-painting": "dent paint",
    "scratches": "scratch", "warrenty": "warranty", "warrantee": "warranty",
    "guarantee": "warranty", "gaurantee": "warranty",
    "servis": "service", "servicing": "service", "services": "service",
    "sunday": "sunday", "sundays": "sunday",
    "membrship": "membership", "membrshp": "membership",
}

# Words that carry no meaning for matching. Removing them stops long polite
# sentences from scoring lower than terse ones.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "do", "does", "did", "can", "could",
    "will", "would", "i", "me", "my", "we", "our", "you", "your", "it", "its",
    "of", "for", "to", "in", "on", "at", "and", "or", "but", "if", "so",
    "please", "pls", "hi", "hello", "hey", "sir", "bhai", "bro", "yaar",
    "ji", "kya", "hai", "he", "h", "ho", "hu", "hoon", "ka", "ki", "ke",
    "me", "mein", "se", "ko", "par", "pe", "bhi", "aur", "tha", "the",
    "much", "how", "what", "there", "any", "some", "get", "got", "want",
    "need", "tell", "give", "know", "us", "have", "has", "am", "be",
}

# Multi-word idioms whose meaning no single word carries. Applied before the
# word-level synonyms. Keep these canonical forms inside the vocabulary.
PHRASE_SYNONYMS: Dict[str, str] = {
    "xuv 700": "xuv700", "xuv 300": "xuv300",
    "jaisa banana": "convert", "jaise banana": "convert", "jaisa bana": "convert",
    "jaisa look": "convert look", "jaisa dikhna": "convert look",
    "convert karna": "convert", "convert karwana": "convert",
    "new model jaisa": "convert new model", "naye model jaisa": "convert new model",
    "look change": "convert look", "body kit": "body kit conversion",
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, map known spellings, fix typos.

    Typo handling is a real fuzzy layer, not a hand-written list: any word
    the vocabulary does not know is compared against every word it does
    know (services, products, cars, problems, Hinglish), and replaced when
    it is clearly a misspelling of one. "scraches" becomes "scratches",
    "fortunar" becomes "fortuner", "ceramik" becomes "ceramic". Short and
    already-known words are never touched, so "alto" stays "alto".
    """
    text = (text or "").lower().strip()
    text = _PUNCT.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    # Multi-word Hinglish idioms first: "jaisa banana" (make it look like)
    # is a body-conversion request, but no single word in it says so.
    for phrase, canon in PHRASE_SYNONYMS.items():
        if phrase in text:
            text = text.replace(phrase, canon)
    # Synonyms run before AND after the fuzzy step: a corrected spelling
    # ("scraches" -> "scratches") must still map to its canonical form.
    words = [SYNONYMS.get(f, f) for f in
             (fuzzy_fix(SYNONYMS.get(w, w)) for w in text.split())]
    return " ".join(words)


# ---------------------------------------------------------------------------
# FUZZY TYPO CORRECTION
# ---------------------------------------------------------------------------
# Built lazily from every vocabulary list in this module plus the approved
# FAQ questions, so a term the bot can act on is a term it can also
# recognise when misspelt. Words that appear in the FAQ questions are
# "known" and therefore protected from correction.
_FUZZY_VOCAB: List[str] = []
_FUZZY_SET = set()

COMMON_WORDS = [
    # Hinglish and English words customers use that are not in any list;
    # listed so they are treated as known and never "corrected" away.
    "bahut", "bohot", "thoda", "problem", "issue", "want", "need", "please",
    "gaadi", "gadi", "car", "model", "wala", "wale", "wali", "karwao",
    "karwau", "karwaun", "batao", "bhejo", "dikkat", "kharab", "padega",
    "lagega", "lagenge", "hoga", "hogi", "chahiye", "chaiye", "sakta",
    "sakti", "milega", "milegi", "kitna", "kitne", "kitni", "kab", "kaise",
    "kaisa", "kaunsa", "konsa", "acha", "achha", "theek", "thik", "haan",
    "nahi", "nhi", "abhi", "kal", "aaj", "subah", "shaam", "baje", "time",
    "front", "rear", "back", "side", "door", "bonnet", "bumper", "roof",
    "full", "body", "interior", "exterior", "andar", "bahar", "dono",
    "already", "actually", "forget", "sorry", "thanks", "thank", "okay",
    "hello", "hi", "hey", "good", "morning", "evening", "night", "bhai",
    "bro", "sir", "madam", "mam", "yaar", "suggest", "recommend", "which",
    "what", "when", "where", "how", "much", "many", "long", "does", "take",
    "week", "month", "year", "today", "tomorrow", "urgent", "quick",
    "laptop", "tablet", "phone", "number", "location", "address",
    # Hinglish verb forms that fuzzy-matched onto product words
    # ("milenge" -> "mileage", "kharcha" -> "kharoch").
    "milenge", "milega", "milegi", "milti", "milta", "mile", "jayenge",
    "jayega", "jayegi", "karenge", "karega", "karegi", "denge", "dega",
    "degi", "lenge", "lunga", "lungi", "sakenge", "honge", "hoga", "hogi",
    "rahega", "rahegi", "rahenge", "chahenge", "aayega", "aayegi", "aaunga",
    "aaungi", "laaunga", "launga", "bhejna", "bhejo", "bataye", "batana",
    "kharcha", "kharch", "kharche", "paisa", "paise", "rupaye", "rupees",
    # words the rules key on - "brand" once became "band" -> "closed"
    "brand", "brands", "garware", "xpel", "llumar", "company", "deal",
    "deals", "sale", "offer", "offers", "lasts", "lifespan", "durability",
    "durable", "tikega", "chalega", "chalegi", "saal", "android", "system",
    "wireless", "installation", "quote", "approx", "lakh", "branch",
    "branches", "timing", "timings", "inch", "size",
    # real words one typo away from a car model - "shift" is not a Swift
    "shift", "shifted", "shifting", "swiss", "verma", "sharma", "innovate",
    "innovation", "virtue", "breeze", "fortune", "fortunate", "crete", "sonnet",
    "nexus", "pole", "poll", "polio", "altar", "wagon", "pool", "polar",
    "andar", "bahar", "upar", "neeche", "peeche", "aage", "saath", "bina",
]


_VOCAB_LIST_NAMES = (
    "PRICE_WORDS", "WARRANTY_WORDS", "DURATION_WORDS", "BOOKING_WORDS",
    "COMPARISON_WORDS", "AVAILABILITY_WORDS", "PICKUP_WORDS",
    "COMPLAINT_WORDS", "HUMAN_WORDS", "HIGH_INTENT", "MEDIUM_INTENT",
    "AFFIRM_WORDS", "CANCEL_WORDS", "OPTIONS_WORDS", "LIVE_STOCK_WORDS",
    "RECOMMEND_WORDS", "STRONG_BOOKING_WORDS", "OWNERSHIP_WORDS",
    "WANT_WORDS", "BOTH_WORDS", "COMMON_WORDS", "CAR_MODELS", "CAR_MAKES",
)
_FUZZY_READY = False


def _build_fuzzy_vocab() -> bool:
    """Build the vocabulary. Returns False if the module is still importing
    (some lists are defined later in this file) - nothing is corrected
    until every list exists, so import order can never produce a partial
    vocabulary that corrects words wrongly."""
    global _FUZZY_READY
    g = globals()
    needed = list(_VOCAB_LIST_NAMES) + ["SERVICE_VOCAB", "ISSUE_WORDS",
                                        "SYNONYMS", "STOPWORDS"]
    if any(name not in g for name in needed):
        return False
    words = set()

    def add_phrases(phrases):
        for ph in phrases:
            for w in str(ph).split():
                if w.isalpha():
                    words.add(w)

    for _, vocab in g["SERVICE_VOCAB"]:
        add_phrases(vocab)
    add_phrases(kb.PRODUCT_ALIASES.keys())
    for _, ws in g["ISSUE_WORDS"]:
        add_phrases(ws)
    for name in _VOCAB_LIST_NAMES:
        add_phrases(g[name])
    add_phrases(g["SYNONYMS"].keys())
    add_phrases(g["SYNONYMS"].values())
    add_phrases(g["STOPWORDS"])
    for faq in kb.APPROVED_FAQS:
        add_phrases(_PUNCT.sub(" ", faq["question"].lower()).split())
    _FUZZY_SET.clear()
    _FUZZY_SET.update(words)
    _FUZZY_VOCAB[:] = sorted(w for w in words if len(w) >= 4)
    _FUZZY_READY = True
    return True


def fuzzy_fix(token: str) -> str:
    """Return the vocabulary word `token` is a misspelling of, or `token`."""
    if not _FUZZY_READY and not _build_fuzzy_vocab():
        return token
    if len(token) < 5 or not token.isalpha() or token in _FUZZY_SET:
        return token
    # Stricter for short words - a 5-letter word has little room for error.
    cutoff = 0.80                      # one wrong/transposed letter in 5+ letters
    match = difflib.get_close_matches(token, _FUZZY_VOCAB, n=1, cutoff=cutoff)
    if not match:
        return token
    # Never let a correction change the word's length by more than 2.
    return match[0] if abs(len(match[0]) - len(token)) <= 2 else token


def _singular(word: str) -> str:
    """Crude plural stripping so "mats" and "mat" are the same token.

    Only for words of 4+ characters, and never for "ss" endings, so
    "address" and "class" survive intact.
    """
    if len(word) > 5 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]                       # scratches -> scratch
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> List[str]:
    """Meaningful words only, for similarity scoring.

    Car models and makes are EXCLUDED on purpose. A car is an entity, not a
    topic: "my alto has scratches" shares the rare word "alto" with the
    alloy-wheels FAQ ("...for my alto"), and rarity made that one word
    outweigh the topic. Which FAQ applies is decided by what the customer
    wants done, never by which car they drive.
    """
    cars = globals().get("_CAR_TOKENS") or set()
    return [_singular(w) for w in normalise(text).split()
            if w not in STOPWORDS and len(w) > 1 and w not in cars]


# Words that negate the term right before or after them:
#   "ceramic nahi, ppf"  "not fortuner, innova"  "normal wale nahi"
_NEG_AFTER = re.compile(r"\b([a-z0-9]+)(?:\s+wal[aei])?\s+(?:nahi|nhi|nai|nahin)\b")
_NEG_BEFORE = re.compile(r"\b(?:not|never)\s+(?:the\s+|a\s+)?([a-z0-9]+)\b")

# "no ppf, ceramic chahiye" rules out ppf; "No, Alto." (nothing after it)
# is an answer to our question, not a negation.
_NEG_NO = re.compile(r"\bno\s+(?:the\s+|a\s+)?([a-z0-9]+)\b(?=\s+[a-z0-9])")

# "rehne do pro, normal hi chahiye" - the word after a drop-verb is ruled out.
_NEG_LEAVE = re.compile("(?:rehne do|rahne do|chhod do|chod do|chhodo|chodo|hatao|leave|skip)"
                        + r"\s+([a-z0-9]+)")


def negated_terms(norm: str) -> set:
    """Every word the customer explicitly ruled OUT in this message."""
    terms = (set(_NEG_AFTER.findall(norm)) | set(_NEG_BEFORE.findall(norm))
             | set(_NEG_NO.findall(norm)) | set(_NEG_LEAVE.findall(norm)))
    # "no wait", "not sure" etc. are not negated terms.
    return {t for t in terms if t not in ("wait", "sure", "problem", "issue",
                                           "worry", "tension", "thanks", "thank")}


def contains_any(text: str, phrases: List[str]) -> bool:
    """Whole-word/phrase containment against already-normalised text.

    A trailing "s"/"es" is tolerated so a vocabulary only has to list the
    singular: "subwoofer" also matches "subwoofers", "mat" matches "mats".
    Word boundaries still apply, so "rate" never matches inside "great".
    """
    for p in phrases:
        if re.search(r"\b" + re.escape(p) + r"(?:e?s)?\b", text):
            return True
    return False


# ===========================================================================
# SERVICE VOCABULARY
# ===========================================================================
# Order matters: the first service whose vocabulary matches wins, so the
# more specific entries are listed first.
SERVICE_VOCAB: List[Tuple[str, List[str]]] = [
    (Service.MEMBERSHIP, [
        "gold membership", "membership", "member", "2999", "fire extinguisher",
        "extinguisher", "gold card"]),
    # CERAMIC before PPF: "ceramic vs ppf" is catalogued under ceramic in
    # the approved FAQs (31, 34), and "ppf price" still lands on PPF
    # because no ceramic word appears in it.
    (Service.CERAMIC, [
        "ceramic", "ceramic coating", "glass coating", "sun proofing",
        "sunproofing", "sun protection", "teflon"]),
    # PPF before USED_CARS so "old car pe ppf ho sakta hai" is a PPF
    # question, not a used-car one - "old car" appears in both.
    (Service.PPF, ["ppf", "paint protection", "paint protection film"]),
    (Service.USED_CARS, [
        "second hand", "2nd hand", "used car", "old car", "purani car",
        "pre owned", "preowned", "resale", "downpayment", "finance", "rto",
        "used", "purani"]),
    (Service.ALLOY_SALES, [
        "alloy wheel", "alloy rim", "new alloy", "buy alloy", "alloy"]),
    (Service.DETAILING, [
        "detailing", "polish", "polishing", "compounding", "rubbing",
        "paint correction", "buffing", "shine"]),
    (Service.CAR_WASH, ["car wash", "wash", "washing", "washes"]),
    (Service.PAINTING, [
        "paint", "painting", "repaint", "paint booth", "jetstar", "caliper",
        "colour", "color"]),
    (Service.DENTING, [
        "dent", "denting", "scratch", "bumper", "body conversion",
        "conversion", "accident", "modification", "body work", "bodywork",
        # "Fortuner 2011 ko Legender jaisa banana hai" is a body conversion -
        # FAQ 40 covers exactly this - but none of the words above matched it.
        "convert", "legender", "jaisa banana", "jaisa bana", "modify",
        "body kit", "facelift", "new model jaisa", "look change"]),
    (Service.TYRES, [
        "tyre", "tire", "wheel alignment", "alignment", "balancing",
        "puncture", "wheel balance"]),
    (Service.SUSPENSION, [
        "suspension", "shock absorber", "shocker", "spring cushion",
        "ground clearance"]),
    (Service.AUDIO, [
        "speaker", "subwoofer", "woofer", "jbl", "hertz", "jl audio", "morel",
        "music system", "sound system", "apple carplay", "carplay",
        "apple car play", "car play", "android auto", "amplifier", "stereo"]),
    (Service.SPARE_PARTS, [
        "spare part", "spare parts", "timing belt", "water pump",
        "clutch plate", "valve lifter", "vvt solenoid", "vvt"]),
    (Service.MECHANICAL, [
        "service", "engine", "brake", "brake pad", "brake disc", "ac",
        "air conditioner", "mileage", "battery", "catalytic", "converter",
        "o2 sensor", "oxygen sensor", "clutch", "overhaul", "repair",
        "cooling", "noise", "awaz", "aawaz", "overheating"]),
    (Service.ACCESSORIES, [
        "mat", "mats", "7d mat", "lifelong", "wiper", "vacuum", "blaupunkt",
        "moco", "seat cover", "engine oil", "coolant", "perfume", "sun shade",
        "wind visor", "badge", "neck pillow", "neck rest", "organizer",
        "organiser", "accessory", "accessories", "dashcam", "camera"]),
    (Service.LOCATION, [
        "where", "address", "location", "map", "direction", "directions",
        "area", "landmark", "branch", "branches", "parking", "dholai",
        "mansarovar", "iskcon", "reach", "shop", "showroom", "store"]),
    (Service.CONTACT, [
        "number", "mobile", "phone", "whatsapp", "contact", "call"]),
    (Service.HOURS, [
        "open", "closed", "time", "sunday", "monday", "saturday", "holiday",
        "weekend", "chutti", "working day", "working days", "timing",
        "timings", "khula", "khule", "band"]),
]


# ===========================================================================
# PRODUCT RECOGNITION
# ===========================================================================
# Longest alias first, so "seat cover" beats "cover" and "engine oil" beats
# "oil". Without this the customer's actual product is missed and the
# matcher falls back to guessing from whatever words happen to be shared.
_ALIASES_SORTED = sorted(kb.PRODUCT_ALIASES.items(),
                         key=lambda kv: len(kv[0]), reverse=True)


def detect_products(norm: str) -> List[str]:
    """Every product the customer named, in order of appearance."""
    found: List[Tuple[int, int, str]] = []
    seen = set()
    neg = negated_terms(norm)
    for alias, key in _ALIASES_SORTED:
        m = re.search(r"\b" + re.escape(alias) + r"\b", norm)
        if m and any(w in neg for w in alias.split()):
            continue                      # "not normal, pro wala" - drop normal
        if m and key not in seen:
            seen.add(key)
            # Sort key: position, then the LONGER alias first. "gfx pro mat"
            # and "gfx" start at the same place; the specific one must win,
            # not the alphabetically earlier key.
            found.append((m.start(), -len(alias), key))
    return [k for _, _, k in sorted(found)]


def _product_suppressed(key: str, norm: str) -> bool:
    """True when the message is about a SERVICE on this item, not buying it.

    "tyre balancing karte ho" matched the (unverified) tyres product and
    escalated, even though wheel balancing is verified in FAQ 70.
    """
    words = kb.PRODUCTS[key].get("suppress_if") or []
    return bool(words) and contains_any(norm, words)


def detect_product(norm: str) -> Optional[str]:
    """The product to ANSWER about when several are named.

    A verified product wins over an unverified one, so "Fortuner ke mats
    chahiye aur armrest bhi" is answered about the mats we can actually
    confirm, with the armrest handled separately rather than hijacking the
    whole reply.
    """
    products = [k for k in detect_products(norm)
                if not _product_suppressed(k, norm)]
    if not products:
        return None
    # "mats chahiye but normal wale nahi gfx pro wale" - the specific GFX
    # variant must beat the generic "mats" that happens to come first.
    for key in products:
        if key in GFX_KEYS:
            return key
    for key in products:
        if kb.PRODUCTS[key]["verified"]:
            return key
    return products[0]


# "aapki koi aur branch hai?" - nothing approved says; the address alone
# does not answer a yes/no question about OTHER branches.
BRANCH_WORDS = ["aur branch", "koi branch", "other branch", "another branch",
                "branches", "kitni branch", "dusri branch", "branch hai kya",
                "branch hai ya", "any branch", "aur koi location",
                "other location", "dusri location", "another location",
                "aur shop", "dusri shop", "second shop", "other outlet",
                "outlets", "aur koi shop"]

# "ye reel wali car konsi hai" - a question about the video, never a catalogue match.
REEL_CAR_WORDS = ["reel wali car", "reel me car", "reel mein car", "video wali car",
                  "video me car", "which car is this", "what car is this", "car in this reel",
                  "car in the reel", "car in this video", "ye car konsi", "yeh car konsi",
                  "ye konsi car", "is car ka naam", "car ka naam kya", "kaunsi car hai ye",
                  "konsi car hai ye", "reel wali gaadi", "video wali gaadi"]

HOURS_STRONG_WORDS = ["open", "opens", "closed", "close", "khula", "khule",
                      "khulte", "band", "timing", "timings", "holiday",
                      "chutti", "weekend", "working day", "hours"]


def detect_service(norm: str) -> Optional[str]:
    """Return the service the message is about, or None.

    The alloy check comes first because we do two different things with
    alloys - we sell them and we paint them - and the plain vocabulary loop
    cannot tell "alloy wheels for my Alto" (a sale) from "do you paint alloy
    wheels?" (a paint job). The presence of a painting word decides it.
    """
    # A scratch is bodywork/detailing even when the word "paint" appears -
    # "scratch hai but paint nahi karwana" used to pull the alloy-painting
    # FAQ. Scratches are handled by the denting/detailing route (FAQ 35/36).
    if contains_any(norm, ["scratch", "scratches", "kharoch"]) and not \
            contains_any(norm, ["rubbing", "polish", "polishing", "compounding",
                                "correction", "buffing"]):
        return Service.DENTING

    if contains_any(norm, ["alloy", "rim"]) and contains_any(
            norm, ["paint", "painting", "painted", "colour", "color",
                   "shade", "finish"]):
        return Service.PAINTING

    neg = negated_terms(norm)
    for service, vocab in SERVICE_VOCAB:
        if contains_any(norm, vocab):
            # "ceramic nahi, ppf karwana hai": ceramic is ruled out - skip it
            # and let the next service (PPF) win.
            evidence = [p for p in vocab if contains_any(norm, [p])]
            if neg and all(any(w in neg for w in p.split()) for p in evidence):
                continue
            # "kitna time lagega": the word "time" alone is not an hours
            # question when the message asks how long a job takes.
            if service == Service.HOURS and contains_any(norm, DURATION_WORDS) \
                    and not contains_any(norm, HOURS_STRONG_WORDS):
                continue
            return service
    return None


# ===========================================================================
# INTENT VOCABULARY
# ===========================================================================
PRICE_WORDS = ["price", "budget", "cheap", "cheapest", "expensive", "emi",
               "discount", "offer", "quote", "estimate", "how much",
               "kitna", "kitne", "kitni", "kharcha", "kharch", "kharche",
               "paisa", "paise", "rupaye", "start hai", "se start"]
WARRANTY_WORDS = ["warranty", "guarantee", "claim", "valid",
                  # durability: "how many years does it last" - no approved
                  # answer states a lifespan, so this must escalate too
                  "how long does it last", "how long will it last", "last long",
                  "does ceramic last", "does ppf last", "does it last",
                  "does coating last", "ceramic last", "ppf last", "coating last",
                  "kab tak chalega", "kitna chalega", "kitne saal chalega",
                  "kitne din chalega", "kab tak tikega",
                  "lasts", "how many years", "kitne saal", "kitne years",
                  "chalega", "chalegi", "chalta hai", "chalti hai", "lifespan",
                  "durability", "durable", "tikega", "tikegi", "tikta hai"]
DURATION_WORDS = ["time lagega", "kitna time", "how long", "how much time",
                  "kitne time", "time me ho", "kitne din", "how many days",
                  "kab tak ho jayega", "kab tak ho jayegi", "din lagenge",
                  "din lagega", "time lagenge",
                  "duration", "hours to", "same day", "kitne din",
                  "kitna time lagega", "kitne ghante", "how many days",
                  "kab tak ho jayega", "kitne din me"]
BOOKING_WORDS = ["book", "booking", "appointment", "slot", "schedule",
                 "tomorrow", "today", "come", "aana", "aaunga", "visit",
                 "reserve", "karwana hai", "karana hai", "karani hai",
                 "kal", "aaj", "aa sakta", "aa sakti", "this week",
                 "get it done", "i want to", "lagwana hai",
                 "bring my car", "when can i come", "kab aau", "kab aa",
                 "walk in", "walkin", "reschedule", "time de do",
                 "advance booking"]

# The subset that can ONLY mean "I want to come in" - checked before the
# opening-hours shortcut. Day words are deliberately excluded: "are you open
# on Sunday?" must stay an hours question.
STRONG_BOOKING_WORDS = ["book", "booking", "appointment", "slot", "schedule",
                        "aa sakta", "aa sakti", "aana hai", "aaunga",
                        "reserve", "walk in", "walkin", "bring my car",
                        "kab aau", "advance booking"]
COMPARISON_WORDS = ["vs", "versus", "difference", "diff", "compare",
                    "better", "which one"]
AVAILABILITY_WORDS = ["available", "stock", "in stock", "milega", "hai kya",
                      "do you have", "do you sell", "sell"]
PICKUP_WORDS = ["pickup", "pick up", "drop", "doorstep", "home service"]
# "What should I get?" - a request for guidance, not a specific product.
# Answering it with a random accessory FAQ ("back seat organizers for
# laptops") is exactly the keyword-soup failure this release fixes.
RECOMMEND_WORDS = ["recommend", "recommendation", "suggest", "suggestion",
                   "what should i do", "what should i get", "kya karu",
                   "kya karun", "kya karwau", "kya karwaun", "kya karna chahiye",
                   "best solution", "solution kya", "karwau ya", "ya ceramic",
                   "ya ppf",
                   "best", "which one", "konsa", "kaunsa", "kon sa",
                   "kya lena chahiye", "kya le sakta", "kya le sakti",
                   "aur kya le", "kya lu", "kya loon", "advice", "achha kya",
                   "good option", "kya better", "options kya"]
# A complaint is about OUR work, not about the car being broken. Bare
# "problem with" and "damaged" used to live here, which made "my car has a
# problem with the AC" - an ordinary service request - come back as an
# apology, and never reach the AC service answer.
COMPLAINT_WORDS = ["complaint", "shikayat", "not happy", "unhappy",
                   "bakwas", "bakwaas", "paisa barbaad", "paise barbaad",
                   "scam", "fraud", "dhokha", "dhoka", "loot", "lut gaye",
                   "faltu service", "bekaar service", "bekar service",
                   "peel ho", "peeling", "nikal gayi", "nikal gaya", "ukhad",
                   "bad service", "very bad", "service was bad", "refund",
                   "paisa wapas", "paise wapas", "cheated", "cheat", "waste",
                   "worst", "poor service", "issue with your",
                   "problem with your service", "problem with the work",
                   "you damaged", "damaged my car", "aapne kharab",
                   "kharab kar diya", "kharab kr diya", "galat kaam",
                   "ghatiya", "disappointed", "never coming back",
                   "paise waste"]
OPTIONS_WORDS = ["which ones do you have", "which one do you have",
                 "which ones", "what options", "options kya", "kya kya options",
                 "kaunse hai", "konse hai", "kaunse option", "konse option",
                 "what all do you have", "kya kya hai"]

# Live-stock phrasing. Handled deterministically, because "right now" once
# matched the "Can I call right now?" FAQ and answered about opening hours.
LIVE_STOCK_WORDS = ["in stock", "stock right now", "stock hai", "stock mein",
                    "available right now", "currently available",
                    "abhi available", "abhi hai", "ready stock",
                    "in stock right now", "stock me"]

# "Actually forget that" - drop the current topic but keep the car.
CANCEL_WORDS = ["forget that", "forget it", "leave that", "leave it",
                "cancel that", "never mind", "nevermind", "chhod do",
                "chod do", "rehne do", "rahne do", "skip that", "drop that",
                "scrap that", "ignore that"]

AFFIRM_WORDS = ["haan", "han", "hanji", "ha", "yes", "yeah", "yep", "ok",
                "okay", "sure", "theek hai", "thik hai", "done", "kar do",
                "chalega", "fine", "alright"]
# A longer message only counts as an affirmation when it STARTS with one:
# "it has 2 dents" is a dent report, "ok full body" is a choice.
_AFFIRM_FIRST = {"haan", "han", "hanji", "ha", "yes", "yeah", "yep", "ok",
                 "okay", "sure", "theek", "thik", "done", "fine", "alright"}

HUMAN_WORDS = ["human", "talk to someone", "real person", "manager",
               "owner", "staff", "agent"]

# "How to order" measured ~65 seconds because nothing recognised it and it
# fell through to the language model. It is one of the commonest opening
# messages a shop gets, and FAQ 16 already supplies the approved answer -
# so it is now handled deterministically, in about a millisecond.
ORDER_WORDS = ["how to order", "how do i order", "how can i order",
               "how to buy", "how do i buy", "how can i buy",
               "order kaise", "kaise order", "order karna hai",
               "kaise kharide", "kaise kharidu", "kaise le", "kaise milega",
               "purchase kaise", "buy kaise", "ordering", "how to purchase",
               "delivery karte ho", "home delivery", "online order",
               "order online", "shipping"]


_FIGURE_SUFFIX = re.compile(r"[0-9][0-9,.]*\s?(k|rs|rupees|rupay|rupaye|hazar|hazaar|lakh|lac|lakhs)\b")
_RS_PREFIX = re.compile(r"\b(rs|inr)\.?\s?[0-9]")
# "500 me", "999 ka", "2000 me de do" - a number used as an amount
_FIGURE_CONTEXT = re.compile(
    r"\b([0-9][0-9,.]*)\s?(me|mein|mai|ka|ki|ke|tak|se|only|de do|dedo|milega|milegi|"
    r"lagega|padega|hoga|hogi)\b")
# a year-like number is an amount only in "2000 me de do" / "2000 me?" forms,
# never in "creta 2021 me li thi"
_YEAR_AS_AMOUNT = re.compile(
    r"\b(19[5-9][0-9]|20[0-3][0-9])\s?(me|mein)(\s+(de do|dedo|milega|milegi|ho jayega|"
    r"ho jayegi|hoga|hogi|chalega|possible|karoge|kar doge|ho sakta|ho sakti|kya))*$")
_UNIT_WORDS = {"inch", "inches", "mm", "cm", "cc", "hp", "bhp", "w", "watt", "km", "kms",
               "kmpl", "ml", "litre", "liter", "l", "gm", "g", "kg", "model", "seater",
               "days", "din", "hours", "ghante", "months", "mahine", "years", "saal"}


def _year_like(num: str) -> bool:
    return len(num) == 4 and 1950 <= int(num) <= 2035


def mentions_price_figure(norm: str) -> bool:
    """Does the message quote an amount of money? "15000 me", "10k", "1 lakh",
    "rs 3000", "wiper 500 me", "mats 2000 me de do" - but not a year (creta
    2021 me li thi), a size (17 inch) or a count (2 dents)."""
    if _FIGURE_SUFFIX.search(norm) or _RS_PREFIX.search(norm) or "₹" in norm:
        return True
    flat = norm.replace(",", "")
    for m in _FIGURE_CONTEXT.finditer(flat):
        num = m.group(1).replace(".", "")
        if not num.isdigit():
            continue
        if _year_like(num):
            if _YEAR_AS_AMOUNT.search(flat):
                return True
            continue
        return True
    for m in re.finditer(r"\b([0-9]+)\b(?:\s+([a-z]+))?", flat):
        num, nxt = m.group(1), (m.group(2) or "")
        if nxt in _UNIT_WORDS:
            continue
        if _year_like(num):
            continue
        if len(num) >= 3:
            return True
    return False


def detect_intent(norm: str, service: Optional[str]) -> str:
    """Classify what the customer wants. Checked most-specific first."""
    if contains_any(norm, COMPLAINT_WORDS):
        return Intent.COMPLAINT
    if contains_any(norm, HUMAN_WORDS):
        return Intent.COMPLAINT if "complaint" in norm else Intent.OTHER

    # An explicit booking verb beats a day name. "Sunday ko aa sakta hu?"
    # (can I come on Sunday) mentions a day, so the HOURS detector claims
    # it - but the customer is asking to come in, not what time we open.
    # Only unambiguous booking words qualify; "kal" and "today" are left
    # out because they appear in genuine timings questions too.
    if contains_any(norm, STRONG_BOOKING_WORDS):
        return Intent.BOOKING_REQUEST

    # Opening-hours questions are settled here, before the price check.
    # "kitne baje tak khule ho" contains "kitne" (how much/many) but is
    # plainly a timings question, and the HOURS service detector already
    # said so.
    if service == Service.HOURS:
        return Intent.HOURS

    # A comparison is checked FIRST. "ppf ya ceramic konsa better hai" reads
    # like a request for advice, but it is asking us to compare two services
    # - and no approved answer compares them (FAQ 31, 34 are both marked
    # information-required), so it must hand over rather than improvise.
    # "Which ones do you have?" is a catalogue question, not a comparison.
    if contains_any(norm, OPTIONS_WORDS):
        return Intent.AVAILABILITY_REQUEST

    if contains_any(norm, COMPARISON_WORDS):
        return Intent.SERVICE_COMPARISON

    # A request for guidance. Checked before price/availability because
    # "kaunsa lena chahiye?" is asking us to advise, not to quote. The
    # answer must never actually say "best" - see rule 5.
    if contains_any(norm, RECOMMEND_WORDS):
        return Intent.RECOMMENDATION

    # "scratch hai but paint nahi karwana" - a scratch with painting ruled
    # out is a request for the non-paint route (compounding/polishing), i.e.
    # guidance, not a painting enquiry.
    if contains_any(norm, ["scratch", "scratches", "kharoch"]) and contains_any(
            norm, ["paint nahi", "paint nhi", "no paint", "without paint",
                   "bina paint", "don't want paint", "dont want paint"]):
        return Intent.RECOMMENDATION
    # "Can you guarantee my car will be ready tomorrow?" contains "guarantee"
    # but is asking about a slot, not a warranty. Readiness + a day = booking.
    if contains_any(norm, ["ready", "ho jayegi", "ho jayega", "mil jayegi",
                           "taiyar", "tayyar"]) and (
            extract_preferred_day(norm) or contains_any(norm, ["by", "tak"])):
        return Intent.BOOKING_REQUEST

    # "50% off chal raha hai?" - an offers question, never an availability
    # one; no approved answer states a percentage.
    if re.search("[0-9]+ off", norm) or contains_any(
            norm, ["off chal", "sale chal", "sale hai", "on sale", "deal",
                   "offer chal", "koi offer", "any offer", "offers"]):
        return Intent.OFFER_DISCOUNT
    if contains_any(norm, WARRANTY_WORDS):
        return Intent.WARRANTY
    if contains_any(norm, DURATION_WORDS):
        return Intent.DURATION
    # "ceramic coating 15000 me?" - a quoted amount is a price question, and
    # answering "yes" to it would read as accepting the figure.
    if mentions_price_figure(norm):
        return Intent.PRICE_INQUIRY
    if contains_any(norm, PICKUP_WORDS):
        return Intent.PICKUP_DROP
    if contains_any(norm, PRICE_WORDS):
        # "Any discount on accessories?" is an offers question whatever the
        # service is - and FAQ 87 answers it (discounts come with the Gold
        # Membership). Classifying it as a price question made it collide
        # with the never-guess rule and escalate on an answer we do have.
        if contains_any(norm, ["discount", "offer"]):
            return Intent.OFFER_DISCOUNT
        return Intent.PRICE_INQUIRY
    if contains_any(norm, BOOKING_WORDS):
        return Intent.BOOKING_REQUEST
    if contains_any(norm, AVAILABILITY_WORDS):
        return Intent.AVAILABILITY_REQUEST

    if service == Service.LOCATION:
        return Intent.LOCATION
    if service == Service.CONTACT:
        return Intent.CONTACT
    if service == Service.HOURS:
        return Intent.HOURS
    if service == Service.MEMBERSHIP:
        return Intent.MEMBERSHIP_INQUIRY
    if service == Service.USED_CARS:
        return Intent.USED_CAR_INQUIRY
    if service in (Service.ACCESSORIES, Service.AUDIO, Service.ALLOY_SALES):
        return Intent.ACCESSORY_INQUIRY
    if service:
        return Intent.SERVICE_INQUIRY
    return Intent.UNKNOWN


# ===========================================================================
# CAR MODEL EXTRACTION
# ===========================================================================
CAR_MODELS = [
    "creta", "seltos", "sonet", "venue", "nexon", "punch", "harrier",
    "safari", "thar", "scorpio", "xuv", "bolero", "fortuner", "innova",
    "swift", "baleno", "dzire", "wagonr", "wagon r", "alto",
    "celerio", "ertiga", "brezza", "ciaz", "i10", "i20", "verna", "aura",
    "compass", "jeep", "city", "amaze", "jazz", "polo", "virtus", "slavia",
    "kushaq", "altroz", "tiago", "tigor", "kwid", "triber", "duster",
    "bmw", "audi", "mercedes", "endeavour", "hector", "astor",
    # Added after customers were answered about the wrong car: "Skoda
    # octavia mat" was replied to with a SONET answer partly because
    # octavia was not recognised at all.
    "octavia", "superb", "rapid", "kodiaq", "taigun", "tiguan", "vento",
    "ameo", "carens", "carnival", "sonata", "elantra", "exter", "alcazar",
    "grand i10", "santro", "eon", "xcent", "figo", "ecosport", "aspire",
    "curvv", "nexon ev", "tata punch", "marazzo", "xuv700", "xuv300",
    "thar roxx", "gurkha", "jimny", "fronx", "grand vitara", "invicto",
    "hyryder", "glanza", "urban cruiser", "civic", "wrv", "elevate",
    "kicks", "magnite", "sunny", "octavia rs", "legender", "hilux",
    "camry", "corolla", "etios", "yaris", "verito", "lodgy", "captur",
]

# Make names, so "Skoda octavia" and a bare "Skoda" both register.
CAR_MAKES = [
    "maruti", "suzuki", "hyundai", "tata", "mahindra", "toyota", "honda",
    "kia", "skoda", "volkswagen", "vw", "renault", "nissan", "ford",
    "jeep", "mg", "bmw", "audi", "mercedes", "volvo", "isuzu", "force",
]

CAR_BRANDS = {
    "creta": "Hyundai", "venue": "Hyundai", "i10": "Hyundai",
    "i20": "Hyundai", "verna": "Hyundai", "aura": "Hyundai",
    "seltos": "Kia", "sonet": "Kia", "carens": "Kia",
    "nexon": "Tata", "punch": "Tata", "harrier": "Tata", "safari": "Tata",
    "altroz": "Tata", "tiago": "Tata", "tigor": "Tata",
    "thar": "Mahindra", "scorpio": "Mahindra", "xuv": "Mahindra",
    "bolero": "Mahindra",
    "fortuner": "Toyota", "innova": "Toyota",
    "swift": "Maruti", "baleno": "Maruti", "dzire": "Maruti",
    "wagonr": "Maruti", "alto": "Maruti", "celerio": "Maruti",
    "ertiga": "Maruti", "brezza": "Maruti", "ciaz": "Maruti",
    "compass": "Jeep", "jeep": "Jeep",
    "city": "Honda", "amaze": "Honda", "jazz": "Honda",
    "bmw": "BMW", "audi": "Audi", "mercedes": "Mercedes",
}

_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")

# --- Preferred day ---------------------------------------------------------
# Captured for the LEAD record only, so the team can see when the customer
# wanted to come. It is never echoed back as a confirmed appointment.
DAY_WORDS = {
    "today": "today", "aaj": "today", "abhi": "today",
    "tomorrow": "tomorrow", "kal": "tomorrow", "kl": "tomorrow",
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday",
    "sunday": "Sunday", "weekend": "weekend", "this week": "this week",
    "next week": "next week",
}


def extract_preferred_day(norm: str) -> Optional[str]:
    """The day the customer mentioned, if any. Not a confirmed booking."""
    for word, label in DAY_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", norm):
            return label
    return None


# Longest first, so "grand i10" wins over "i10" and "wagon r" over "r".
_CAR_MODELS_SORTED = sorted(CAR_MODELS, key=len, reverse=True)


# Words that name a car, never a topic - kept out of FAQ similarity.
_CAR_TOKENS = {w for m in CAR_MODELS for w in m.split()} | set(CAR_MAKES)


# How a model is written back to the customer ("Hyundai I20" reads wrong).
_MODEL_DISPLAY = {"i10": "i10", "i20": "i20", "bmw": "BMW", "xuv700": "XUV700",
                  "xuv300": "XUV300", "wagonr": "WagonR", "kwid": "Kwid",
                  "mg hector": "MG Hector", "vw": "VW"}


def extract_car(norm: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (brand, model, year) mentioned in the message, if any."""
    model = None
    neg = negated_terms(norm)
    for m in _CAR_MODELS_SORTED:
        if re.search(r"\b" + re.escape(m) + r"\b", norm) and not any(
                w in neg for w in m.split()):
            model = m                     # "not fortuner, innova" -> innova
            break

    year_match = _YEAR.search(norm)
    year = year_match.group(1) if year_match else None

    brand = CAR_BRANDS.get(model) if model else None
    if not brand:
        # Fall back to an explicitly named make ("Skoda octavia", "my BMW").
        for mk in CAR_MAKES:
            if re.search(r"\b" + re.escape(mk) + r"\b", norm):
                brand = mk.title() if mk not in ("vw", "mg") else mk.upper()
                break
    return brand, (_MODEL_DISPLAY.get(model, model.title()) if model else None), year


def car_models_in(text: str) -> set:
    """Every car model named in a piece of text, lowercased.

    Used to stop an approved answer about one car being sent to the owner
    of another - the "Skoda octavia mat" -> "mats for Sonet" failure.
    """
    low = normalise(text)
    return {m for m in CAR_MODELS
            if re.search(r"\b" + re.escape(m) + r"\b", low)}


# ===========================================================================
# BUYING INTENT
# ===========================================================================
HIGH_INTENT = ["i want", "chahiye", "karwana hai", "karana hai", "karani hai",
               "karwani hai", "karvana hai", "lagwani hai", "karwa lo",
               "chaiye", "leni hai", "lena hai",
               "book", "booking", "tomorrow", "today", "kal", "aaj",
               "kab aa", "when can i come", "how can i get", "install",
               "lagwana", "this week", "abhi", "ready to", "confirm"]
MEDIUM_INTENT = ["price", "quote", "estimate", "available", "stock",
                 "how much", "discount"]


def detect_buying_intent(norm: str, intent: str) -> str:
    if contains_any(norm, HIGH_INTENT) or intent == Intent.BOOKING_REQUEST:
        return "HIGH"
    if contains_any(norm, MEDIUM_INTENT) or intent in (
            Intent.PRICE_INQUIRY, Intent.AVAILABILITY_REQUEST,
            Intent.ACCESSORY_INQUIRY):
        return "MEDIUM"
    return "LOW"


# ===========================================================================
# FAQ SEMANTIC MATCHING
# ===========================================================================
# Not an embedding model - a weighted overlap score. It is deterministic,
# instant, needs no extra dependency, and is easy to debug when a customer
# gets the wrong answer.
#
#   token overlap   how many meaningful words the two share
#   service agree   both about PPF, both about hours, ...
#   intent agree    both a price question, both a warranty question, ...
#
# Service and intent agreement are weighted heavily because "ppf price" and
# "ppf warranty" share the word "ppf" but need completely different answers.

_FAQ_TOKENS: List[Tuple[Dict[str, Any], set]] = []

# How informative each word is. A word appearing in many FAQ questions -
# "mat", "available", "price", "car" - says almost nothing about WHICH FAQ
# the customer means. A word appearing in one - "blaupunkt", "extinguisher" -
# is nearly decisive.
#
# This is the fix for the headline bug. "Skoda octavia mat" matched the
# SONET mats answer on the single shared word "mat", and "virtus armrest
# available?" matched GLASS COATING on the single shared word "available",
# because every shared word used to count the same.
_TOKEN_WEIGHT: Dict[str, float] = {}


def _build_faq_index() -> None:
    _FAQ_TOKENS.clear()
    for faq in kb.APPROVED_FAQS:
        _FAQ_TOKENS.append((faq, set(tokens(faq["question"]))))

    # Inverse document frequency, normalised to roughly 0.1 - 1.0.
    total = len(_FAQ_TOKENS) or 1
    counts: Dict[str, int] = {}
    for _, toks in _FAQ_TOKENS:
        for t in toks:
            counts[t] = counts.get(t, 0) + 1

    _TOKEN_WEIGHT.clear()
    for token, n in counts.items():
        # log-scaled so a word in 1 FAQ scores ~1.0 and a word in 12 scores
        # ~0.25, rather than falling off a cliff.
        _TOKEN_WEIGHT[token] = max(0.08, math.log(total / n) / math.log(total))


_build_faq_index()


def token_weight(token: str) -> float:
    """Weight for a word. Unseen words are distinctive by definition."""
    return _TOKEN_WEIGHT.get(token, 1.0)


def score_faq(faq: Dict[str, Any], faq_tokens: set, msg_tokens: set,
              service: Optional[str], intent: str) -> float:
    if not msg_tokens:
        return 0.0

    overlap = msg_tokens & faq_tokens
    if not overlap:
        # No shared word at all means no match, however well the service
        # tags happen to line up. Without this, "do you have branches?"
        # scored 0.45 against every location FAQ purely on the service
        # bonus and was answered with the shop address.
        return 0.0

    # WEIGHTED overlap. Sharing "available" is weak evidence; sharing
    # "extinguisher" is strong. The unweighted version treated them alike,
    # which is what sent "virtus armrest available?" to the glass-coating
    # answer.
    shared = sum(token_weight(t) for t in overlap)
    msg_mass = sum(token_weight(t) for t in msg_tokens) or 1.0
    faq_mass = sum(token_weight(t) for t in faq_tokens) or 1.0
    dice = (2 * shared) / (msg_mass + faq_mass)

    # MINIMUM EVIDENCE. At least one genuinely distinctive word must be
    # shared, or the whole match rests on filler. 0.45 is above the weight
    # of the commonest FAQ words and below that of a brand or product name.
    if max(token_weight(t) for t in overlap) < 0.45:
        return 0.0

    score = dice * 1.0

    same_service = faq["service"] == service and service is not None
    if same_service:
        score += 0.45

    # The intent bonus only counts when the services agree (or when the
    # message named no service at all). Agreeing on intent ACROSS different
    # services means nothing: "parking milegi kya" and "glass coating
    # available?" are both availability questions and share the word
    # "available", which was enough to answer a parking question with a
    # coating answer.
    if faq["intent"] == intent and (same_service or service is None):
        score += 0.35
    # A specific missing-info entry must be able to beat its confirmed
    # sibling: "creta ppf cost" should reach FAQ 20, not FAQ 18. The nudge
    # is deliberately small - the shared word "creta" already does most of
    # the work, and a larger bonus made vague questions escalate wrongly.
    if not faq["confirmed"] and faq["service"] == service and \
            faq["intent"] == intent:
        score += 0.05
    return score


# Questions where being wrong is worse than admitting we do not know. An
# approved answer is only accepted for these when the FAQ is about the SAME
# thing - a "what is the Gold Membership" answer must never be served to
# "how long is the membership valid?".
NEVER_GUESS = {Intent.PRICE_INQUIRY, Intent.WARRANTY, Intent.DURATION,
               Intent.SERVICE_COMPARISON, Intent.OFFER_DISCOUNT,
               Intent.PICKUP_DROP}


_FAQ_PRODUCT_CACHE: Dict[int, Optional[str]] = {}


def _faq_product(faq: Dict[str, Any]) -> Optional[str]:
    """Which product an approved FAQ is about, if any. Cached per FAQ."""
    fid = faq["id"]
    if fid not in _FAQ_PRODUCT_CACHE:
        text = normalise((faq["question"] or "") + " " + (faq["answer"] or ""))
        _FAQ_PRODUCT_CACHE[fid] = detect_product(text)
    return _FAQ_PRODUCT_CACHE[fid]


def service_intro(service: Optional[str]) -> str:
    """The approved one-liner that says we do this service.

    Used when a continuation has nothing remembered for its topic - the
    first turn was an escalation ("warranty?") - so "full body PPF hoga na?"
    still gets "we do complete PPF covering..." instead of an empty body.
    """
    if not service:
        return ""
    family = ([Service.DENTING, Service.PAINTING]
              if service in (Service.DENTING, Service.PAINTING) else [service])
    for svc in ([service] + [x for x in family if x != service]):
        for want in (Intent.SERVICE_INQUIRY, Intent.GENERAL_INFORMATION):
            for faq in kb.APPROVED_FAQS:
                q = (faq["question"] or "").lower()
                if (faq["confirmed"] and faq["service"] == svc
                        and faq["intent"] == want and faq["answer"]
                        and not contains_any(q, ["alloy", "caliper", "rim"])):
                    return faq["answer"]
    return ""


def verified_price_faq(service: Optional[str],
                       asked_model: Optional[str]) -> Optional[Dict[str, Any]]:
    """The approved answer that actually STATES a price - only two exist.

    Gold Membership is a fixed Rs 2,999. Used cars have a verified floor
    (from Rs 99,000) that holds only while no particular model is asked
    about. Every other price is unknown here and must escalate.
    """
    if service == Service.MEMBERSHIP:
        needle, want = "2,999", Intent.MEMBERSHIP_INQUIRY
    elif service == Service.USED_CARS and not asked_model:
        needle, want = "99,000", Intent.USED_CAR_INQUIRY
    else:
        return None
    for faq in kb.APPROVED_FAQS:
        if (faq["confirmed"] and faq["service"] == service
                and faq["intent"] == want and needle in (faq["answer"] or "")):
            return faq
    return None


def used_model_in_approved_text(model: str) -> bool:
    """Is this model named in a confirmed used-car answer ("models like Creta")?"""
    return any(model.lower() in (f["answer"] or "").lower()
               for f in kb.APPROVED_FAQS
               if f["confirmed"] and f["service"] == Service.USED_CARS)


def faq_is_eligible(faq: Dict[str, Any], intent: str,
                    service: Optional[str] = None,
                    customer_car: Optional[str] = None,
                    customer_product: Optional[str] = None,
                    asked_car: Optional[str] = None) -> bool:
    """Should this FAQ be allowed to answer a question of this intent?

    Two guards, both learned from real mis-answers during testing:

    1. On a never-guess question, a CONFIRMED answer about something else
       must not be used. "How long is the membership valid?" matching the
       general membership answer would imply a validity period that answer
       does not contain.

    2. A MISSING-INFORMATION entry must not escalate a question the
       customer did not ask. "dent theek karwana hai" (please fix my dent)
       matched the missing "how long does a dent take" entry - but nobody
       asked how long, so there was nothing to escalate.
    """
    if intent in NEVER_GUESS and faq["confirmed"] and faq["intent"] != intent:
        # Two services DO have verified prices in their approved answers -
        # the Gold Membership (Rs 2,999) and used cars (from Rs 99,000).
        # A price question about them must be answered from those, not
        # escalated as unknown.
        # ...but the used-car figure is a FLOOR, so a price question that
        # names a specific model ("second hand creta ka price") still has
        # no verified answer and escalates.
        if intent == Intent.PRICE_INQUIRY and service == faq["service"] and (
                faq["service"] == Service.MEMBERSHIP
                or (faq["service"] == Service.USED_CARS and not asked_car)):
            return True
        return False

    # 3. A booking question may only be answered by an FAQ about the SAME
    #    service. "kal ka slot mil jayega?" (can I get a slot tomorrow)
    #    otherwise matched "neck rest pillows" on the shared word
    #    "available" and answered with the accessories catalogue.
    if intent == Intent.BOOKING_REQUEST and faq["service"] != service:
        return False

    # 5. NEVER answer about a different PRODUCT than the customer named.
    #    "Seltos GFX mat available?" matched the neck-rest-pillow answer on
    #    the shared word "available". Same failure as the wrong-car one,
    #    one level down.
    if customer_product:
        faq_product = _faq_product(faq)
        if faq_product and faq_product != customer_product:
            return False

    # 4. NEVER answer about a different car than the customer named.
    #    "Skoda octavia mat" was answered with "mats for Sonet in stock" -
    #    the worst class of mistake here, because it reads as a confident,
    #    specific answer about somebody else's car.
    #
    #    Only the ANSWER is scanned: FAQ 60's question mentions a Creta but
    #    its answer says "for all models", which is fine for anyone. And an
    #    answer listing three or more cars is a general list (FAQ 88's used
    #    car stock), not a claim about one specific car.
    answer_cars = car_models_in((faq["answer"] or "") + " " + (faq["note"] or ""))
    if 1 <= len(answer_cars) <= 2:
        if not customer_car or customer_car.lower() not in answer_cars:
            return False
    # An unconfirmed entry's only content is an intent-specific note, so it
    # may only ever answer its OWN intent. Without this, "ceramic coating
    # price?" surfaced FAQ 31's comparison note to a price question.
    if not faq["confirmed"] and faq["intent"] != intent:
        return False
    return True


def match_faq(message: str, service: Optional[str], intent: str,
              customer_car: Optional[str] = None,
              customer_product: Optional[str] = None,
              asked_car: Optional[str] = None,
              ) -> Tuple[Optional[Dict[str, Any]], float]:
    """Best-matching ELIGIBLE FAQ and its score.

    Eligibility is applied inside the loop rather than to the winner
    afterwards. Filtering afterwards threw away the whole match and fell
    through to the language model - so "I want PPF for my Creta this week"
    lost its perfectly good PPF answer because the top scorer happened to
    be the missing "Creta PPF price" entry.
    """
    # A word the customer ruled out ("ceramic nahi, PPF karwana hai") must
    # not pull the FAQ that happens to mention it.
    msg_norm = normalise(message)
    msg_tokens = set(tokens(message)) - {
        _singular(w) for w in negated_terms(msg_norm)}
    # "scorpio ka full paint job" must not pull the alloy-painting answer:
    # an FAQ about alloys or calipers needs the customer to have named them.
    wheels_asked = customer_product == "alloy_wheels" or contains_any(
        msg_norm, ["alloy", "alloys", "rim", "rims", "caliper", "calipers",
                   "wheel", "wheels"])
    best, best_score = None, 0.0
    for faq, faq_tokens in _FAQ_TOKENS:
        if not faq_is_eligible(faq, intent, service, customer_car,
                               customer_product, asked_car):
            continue
        if not wheels_asked and contains_any((faq["question"] or "").lower(),
                                             ["alloy", "caliper"]):
            continue
        s = score_faq(faq, faq_tokens, msg_tokens, service, intent)
        if s > best_score:
            best, best_score = faq, s
    if best_score < config.FAQ_MATCH_THRESHOLD:
        return None, best_score
    return best, best_score


# ===========================================================================
# KNOWLEDGE GAP NORMALISATION
# ===========================================================================
# "PPF warranty?", "PPF ki warranty kya hai?" and "PPF kitne saal ki
# warranty?" must collapse to ONE dashboard row, so the topic is built from
# the classified service and intent rather than from the raw wording.
GAP_INTENT_LABEL = {
    Intent.PRICE_INQUIRY: "price",
    Intent.WARRANTY: "warranty",
    Intent.DURATION: "duration / time required",
    Intent.SERVICE_COMPARISON: "comparison",
    Intent.AVAILABILITY_REQUEST: "availability",
    Intent.BOOKING_REQUEST: "booking",
    Intent.PICKUP_DROP: "pickup and drop",
    Intent.OFFER_DISCOUNT: "discounts",
    Intent.MEMBERSHIP_INQUIRY: "membership details",
    Intent.GENERAL_INFORMATION: "general information",
}


def gap_topic(service: Optional[str], intent: str) -> str:
    svc = (service or "GENERAL").replace("_", " ").title()
    label = GAP_INTENT_LABEL.get(intent, intent.replace("_", " ").lower())
    return f"{svc} - {label}"


# ===========================================================================
# DETERMINISTIC REPLIES for the three facts asked most often
# ===========================================================================
LOCATION_REPLY = (
    f"{BUSINESS['name']}, {BUSINESS['address']}. "
    f"Google Maps: {BUSINESS['maps_link']} - Call/WhatsApp: {PHONE}."
)

# --- The former location ---------------------------------------------------
# Mansarovar is where we USED to be listed. These words exist so the bot can
# RECOGNISE a customer asking about the old place; the reply always gives the
# current Dholai address. Mansarovar is never stated as where we are, and the
# wording deliberately does not claim a move - it simply answers with the one
# verified address.
FORMER_AREA_WORDS = ["mansarovar", "mansarowar", "mansarover", "scot temple"]

FORMER_AREA_REPLY = (
    f"Our workshop is at {BUSINESS['address']}. "
    f"Google Maps: {BUSINESS['maps_link']} - Call/WhatsApp {PHONE} and "
    "we'll guide you in."
)
HOURS_REPLY = (
    f"{BUSINESS['hours_sentence']} You can call or WhatsApp us on {PHONE}."
)
CONTACT_REPLY = (
    f"You can reach us on {PHONE} - the same number works for calls and "
    f"WhatsApp. {BUSINESS['hours_sentence']}"
)
OFFLINE_REPLY = (
    f"Thanks for messaging {BUSINESS['name']}! Please call or WhatsApp us "
    f"at {PHONE} and our team will help you right away."
)

# --- How to order ----------------------------------------------------------
# Built only from verified facts: FAQ 16 (no website, WhatsApp us directly
# for queries or bookings), the address, and the opening hours. It claims no
# delivery, no shipping and no online ordering, because none of those are
# verified - and FAQ 16 states we have no website at all.
ORDER_REPLY = (
    f"Ordering is simple - WhatsApp or call us on {PHONE} with your car "
    "model and what you need, and our team will confirm the option and "
    f"price for you. You can also visit us at {BUSINESS['address_short']}. "
    f"{BUSINESS['hours_sentence']}"
)


# --- Booking ---------------------------------------------------------------
# We do NOT know slot availability, lead times, or how far ahead we take
# work - all of that is on the information-required list. So these replies
# say only what is verified: bookings go through WhatsApp or a call (FAQ 16
# confirms "WhatsApp us directly for any queries or bookings"), and our
# opening hours.
#
# Note the careful wording: "our team will confirm your slot" - the bot
# never says a slot IS booked or IS available, because nothing here can
# actually reserve one.
#
# A booking request is NOT flagged as a human escalation. Escalation means
# "the bot could not answer"; here it answered correctly. Booking requests
# are tracked separately through booking_status, lead_status and the
# BOOKING_REQUESTED resolution, which is what feeds the dashboard's own
# "Booking requests" figure. Folding them into the escalation count would
# make the escalation rate meaningless, since half of all sales messages
# contain "karwana hai".
BOOKING_REPLY = (
    f"We take bookings directly on WhatsApp or by phone at {PHONE} - we "
    "don't have online slot booking. Send us your car model, what you'd "
    "like done and your preferred day, and our team will confirm the slot "
    f"with you. {BUSINESS['hours_sentence']}"
)

# The shorter version, appended when an approved answer already explained
# the service itself.
BOOKING_LINE = (
    f"To book, WhatsApp or call us on {PHONE} with your car model and "
    "preferred day - our team will confirm your slot."
)


def booking_line(car_model: Optional[str] = None) -> str:
    """The booking sentence, without asking for what we already know.

    When the customer has already named their car, asking for "your car
    model" again makes the reply both longer and slightly rude.
    """
    if car_model:
        return (f"To book your {car_model} in, WhatsApp or call us on "
                f"{PHONE} with your preferred day and our team will confirm "
                "the slot.")
    return BOOKING_LINE


# ===========================================================================
# THE SYSTEM PROMPT
# ===========================================================================
SYSTEM_PROMPT: str = f"""You are the Instagram DM customer-support representative for {BUSINESS['name']}, a one-stop car workshop and accessories warehouse in Jaipur. {BUSINESS['tagline']}.

=== VERIFIED FACTS (the only facts you may state) ===
Address        : {BUSINESS['address']}
Phone/WhatsApp : {PHONE}
Opening hours  : {BUSINESS['hours_sentence']}
Brands         : {BUSINESS['brands']}
Website        : WE DO NOT HAVE ONE. All enquiries go through WhatsApp.

=== WHAT WE DO ===
PPF and ceramic coating; polishing, compounding and paint correction; car
wash and detailing; denting and painting in our in-house Jetstar paint
booth; alloy wheel painting and gold caliper painting; brand-new alloy
wheels on advance order; full mechanical service including engine, brakes,
AC, catalytic converter and O2 sensor cleaning; wheel alignment, balancing
and puncture repair; suspension work; spare parts; car accessories and
audio; used cars; and the Rs 2,999 Gold Membership.

=== RULES ===
1. NEVER state a price. You do not have the price list. Ask for the car
   model and point the customer to WhatsApp {PHONE}.
2. NEVER state a warranty period, a service duration, stock levels, booking
   slots, membership validity, parking or the number of branches. You do
   not know any of these. Say the team will confirm on {PHONE}.
3. NEVER invent a product, brand, service or offer that is not listed above.
4. NEVER say we cannot do something and NEVER refer a customer to another
   garage. If unsure, say our team will confirm.
5. NEVER promise more than is true. "Minor scratches can be removed" must
   not become "all scratches will be removed". "We frequently stock Creta"
   must not become "we have a Creta available right now".
6. Reply in the customer's language: English to English, Hindi to Hindi,
   natural Hinglish to Hinglish.
7. Keep it to 1-3 short sentences. Plain text, no markdown, no bullet
   lists, at most one emoji. This is an Instagram DM, not an email.
8. You are the shop's support representative. Never claim to be a human
   being, and never claim to have inspected the customer's car.
9. Be helpful and warm, never pushy. No fake urgency, no fake discounts.
"""


# ===========================================================================
# HALLUCINATION FILTER  (brief section 18)
# ===========================================================================
# Applied to LANGUAGE-MODEL output only. Composed and approved replies are
# built from verified text and need no filtering.
#
# The model is small and, told not to invent, still does. So anything it
# says about price, stock, warranty, duration, discounts or a website is
# treated as unsupported unless it is one of the two approved figures, and
# the whole reply is dropped in favour of a safe one. Removing a sentence
# mid-paragraph tends to leave a mangled reply; refusing the whole thing is
# honest and predictable.

_AI_MONEY = re.compile(r"(?:rs[.]?|inr|₹)\s*[\d,]+|[\d,]{3,}\s*(?:rupees|rs|/-)", re.I)
_AI_WARRANTY = re.compile(
    r"\b\d+\s*(?:year|yr|saal|month|mahine)s?\b.{0,40}(?:warranty|guarantee)"
    r"|(?:warranty|guarantee).{0,40}\b\d+\s*(?:year|yr|saal|month)", re.I)
_AI_DURATION = re.compile(
    r"\b\d+\s*(?:hour|hrs?|day|days|din|ghante|minute|min)s?\b", re.I)
_AI_STOCK = re.compile(
    r"(?:currently (?:in stock|have)|right now we have|in stock right now|"
    r"we have (?:it|one) available|available right now|is in stock)", re.I)
_AI_WEBSITE = re.compile(r"(?:our website|www\.|https?://|online store|order online)", re.I)
_AI_DISCOUNT = re.compile(r"\d+\s*%|percent off|flat \d+|special offer|combo offer", re.I)

# The only figures any approved source states.
_APPROVED_FIGURES = {"2999", "2,999", "9000", "9,000", "2300", "2,300",
                     "99000", "99,000", "30", "40", "6367857737"}


# The model once answered "Same for mine?" by reciting its own instructions.
_AI_LEAK = re.compile(
    r"===|verified facts|rules you must|system prompt|customer-support "
    r"representative for|the only facts you may|never state a price|"
    r"you are the instagram", re.I)

_AI_NEGATIVE = re.compile(
    r"we (?:don't|do not|dont) (?:offer|have|provide|sell|give|do) "
    r"|not available|no discount|no offer", re.I)


def ai_reply_is_safe(reply: str) -> Tuple[bool, str]:
    """(safe, reason). False means do not send this to a customer."""
    for m in _AI_MONEY.finditer(reply):
        figure = re.sub(r"[^\d]", "", m.group(0))
        if figure and figure not in {f.replace(",", "") for f in _APPROVED_FIGURES}:
            return False, f"unapproved price figure {figure!r}"
    if _AI_WARRANTY.search(reply):
        return False, "stated a warranty period"
    if _AI_STOCK.search(reply):
        return False, "claimed live stock"
    if _AI_WEBSITE.search(reply):
        return False, "referred to a website or online ordering"
    if _AI_DISCOUNT.search(reply):
        return False, "stated a discount or offer"
    low = reply.lower()
    if _AI_DURATION.search(reply) and not any(
            ok in low for ok in ("10:00", "8:00", "7 days", "3 free",
                                 "6 free", "2 free")):
        return False, "stated a duration"
    if _AI_LEAK.search(reply):
        return False, "leaked the system prompt / internal rules"
    if _AI_NEGATIVE.search(reply):
        return False, "asserted something we do NOT offer/have"
    return True, ""


# ===========================================================================
# OLLAMA CLIENT
# ===========================================================================
def ask_ollama(user_message: str, context_note: str = "") -> Tuple[str, bool]:
    """Ask the local model. Returns (reply, succeeded).

    succeeded is False when Ollama could not be reached, so the caller can
    fall back to the offline reply and mark the conversation unresolved.
    """
    prompt = user_message
    if context_note:
        prompt = f"{context_note}\n\nCustomer's message: {user_message}"

    body = {
        "model": config.OLLAMA_MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        # num_predict is the single biggest lever on latency here: the
        # model streams tokens one at a time on CPU, so asking for fewer
        # is directly faster. A DM answer needs 1-3 sentences.
        # num_ctx is capped too - the default 2048-token window costs
        # prompt-processing time we do not need for a one-line question.
        "options": {"temperature": 0.6, "num_predict": 90, "num_ctx": 1024,
                    "top_k": 20, "top_p": 0.9},
    }
    try:
        r = requests.post(config.OLLAMA_URL, json=body,
                          timeout=config.OLLAMA_TIMEOUT)
        r.raise_for_status()
        reply = (r.json().get("response") or "").strip()
        return (reply, True) if reply else (OFFLINE_REPLY, False)
    except requests.exceptions.ConnectionError:
        print(f"[OLLAMA] cannot reach {config.OLLAMA_URL} - is it running?")
    except requests.exceptions.Timeout:
        print("[OLLAMA] timed out")
    except ValueError:
        print("[OLLAMA] response was not JSON - is stream set to False?")
    except Exception as e:            # never let the bot die on a model error
        print(f"[OLLAMA] unexpected error: {e!r}")
    return OFFLINE_REPLY, False


# ===========================================================================
# THE RESULT OBJECT
# ===========================================================================
@dataclass
class Answer:
    """Everything the pipeline worked out. Only `reply` reaches the customer."""
    reply: str
    source: str                      # MANAGER | FAQ | RULE | AI | ESCALATION
    service: Optional[str] = None
    intent: str = Intent.UNKNOWN
    buying_intent: str = "LOW"
    confidence: float = 0.0
    faq_id: Optional[int] = None
    escalated: bool = False
    covered: bool = False            # was the knowledge base able to answer?
    resolution: str = Resolution.ANSWERED
    core_reply: str = ""             # the approved text BEFORE car/booking decoration
    keep_core: bool = True           # False: this reply must not become topic memory
    car_brand: Optional[str] = None
    car_model: Optional[str] = None
    car_year: Optional[str] = None
    product: Optional[str] = None
    upsold: List[str] = field(default_factory=list)
    preferred_day: Optional[str] = None
    # Dialogue state. answer_to_pending is True when this message filled a
    # slot the bot had asked for; issue is the customer's stated problem;
    # next_slot is what the bot is now waiting on (car_model / car_year).
    answer_to_pending: bool = False
    clear_topic: bool = False
    message_type: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)
    frame: Any = None
    issue: Optional[str] = None
    next_slot: Optional[str] = None
    own_intent: bool = True
    gap_topic: Optional[str] = None
    context_used: Dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# CONVERSATION CONTEXT
# ===========================================================================
def build_context(conversation_id: Optional[str]) -> Dict[str, Any]:
    """Load the conversation's memory.

    The persisted dialogue STATE is authoritative. Scanning old messages is
    only a fallback for conversations that pre-date structured state; it
    never overrides state, because a scan of "Creta ... Swift ... Alto"
    cannot know which car the customer meant last.
    """
    ctx: Dict[str, Any] = {"car_model": None, "car_brand": None,
                           "car_year": None, "service": None,
                           "product": None, "booking": False, "state": {}}
    if not conversation_id:
        return ctx

    try:
        ctx["state"] = db.get_state(conversation_id) or {}
    except Exception as error:
        print(f"[STATE] could not load state: {error!r}")

    st = ctx["state"]
    if st:                      # any stored state row: no message scan, ever
        ctx["car_model"], ctx["car_brand"] = st.get("car_model"), st.get("car_brand")
        ctx["car_year"], ctx["service"] = st.get("car_year"), st.get("service")
        ctx["product"] = st.get("product")
        ctx["booking"] = st.get("intent") == Intent.BOOKING_REQUEST
        return ctx

    # Legacy fallback: no structured state yet for this conversation.
    for text in db.recent_customer_messages(conversation_id, limit=8):
        norm = normalise(text)
        brand, model, year = extract_car(norm)
        if model:
            ctx["car_model"], ctx["car_brand"] = model, brand
        if year:
            ctx["car_year"] = year
        svc = detect_service(norm)
        if svc and svc not in (Service.LOCATION, Service.CONTACT,
                               Service.HOURS):
            ctx["service"] = svc
        prod = detect_product(norm)
        if prod:
            ctx["product"] = prod
        if contains_any(norm, STRONG_BOOKING_WORDS):
            ctx["booking"] = True
    return ctx


# ===========================================================================
# PERCEPTION - everything ONE message says, with no memory involved
# ===========================================================================
OWNERSHIP_WORDS = ["already have", "already got", "already done",
                   "already ceramic", "already ppf", "pehle se", "lagi hui",
                   "lagwa rakha", "karwa rakha", "karwaya hua", "have ppf",
                   "have ceramic", "ppf hai", "ceramic hai", "lagi hai",
                   "i have ppf", "i have ceramic", "mere paas", "meri car pe"]
WANT_WORDS = ["want", "chahiye", "karwani", "karwana", "karana", "need",
              "lagwani", "lagwana", "karwau", "karwaun", "book", "price",
              "kitna", "kitne", "cost", "rate"]
BOTH_WORDS = ["both", "dono", "donon", "together", "ek saath", "saath me",
              "saath mein", "over it", "on top"]


_OWNED_PHRASES = {
    Service.PPF: ["have ppf", "ppf hai", "already ppf", "ppf already", "ppf laga",
                  "ppf lagi", "ppf lagwa", "pehle se ppf", "ppf done", "ppf karwa rakha",
                  "ppf karwaya", "got ppf", "ppf installed"],
    Service.CERAMIC: ["have ceramic", "ceramic hai", "already ceramic", "ceramic already",
                      "ceramic laga", "ceramic lagi", "ceramic karwa rakha", "pehle se ceramic",
                      "ceramic done", "ceramic karwaya", "got ceramic", "ceramic coated",
                      "ceramic ho rakhi", "ceramic ho rakha"],
}


def owned_service(norm: str) -> Optional[str]:
    """Which coating the customer says is ALREADY on the car, if any."""
    for svc, phrases in _OWNED_PHRASES.items():
        if contains_any(norm, phrases):
            return svc
    return None


def perceive(message: str) -> dialogue.Perception:
    """What THIS message says on its own. No state is consulted here."""
    norm = normalise(message)
    product = detect_product(norm)
    service = detect_service(norm)
    # "ok kal 11 baje, gfx pro bhi": the o'clock word made it look like an
    # hours question, but a named product or problem IS the topic - the
    # side word is incidental and must never become the stored service.
    if service in (Service.LOCATION, Service.HOURS, Service.CONTACT)             and (product or extract_issue(norm)):
        service = None
    intent = detect_intent(norm, service)
    brand, model, year = extract_car(norm)
    _neg = negated_terms(norm)
    p = dialogue.Perception(
        text=message, norm=norm, n_tokens=len(tokens(message)),
        service=service, product=product, intent=intent,
        brand=brand, model=model, year=year,
        issue=extract_issue(norm), day=extract_preferred_day(norm),
        language="hi" if _hinglish(norm) else "en",
        is_affirm=(contains_any(norm, AFFIRM_WORDS)
                   and (len(norm.split()) <= 3
                        or norm.split()[0] in _AFFIRM_FIRST)),
        is_cancel=contains_any(norm, CANCEL_WORDS),
        is_side=(service in (Service.LOCATION, Service.CONTACT, Service.HOURS)
                 or intent in (Intent.LOCATION, Intent.CONTACT, Intent.HOURS)),
        is_complaint=(intent == Intent.COMPLAINT),
        is_hours_question=(contains_any(norm, HOURS_STRONG_WORDS)
                           or contains_any(message.lower(),
                                           ["timing", "timings", "baje tak",
                                            "kitne baje", "kab tak"])),
        services=tuple(
            svc for svc, vocab in SERVICE_VOCAB
            if contains_any(norm, vocab) and not any(
                all(w in _neg for w in ph.split())
                for ph in vocab if contains_any(norm, [ph]))),
        negated_services=tuple(
            svc for svc, vocab in SERVICE_VOCAB
            if any(all(w in _neg for w in ph.split())
                   for ph in vocab if contains_any(norm, [ph]))),
        both=contains_any(norm, BOTH_WORDS),
        asks=detect_asks(norm, product),
    )
    # "I already have PPF" is a fact, not a request for PPF.
    owned = owned_service(norm)
    if owned:
        p.ownership_service = owned
        # "i already have ppf, can i do ceramic on top?" - the request is
        # the OTHER service named in the message.
        if p.service == owned:
            others = [x for x in p.services if x != owned
                      and x in (Service.PPF, Service.CERAMIC, Service.DETAILING)]
            p.service = others[0] if others else owned
    elif service in (Service.PPF, Service.CERAMIC) \
            and contains_any(norm, OWNERSHIP_WORDS) \
            and not contains_any(norm, WANT_WORDS):
        p.ownership_service = service
    # "Pro." / "normal" on their own narrow a GFX conversation.
    # "not normal, pro wala": the ruled-out word does not count.
    if product is None:
        words = set(norm.split()) - negated_terms(norm)
        if "pro" in words and "normal" not in words:
            p.gfx_refinement = "gfx_pro"
        elif "normal" in words and "pro" not in words:
            p.gfx_refinement = "gfx_normal"
    return p


# ===========================================================================
# THE MAIN ENTRY POINT
# ===========================================================================
def answer(message: str, conversation_id: Optional[str] = None,
           use_ai: bool = True) -> Answer:
    """Work out what to say to one customer message.

    conversation_id enables context and knowledge-gap recording. Passing
    None makes this a pure function, which is what the test suite uses.
    """
    norm = normalise(message)
    ctx = build_context(conversation_id)

    # ======================================================================
    # 1. PERCEIVE   2. CLASSIFY (once)   3. RESOLVE one frame
    # ======================================================================
    # Every branch below reads `frame`-derived values. None of them decides
    # for itself whether to trust the message, the state or an old scan -
    # that decision was made exactly once, in dialogue.classify().
    p = perceive(message)
    state = ctx.get("state") or {}
    mtype = dialogue.classify(p, state)
    frame = dialogue.resolve(p, state, mtype,
                             brand_for_model=lambda m: CAR_BRANDS.get(m.lower()))

    frame.model_named = bool(p.model)          # the car came from THIS message
    service, product, intent = frame.service, frame.product, frame.intent
    brand, model, year = frame.brand, frame.model, frame.year

    # A recognised product settles the category. "virtus armrest available?"
    # names no service word, and letting it stay serviceless is what let the
    # word "available" drag it to a glass-coating answer.
    if product and service is None:
        service = kb.PRODUCTS[product]["category"]
        if intent in (Intent.UNKNOWN, Intent.OTHER):
            intent = Intent.ACCESSORY_INQUIRY
        frame.service, frame.intent = service, intent   # so the state agrees

    # Legacy conversations with no structured state: the message scan may
    # still know the car.
    if not model and not state and ctx["car_model"]:
        brand, model, year = ctx["car_brand"], ctx["car_model"], ctx["car_year"]
    if service is None and product is None and not state and ctx["service"] \
            and mtype == MT.AMBIGUOUS:
        service = ctx["service"]

    # What THIS message said on its own (branches that must judge the raw
    # words - e.g. the booking-slot acknowledgement - use these).
    msg_service, msg_product, msg_model = p.service, p.product, p.model
    msg_intent = p.intent
    # The car a price/stock question is ABOUT: named in this message, or
    # named earlier inside this same topic. A car remembered from an
    # unrelated earlier topic is not being asked for.
    asked_model = (
        (msg_model or (model if (state.get("last_message_type") == MT.AMBIGUOUS
                                 and state.get("car_named_in_topic")) else None))
        if mtype == MT.NEW_TOPIC
        else (model if (p.model or state.get("car_named_in_topic")) else None))
    slot_answer = mtype in (MT.SLOT_ANSWER, MT.CORRECTION)
    bare_message = mtype == MT.AMBIGUOUS

    buying = detect_buying_intent(norm, intent)

    base = Answer(reply="", source="AI", service=service, intent=intent,
                  buying_intent=buying, car_brand=brand, car_model=model,
                  car_year=year, product=product, context_used=ctx,
                  answer_to_pending=slot_answer,
                  issue=frame.issue if mtype != MT.NEW_TOPIC else p.issue,
                  own_intent=(mtype == MT.NEW_TOPIC),
                  clear_topic=(mtype == MT.CANCEL),
                  message_type=mtype, facts=frame.facts, frame=frame)
    if slot_answer and base.buying_intent == "LOW":
        base.buying_intent = "MEDIUM"    # they came back with their car

    # ---- PRIORITY 1: manager-approved knowledge --------------------------
    if service and (mtype == MT.NEW_TOPIC or p.has_followup):
        supplied = kb.manager_answer(service, p.intent if p.has_followup else intent)
        if supplied:
            base.reply, base.source = supplied, "MANAGER"
            base.confidence, base.covered = 1.0, True
            return base

    # ---- A proposed slot inside an active conversation ---------------------
    # "haan kal 11 baje" is the customer proposing a time, not asking when
    # we open - "baje" (o'clock) only makes it look like an hours question.
    # Must run BEFORE the FAQ lookup, which otherwise serves the opening-time
    # FAQ. Acknowledge, hand the slot to the team, never confirm it here.
    # Only a CONTINUATION can be a slot proposal (classify() already ruled
    # out asides and new topics); a bare "haan" without a day is left to the
    # continuation block, which asks for the preferred day.
    if mtype == MT.CONTINUATION and (p.day or (p.is_affirm and p.is_side)) \
            and p.intent not in (Intent.PRICE_INQUIRY, Intent.DURATION,
                                 Intent.WARRANTY, Intent.AVAILABILITY_REQUEST,
                                 Intent.SERVICE_COMPARISON, Intent.RECOMMENDATION,
                                 Intent.OFFER_DISCOUNT) and (
            p.is_side or intent == Intent.BOOKING_REQUEST
            or p.intent in (Intent.UNKNOWN, Intent.OTHER, Intent.BOOKING_REQUEST))             and not [x for x in p.asks
                     if x in ("price", "duration", "location", "hours", "recommend")]:
        hin = _hinglish(norm)
        car = _car_phrase(brand, model)
        day = p.day
        when = {"tomorrow": "kal" if hin else "tomorrow",
                "today": "aaj" if hin else "today"}.get(day, day or "")
        base.reply = (
            (f"Theek hai 👍 {when + ' ' if when else ''}note kar liya"
             f"{' aapki ' + car + ' ke liye' if car else ''}. Slot team confirm "
             f"karegi - {PHONE} par WhatsApp ya call kar lijiye. "
             f"{BUSINESS['hours_sentence']}")
            if hin else
            (f"Great 👍 noted{' for ' + when if when else ''}"
             f"{' for your ' + car if car else ''}. Our team will confirm the "
             f"slot - WhatsApp or call {PHONE}. {BUSINESS['hours_sentence']}"))
        base.source, base.intent = "RULE", Intent.BOOKING_REQUEST
        base.confidence, base.covered = 0.85, True
        base.resolution = Resolution.BOOKING_REQUESTED
        base.preferred_day = day
        return base

    # ---- "Actually forget that" - drop the topic, keep the car -------------
    if mtype == MT.CANCEL:
        car = _car_phrase(brand, model)
        base.reply = (f"No problem - what else can I help with"
                      f"{' for your ' + car if car else ''}? PPF, ceramic, "
                      "denting/painting, service or accessories.")
        base.source, base.intent = "RULE", Intent.OTHER
        base.confidence, base.covered = 0.8, True
        base.resolution = Resolution.ANSWERED
        base.clear_topic = True
        base.service = None
        base.product = None
        return base

    # ---- "Which car is in this reel?" - about the video, not the catalogue --
    if contains_any(norm, REEL_CAR_WORDS):
        hin = _hinglish(norm)
        base.reply = (f"Is car ki details team share kar degi - {PHONE} par WhatsApp "
                      "ya call kar lijiye." if hin else
                      f"Our team will share this car's details - WhatsApp or call {PHONE}.")
        base.source, base.escalated = "ESCALATION", True
        base.covered = False
        base.resolution = Resolution.ESCALATED
        base.gap_topic = "Question about the car shown in a reel/video"
        base.confidence = 0.8
        return base

    # ---- "Do you have another branch?" ------------------------------------
    if p.is_side and (p.service == Service.LOCATION or p.intent == Intent.LOCATION) \
            and contains_any(norm, BRANCH_WORDS):
        hin = _hinglish(norm)
        base.reply = (
            f"{LOCATION_REPLY} Kisi aur branch ke baare me team isi number "
            "par confirm karegi."
            if hin else
            f"{LOCATION_REPLY} Whether we have any other branch is something "
            "our team will confirm on the same number.")
        base.source, base.intent = "ESCALATION", Intent.LOCATION
        base.escalated, base.covered = True, False
        base.resolution = Resolution.ESCALATED
        base.confidence = 0.9
        base.gap_topic = gap_topic(Service.LOCATION, Intent.GENERAL_INFORMATION)
        return base

    # ---- Several questions in one message ---------------------------------
    # "scratches hain, ppf ya ceramic? approx batao, kitne din, kal drop kar
    # sakta hu, gfx pro mats bhi chahiye" - answer each verified part, mark
    # each unverified part, one number at the end.
    asks = detect_asks(norm, product)
    # "ppf ya ceramic konsa better?" is three asks by count (ppf, ceramic,
    # recommend) but ONE question - a comparison, which must still escalate
    # because no approved comparison exists. Only fire when there is at
    # least one genuinely separate ask beyond the service names.
    substantive = [x for x in asks if x not in ("ppf", "ceramic", "recommend")]
    if (len(asks) >= 3 and substantive) or (len(asks) == 2 and "location" in asks):
        base.reply = compose_multi_reply(asks, brand, model, year, norm)
        base.keep_core = False           # built around the car phrase
        base.source = "RULE"
        base.confidence, base.covered = 0.85, True
        base.resolution = Resolution.ANSWERED
        base.intent = (Intent.RECOMMENDATION if "recommend" in asks
                       else Intent.SERVICE_INQUIRY)
        if base.service is None:
            if "ppf" in asks:
                base.service = Service.PPF
            elif "ceramic" in asks:
                base.service = Service.CERAMIC
            elif "scratch" in asks or "dent" in asks:
                base.service = Service.DENTING
        if "gfx" in asks and not base.product:
            base.product = "gfx_pro"
        base.issue = ("the scratches" if "scratch" in asks
                      else "the dent" if "dent" in asks else base.issue)
        base.preferred_day = extract_preferred_day(norm)
        if "booking" in asks:
            base.resolution = Resolution.BOOKING_REQUESTED
        return base

    # ---- Short, cold, ambiguous: ask, don't guess -----------------------------
    # "Available?", "Yes.", "For my car.", "Same for mine?" with no topic at
    # all. These used to reach the FAQ matcher (spare-parts answer) or the
    # model (which once recited its own instructions).
    if mtype == MT.AMBIGUOUS:
        hin = _hinglish(norm)
        about = {Intent.PRICE_INQUIRY: ("the price", "price"),
                 Intent.AVAILABILITY_REQUEST: ("availability", "availability"),
                 Intent.DURATION: ("the time it takes", "time"),
                 Intent.SERVICE_COMPARISON: ("the comparison", "comparison"),
                 Intent.RECOMMENDATION: ("a recommendation", "recommendation"),
                 }.get(p.intent)
        known = _car_phrase(brand, model)
        if about and known:
            base.reply = (
                (f"Zaroor 👍 aapki {known} ke liye {about[1]} kis service ya "
                 "product ke liye chahiye? Bata dijiye, main guide kar deta "
                 f"hoon. Call/WhatsApp: {PHONE}")
                if hin else
                (f"Happy to help with {about[0]} for your {known} 👍 which "
                 "service or product is it for? Tell me and I'll guide you. "
                 f"Call/WhatsApp: {PHONE}"))
        elif about:
            base.reply = (
                (f"Zaroor 👍 {about[1]} kis service ya product ke liye chahiye, "
                 "aur aapki car konsi hai? Bata dijiye, main guide kar deta "
                 f"hoon. Call/WhatsApp: {PHONE}")
                if hin else
                (f"Happy to help with {about[0]} 👍 which service or product is "
                 "it for, and which car model? Tell me and I'll guide you. "
                 f"Call/WhatsApp: {PHONE}"))
        elif known:
            base.reply = (
                (f"Zaroor 👍 Aapki {known} ke liye kya chahiye - PPF, ceramic, "
                 "denting/painting, service ya accessories? Main guide kar "
                 f"deta hoon. Call/WhatsApp: {PHONE}")
                if hin else
                (f"Happy to help 👍 What do you need for your {known} - PPF, "
                 "ceramic coating, denting/painting, service or accessories? "
                 f"I'll guide you. Call/WhatsApp: {PHONE}"))
        else:
            base.reply = (
                ("Zaroor 👍 Bata dijiye aapki car ka model aur kya chahiye - PPF, "
                 "ceramic, denting/painting, service ya accessories - main guide "
                 f"kar deta hoon. Call/WhatsApp: {PHONE}")
                if hin else
                ("Happy to help 👍 Tell me your car model and what you need - PPF, "
                 "ceramic coating, denting/painting, service or accessories - and "
                 f"I'll guide you. Call/WhatsApp: {PHONE}"))
        base.source, base.intent = "RULE", Intent.OTHER
        base.confidence, base.covered = 0.7, True
        base.resolution = Resolution.ANSWERED
        return base

    # ---- Complaints never get a sales answer -----------------------------
    # Checked before the FAQ lookup: "worst service ever" shares the word
    # "service" with the what-does-a-service-include FAQ, and answering an
    # angry customer with a service menu is the worst possible reply.
    if intent == Intent.COMPLAINT:
        base.reply = (
            "I'm really sorry to hear that. Our team will look into this "
            f"personally - please call or WhatsApp us on {PHONE} and we'll "
            "sort it out for you.")
        base.source, base.escalated = "ESCALATION", True
        base.resolution = Resolution.ESCALATED
        base.confidence = 0.8
        return base

    # ---- The customer named their car and nothing else --------------------
    # "I have a Creta." Asking one short qualifying question beats a generic
    # greeting, and it sets up the context so the next message ("PPF.") is
    # understood on its own.
    #
    # Checked BEFORE the FAQ lookup, because the lone word "creta" otherwise
    # matched "wind visor for creta" and answered a question nobody asked.
    # ---- A specific used model: stock nobody here can see ----------------
    # "swift mil jayegi purani?" / "second hand creta ka price" - the
    # approved answers give a floor price and example models, never whether
    # one particular car is on the lot today or what it costs. Only a car
    # named in THIS message counts; a remembered car is not being asked for.
    # A model the approved text itself lists ("purani creta milegi") may be
    # answered from that text - its price still may not.
    if service == Service.USED_CARS and asked_model and intent in (
            Intent.AVAILABILITY_REQUEST, Intent.PRICE_INQUIRY,
            Intent.USED_CAR_INQUIRY, Intent.GENERAL_INFORMATION,
            Intent.SERVICE_INQUIRY, Intent.UNKNOWN, Intent.OTHER) and (
            intent == Intent.PRICE_INQUIRY
            or not used_model_in_approved_text(asked_model)):
        hin = _hinglish(norm)
        car = _car_phrase(brand, asked_model)
        base.reply = (
            f"Used {car} ka current stock aur price team confirm karegi - "
            f"{PHONE} par WhatsApp ya call kar lijiye."
            if hin else
            f"For a used {car}, current stock and price are confirmed by our "
            f"team - WhatsApp or call {PHONE}.")
        base.source, base.escalated = "ESCALATION", True
        base.covered = False
        base.resolution = Resolution.ESCALATED
        base.gap_topic = gap_topic(Service.USED_CARS, intent)
        base.confidence = 0.8
        return base

    # ---- CONTINUE THE PENDING INTENT --------------------------------------
    # The customer answered the bot's question. Pick up the original request
    # with the new detail merged in - never restart with a generic welcome.
    if slot_answer or (mtype == MT.CONTINUATION and not p.has_followup
                       and len(p.services) < 2
                       and intent != Intent.COMPLAINT
                       and not (p.product and p.product in GFX_KEYS)
                       and not p.gfx_refinement):
        if p.is_affirm and not p.model and not p.year and p.n_tokens <= 2:
            car = _car_phrase(brand, model)
            base.reply = (
                (f"Great 👍 WhatsApp or call us on {PHONE} with your preferred "
                 f"day and our team will confirm the slot for your {car}.")
                if car else
                (f"Great 👍 WhatsApp or call us on {PHONE} with your car model "
                 "and preferred day, and our team will confirm the slot."))
            base.source, base.intent = "RULE", Intent.BOOKING_REQUEST
            base.confidence, base.covered = 0.8, True
            base.resolution = Resolution.BOOKING_REQUESTED
            return base
        base.reply, base.next_slot = compose_continuation(
            state, brand, model, year, norm, base, frame=frame)
        base.source = "RULE"
        base.confidence, base.covered = 0.9, True
        base.resolution = Resolution.ANSWERED
        # A booking that was waiting on the car keeps its booking nature,
        # and the day the customer mentioned ("kal", "tomorrow") is still
        # captured for the lead record.
        base.preferred_day = extract_preferred_day(norm)
        if (state.get("intent") == Intent.BOOKING_REQUEST
                or contains_any(norm, STRONG_BOOKING_WORDS)
                or base.preferred_day) and not p.has_followup:
            base.intent = Intent.BOOKING_REQUEST
            base.resolution = Resolution.BOOKING_REQUESTED
        return base

    if mtype == MT.NEW_TOPIC and not frame.has_topic and msg_model \
            and msg_intent in (Intent.UNKNOWN, Intent.OTHER):
        if ctx.get("booking") and not state:
            # Section 13: already booking, so ask only for what is still
            # missing - never for the model they just gave us.
            base.reply = (
                f"Thanks, noted your {msg_model}. What would you like done, "
                "and which day suits you? Our team will confirm the slot on "
                f"{PHONE}.")
            base.resolution = Resolution.BOOKING_REQUESTED
        else:
            base.reply = (
                f"Thanks! What would you like done on your {msg_model}? We "
                "handle PPF and ceramic coating, denting and painting, full "
                "service, accessories and more.")
            base.resolution = Resolution.ANSWERED
        base.source = "RULE"
        base.confidence, base.covered = 0.75, True
        base.preferred_day = extract_preferred_day(norm)
        return base

    # ---- "How to order" ---------------------------------------------------
    # Checked before the FAQ lookup and before the model. This message used
    # to reach Ollama and take about 65 seconds; it is now instant.
    if contains_any(norm, ORDER_WORDS):
        base.reply, base.source = ORDER_REPLY, "RULE"
        base.intent = Intent.GENERAL_INFORMATION
        base.confidence, base.covered = 0.9, True
        base.resolution = Resolution.ANSWERED
        return base

    # ---- Booking requests with no service named --------------------------
    # "book a slot", "how do i book", "do you take appointments". There is
    # no service to look up, so going near the FAQ matcher only produces a
    # wrong answer. Answer the actual question instead.
    if intent == Intent.BOOKING_REQUEST and service is None:
        base.reply, base.source = BOOKING_REPLY, "RULE"
        base.confidence, base.covered = 0.9, True
        base.resolution = Resolution.BOOKING_REQUESTED
        base.preferred_day = extract_preferred_day(norm)
        return base

    # ---- "What should I get?" --------------------------------------------
    # Guidance, not a specific product. Answering it from the FAQ list gave
    # "back seat organizers for laptops" - a real product, but a random one.
    # ---- "Which brand do you use?" - nothing approved names one ----------
    # "PPF me kaunsa brand best hai? 3M ya Garware?" No approved answer
    # states the film or coating brand, so this is handed over rather than
    # answered with the generic PPF/ceramic description.
    if product is None and service in (
            Service.PPF, Service.CERAMIC, Service.PAINTING, Service.DENTING,
            Service.DETAILING) and contains_any(
            norm, ["brand", "brands", "3m", "garware", "xpel", "llumar",
                   "company ka", "kaunsi company", "konsi company",
                   "which company"]):
        hin = _hinglish(norm)
        label = kb.SERVICE_LABELS.get(service, "this")
        base.reply = (
            f"{label.capitalize()} ke liye kaunsa brand/film use hota hai ye "
            f"team confirm karegi - {PHONE} par WhatsApp ya call kar lijiye."
            if hin else
            f"Which brand or film we use for {label} is something our team "
            f"confirms - WhatsApp or call {PHONE}.")
        base.source, base.escalated = "ESCALATION", True
        base.covered = False
        base.resolution = Resolution.ESCALATED
        base.gap_topic = gap_topic(service, Intent.GENERAL_INFORMATION)
        base.confidence = 0.6
        return base

    if intent == Intent.RECOMMENDATION and product is None:
        topic_issue = (frame.issue if (msg_service is None) else None) \
            or extract_issue(norm)
        base.reply = (compose_topic_recommendation(service, topic_issue,
                                                   brand, model, norm)
                      or compose_recommendation(model, norm))
        base.source = "RULE"
        base.confidence, base.covered = 0.75, True
        base.resolution = Resolution.ANSWERED
        base.reply = _finish_sales_reply(base, norm)
        return base

    # ---- Verified prices: the two the approved answers actually state ----
    # "kitne ki h" after the membership answer, or "kitne se start hai" for
    # used cars, come from the approved text - never from the escalation
    # template, which would hide a fact the customer is allowed to hear.
    vp = (verified_price_faq(service, asked_model)
          if intent == Intent.PRICE_INQUIRY else None)
    if vp:
        base.reply, base.source, base.faq_id = vp["answer"], "FAQ", vp["id"]
        base.core_reply = vp["answer"]
        base.confidence, base.covered = 0.9, True
        base.resolution = Resolution.ANSWERED
        return base


    # ---- Two services in one message -------------------------------------
    # "PPF karwani hai aur ceramic bhi" used to be answered about ceramic
    # only. Both are verified (FAQ 28 states we offer both), so answer both
    # - rule 10, answer the verified parts rather than picking one.
    # "ceramic nahi, PPF karwana hai" names both words but rules one out -
    # that is a PPF request, not a request for both.
    if (contains_any(norm, ["ppf"])
            and contains_any(norm, ["ceramic", "ceramic coating"])
            and not ({"ppf", "ceramic"} & negated_terms(norm))
            and not p.ownership_service
            and intent not in (Intent.SERVICE_COMPARISON, Intent.WARRANTY,
                               Intent.PRICE_INQUIRY, Intent.DURATION)):
        hin = _hinglish(norm)
        base.service = frame.service = Service.PPF
        base.reply = (
            "Haan, PPF aur ceramic coating dono hum karte hain."
            if hin else "Yes, we do both PPF and ceramic coating.")
        base.source = "RULE"
        base.confidence, base.covered = 0.85, True
        base.resolution = Resolution.ANSWERED
        base.reply = _finish_sales_reply(base, norm)
        return base


    # ---- Live stock questions: nothing here can see the shelf ------------
    if contains_any(norm, LIVE_STOCK_WORDS) and product not in GFX_KEYS:
        hin = _hinglish(norm)
        what = kb.PRODUCTS[product]["label"] if product else (
            "is item" if hin else "this item")
        car = _car_phrase(brand, model)
        base.intent = Intent.AVAILABILITY_REQUEST
        base.keep_core = False           # a stock answer is not topic memory
        if hin:
            base.reply = (f"{what.capitalize()} ka current stock team confirm "
                          f"karegi - {PHONE} par WhatsApp ya call kar lijiye"
                          + (f", {car} ke liye." if car else "."))
        else:
            base.reply = (f"Current stock for {what} is something our team "
                          f"confirms - WhatsApp or call {PHONE}"
                          + (f" and they'll check it for your {car}." if car
                             else " with your car model and they'll check."))
        base.source = "RULE"
        base.confidence, base.covered = 0.85, True
        base.resolution = Resolution.ANSWERED
        return base

    # ---- Audio: "what brands do you have?" - the verified ones ------------
    if (service == Service.AUDIO or product in ("speakers", "carplay",
                                                "android_system")) \
            and intent in (Intent.AVAILABILITY_REQUEST, Intent.RECOMMENDATION,
                           Intent.ACCESSORY_INQUIRY):
        hin = _hinglish(norm)
        car = _car_phrase(brand, model)
        unv = " Android system ka exact option team confirm kar degi." if (
            product == "android_system" and hin) else (
            " For an Android system specifically, our team will confirm the "
            "options." if product == "android_system" else "")
        if hin:
            base.reply = ("Audio mein hum JBL, Hertz, JL Audio aur Morel ke "
                          "speakers/subwoofers rakhte hain, aur Apple CarPlay "
                          f"systems sell aur install karte hain.{unv} "
                          + (f"Aapki {car} ke liye " if car else "")
                          + f"exact option aur price team confirm kar degi - "
                          f"call/WhatsApp {PHONE}.")
        else:
            base.reply = ("For audio we carry JBL, Hertz, JL Audio and Morel "
                          "speakers and subwoofers, and we sell and install "
                          f"Apple CarPlay systems.{unv} Our team will confirm "
                          f"the exact option and price{' for your ' + car if car else ''}"
                          f" - call or WhatsApp {PHONE}.")
        base.source = "RULE" if product != "android_system" else "ESCALATION"
        base.escalated = product == "android_system"
        base.confidence, base.covered = 0.85, product != "android_system"
        base.resolution = Resolution.ANSWERED
        if product == "android_system":
            base.gap_topic = "Product not in knowledge base - Android infotainment system"
        return base

    # ---- "Which mats do you have?" - list the verified options ------------
    if product == "floor_mats" and intent in (Intent.AVAILABILITY_REQUEST,
                                              Intent.RECOMMENDATION):
        hin = _hinglish(norm)
        car = _car_phrase(brand, model)
        for_car = f" for your {car}" if car else ""
        if hin:
            base.reply = (
                "Mats mein hum waterproof 7D Lifelong / GF Lifelong mats "
                "rakhte hain, aur GFX Normal aur GFX Pro/Lifelong bhi. "
                + (f"Aapki {car} ke liye " if car else "Aapki car ke liye ")
                + f"exact fitting aur price team confirm kar degi - "
                f"call/WhatsApp {PHONE}.")
        else:
            base.reply = (
                "For mats we carry waterproof 7D Lifelong / GF Lifelong mats, "
                "plus GFX Normal and GFX Pro/Lifelong. Our team will confirm "
                f"the exact fit and price{for_car} - call or WhatsApp {PHONE}.")
        base.source = "RULE"
        base.confidence, base.covered = 0.85, True
        base.resolution = Resolution.ANSWERED
        return base

    # ---- GFX mats: always composed from owner-verified knowledge ----------
    # Never routed to the FAQ matcher, so "GFX" can never collapse into the
    # Lifelong FAQ, generic mats, or - as once happened - a battery answer.
    if product in GFX_KEYS:
        if intent in (Intent.WARRANTY, Intent.DURATION):
            base.reply = escalation_reply(intent, None, _hinglish(norm))
            base.source, base.escalated = "ESCALATION", True
            base.resolution = Resolution.ESCALATED
            base.gap_topic = gap_topic(Service.ACCESSORIES, intent)
            base.confidence = 0.5
            return base
        if intent == Intent.AVAILABILITY_REQUEST and msg_product is None:
            norm = norm + " in stock"        # follow-up: short stock answer
        base.reply = compose_gfx_reply(product, brand, model, intent, norm)
        base.source = "RULE"
        base.confidence, base.covered = 0.9, True
        base.resolution = Resolution.ANSWERED
        if intent == Intent.PRICE_INQUIRY:
            # We answered what we could; the price itself still needs the
            # team, and the owner should see that on the gap list.
            base.gap_topic = gap_topic(Service.ACCESSORIES, intent)
        return base

    # ---- The customer named a specific PRODUCT ---------------------------
    # Handled before the FAQ lookup in the two cases where the lookup is
    # known to go wrong:
    #
    #   * the product is NOT verified ("armrest", "GFX mats", "car cover").
    #     No approved answer exists, so any FAQ match is a coincidence -
    #     "virtus armrest available?" landed on glass coating.
    #   * the customer named their CAR. A generic approved answer would be
    #     about somebody else's model, so a tailored one is safer.
    #
    # Everything else still goes to the approved FAQ text first, because an
    # approved answer beats an assembled one whenever it genuinely fits.
    # NOTE: this no longer fires merely because the customer named a car.
    # It used to, as insurance against wrong-car answers - but the
    # car-conflict guard in faq_is_eligible() handles that properly now, and
    # pre-empting the FAQ was costing us exact approved answers like FAQ 72
    # ("7d mat price for nexon").
    _named = detect_products(norm)
    _any_unverified = any(not kb.PRODUCTS[k]["verified"] for k in _named)
    if product and (_any_unverified or intent == Intent.RECOMMENDATION):
        reply, verified = compose_product_reply(product, model, intent, norm)

        # PARTIAL ANSWERS: the customer asked about several things and we
        # can confirm some but not all. Answer what we know, then name the
        # part that needs the team - rather than escalating the whole
        # message ("Fortuner ke mats chahiye aur armrest bhi").
        unconfirmed = [k for k in detect_products(norm)
                       if k != product and not kb.PRODUCTS[k]["verified"]]
        if verified and unconfirmed:
            names = ", ".join(kb.PRODUCTS[k]["label"] for k in unconfirmed[:2])
            # .capitalize() would turn "GFX mats" into "Gfx mats".
            shown = names if names[:1].isupper() or any(
                c.isupper() for c in names) else names.capitalize()
            reply += (f" {shown} ke liye availability team "
                      "confirm kar degi." if _hinglish(norm)
                      else f" For {shown}, our team will confirm availability.")
            base.gap_topic = (f"Product not in knowledge base - "
                              f"{kb.PRODUCTS[unconfirmed[0]]['label']}")

        base.reply = reply
        base.source = "RULE" if verified else "ESCALATION"
        base.covered = verified
        base.confidence = 0.85 if verified else 0.5
        if not verified:
            base.escalated = True
            base.resolution = Resolution.ESCALATED
            # Log it so the owner can confirm the item and turn it on.
            base.gap_topic = (f"Product not in knowledge base - "
                              f"{kb.PRODUCTS[product]['label']}")
            base.covered = False
        else:
            base.resolution = Resolution.ANSWERED
            # We can say what we carry, but not what it costs or how long
            # it is guaranteed for - nothing verified covers that. Answer
            # the part we know, then flag the rest for the team.
            if intent in (Intent.PRICE_INQUIRY, Intent.WARRANTY,
                          Intent.DURATION):
                base.escalated = True
                base.resolution = Resolution.ESCALATED
                base.gap_topic = gap_topic(base.service, intent)
        base.reply = _finish_sales_reply(base, norm)
        return base



    # ---- PRIORITY 2: the approved FAQ knowledge base ---------------------
    faq, score = match_faq(message, service, intent, model, product,
                           asked_car=asked_model)
    if faq is not None:
        base.faq_id = faq["id"]
        base.confidence = round(min(score, 1.0), 3)
        if faq["confirmed"]:
            base.reply, base.source = faq["answer"], "FAQ"
            if mtype == MT.NEW_TOPIC or faq["service"] == service:
                base.core_reply = faq["answer"]   # remembered for the topic, car-free
            if model:                          # "reply with your car model" - they did
                base.reply = _strip_model_asks(base.reply) or faq["answer"]
            base.covered = True
            base.resolution = Resolution.ANSWERED

            # The customer asked to BOOK this service, not just to hear
            # about it. The approved answer explains the work; this adds
            # how to actually book it.
            if intent == Intent.BOOKING_REQUEST:
                base.reply = f"{base.reply} {booking_line(model)}"
                base.resolution = Resolution.BOOKING_REQUESTED
                base.preferred_day = extract_preferred_day(norm)

            # A genuine buying signal earns ONE relevant suggestion. The
            # approved answer is never altered - the suggestion is added
            # after it, and only for verified pairings.
            elif base.buying_intent in ("HIGH", "MEDIUM"):
                base.reply = _finish_sales_reply(base, norm)
            return base

        # A missing-information entry is NOT an answer - it is a recorded
        # admission that we do not know. Escalate and log the gap.
        base.reply = escalation_reply(intent, faq.get("note"),
                                      _hinglish(norm))
        base.source = "ESCALATION"
        base.escalated = True
        base.covered = False
        base.resolution = Resolution.ESCALATED
        base.gap_topic = gap_topic(faq["service"], faq["intent"])
        return base

    # ---- PRIORITY 3: deterministic core facts ----------------------------
    if intent == Intent.LOCATION or service == Service.LOCATION:
        # "Mansarovar me ho?" - the customer is asking about the old
        # location. Answer with the current Dholai address instead of
        # confirming or denying, so the outdated area is never presented as
        # where we are.
        if contains_any(norm, FORMER_AREA_WORDS):
            base.reply, base.source = FORMER_AREA_REPLY, "RULE"
            base.confidence, base.covered = 0.95, True
            return base

        # Guard: "do you have branches" / "parking" are unknown, not location.
        if contains_any(norm, ["branch", "branches", "parking"]):
            base.reply = kb.escalation_message()
            base.source, base.escalated = "ESCALATION", True
            base.resolution = Resolution.ESCALATED
            base.gap_topic = gap_topic(Service.LOCATION,
                                       Intent.GENERAL_INFORMATION)
            base.confidence = 0.9
            return base
        base.reply, base.source = LOCATION_REPLY, "RULE"
        base.confidence, base.covered = 0.95, True
        return base


    if intent == Intent.HOURS or service == Service.HOURS:
        base.reply, base.source = HOURS_REPLY, "RULE"
        base.confidence, base.covered = 0.95, True
        return base

    if intent == Intent.CONTACT or service == Service.CONTACT:
        base.reply, base.source = CONTACT_REPLY, "RULE"
        base.confidence, base.covered = 0.95, True
        return base

    # ---- Booking request naming a service we had no FAQ for ---------------
    # e.g. "book a tyre alignment slot". We know the service but no approved
    # answer matched, so give the booking instructions rather than falling
    # through to the language model.
    if intent == Intent.BOOKING_REQUEST:
        base.reply, base.source = BOOKING_REPLY, "RULE"
        base.confidence, base.covered = 0.9, True
        base.resolution = Resolution.BOOKING_REQUESTED
        base.preferred_day = extract_preferred_day(norm)
        return base

    # ---- Offers and discounts: only the verified one ----------------------
    # The model once answered "We don't offer any discounts" - an invented
    # NEGATIVE, contradicting the Gold Membership discount in FAQ 87.
    if intent == Intent.OFFER_DISCOUNT:
        hin = _hinglish(norm)
        base.reply = (
            ("Jo discount hum confirm kar sakte hain wo Gold Membership "
             "(Rs 2,999) ke saath hai - accessories/painting par special "
             "discounts aur free services. Koi aur current offer hai ya "
             f"nahi, team {PHONE} par confirm kar degi.")
            if hin else
            ("The discount we can confirm is through the Gold Membership "
             "(Rs 2,999) - special discounts on accessories and painting, "
             "plus free services. Whether any other offer is running right "
             f"now, our team will confirm on {PHONE}."))
        base.source = "RULE"
        base.confidence, base.covered = 0.85, True
        base.resolution = Resolution.ANSWERED
        return base


    # ---- Unknown facts we must never guess -------------------------------
    # A price, warranty, duration or availability question about a service
    # with no approved answer must escalate rather than reach the model.
    # "coolant milega?" about a VERIFIED catalogue item is answered from the
    # catalogue (we carry it; stock is the team's call) - not escalated.
    if intent == Intent.AVAILABILITY_REQUEST and product             and kb.PRODUCTS.get(product, {}).get("verified"):
        reply, verified = compose_product_reply(product, model, intent, norm)
        base.reply = reply
        base.source = "RULE" if verified else "ESCALATION"
        base.escalated = not verified
        base.covered = verified
        base.resolution = Resolution.ANSWERED if verified else Resolution.ESCALATED
        base.confidence = 0.8
        return base

    if intent in (Intent.PRICE_INQUIRY, Intent.WARRANTY, Intent.DURATION,
                  Intent.SERVICE_COMPARISON, Intent.AVAILABILITY_REQUEST,
                  Intent.OFFER_DISCOUNT, Intent.PICKUP_DROP):
        base.reply = escalation_reply(intent, None, _hinglish(norm))
        base.source, base.escalated = "ESCALATION", True
        base.covered = False
        base.resolution = Resolution.ESCALATED
        base.gap_topic = gap_topic(service, intent)
        base.confidence = 0.4
        return base

    # ---- Short follow-ups inside an active topic -------------------------
    # "Full body.", "haan", "ok" carry no intent of their own; their meaning
    # is the conversation they sit in. They used to reach the language
    # model (48 seconds for "Full body") and come back generic.
    if mtype in (MT.CONTINUATION, MT.SLOT_ANSWER, MT.CORRECTION) \
            and len(tokens(message)) <= 4 \
            and intent in (Intent.UNKNOWN, Intent.OTHER, Intent.SERVICE_INQUIRY):
        if contains_any(norm, AFFIRM_WORDS):
            car = _car_phrase(brand, model)
            base.reply = (
                (f"Great 👍 WhatsApp or call us on {PHONE} with your preferred "
                 f"day and our team will confirm the slot for your {car}.")
                if car else
                (f"Great 👍 WhatsApp or call us on {PHONE} with your car model "
                 "and preferred day, and our team will confirm the slot."))
            base.source = "RULE"
            base.confidence, base.covered = 0.8, True
            base.resolution = Resolution.BOOKING_REQUESTED
            base.intent = Intent.BOOKING_REQUEST
            return base
        base.reply, base.next_slot = compose_continuation(
            state, brand, model, year, norm, base, frame=frame)
        base.source = "RULE"
        base.confidence, base.covered = 0.8, True
        base.resolution = Resolution.ANSWERED
        return base

    # ---- A named service with no approved answer --------------------------
    # "shocker?" / "teflon?" as a new topic: say what we do for that service
    # from its approved introduction, or hand THAT service to the team -
    # never the old topic, never the generic welcome.
    if mtype == MT.NEW_TOPIC and service and service not in (
            Service.LOCATION, Service.HOURS, Service.CONTACT):
        intro = service_intro(service)
        hin = _hinglish(norm)
        label = kb.SERVICE_LABELS.get(service, "this")
        if intro:
            base.reply = intro if PHONE in intro else f"{intro} {_cta(hin)}"
            base.core_reply = intro
            base.source, base.covered, base.confidence = "RULE", True, 0.7
            base.resolution = Resolution.ANSWERED
        else:
            base.reply = (
                f"{label.capitalize()} ke liye team confirm karegi - {PHONE} "
                "par WhatsApp ya call kar lijiye." if hin else
                f"For {label}, our team will confirm - WhatsApp or call {PHONE}.")
            base.source, base.escalated, base.covered = "ESCALATION", True, False
            base.resolution = Resolution.ESCALATED
            base.gap_topic = gap_topic(service, intent)
        return base

    # ---- PRIORITY 4: the language model, for wording only -----------------
    if not use_ai:
        base.reply = OFFLINE_REPLY
        base.source = "RULE"
        base.resolution = Resolution.UNRESOLVED
        return base

    note = ""
    if model:
        note = f"(The customer drives a {brand or ''} {model}.)".strip()
    reply, ok = ask_ollama(message, note)

    # FACT CHECK the model's wording before it reaches a customer. If it
    # invented a price, a warranty, stock or a website, the whole reply is
    # discarded rather than edited - a half-removed sentence reads worse
    # than a clean handover - and the topic is logged as a gap.
    if ok:
        safe, reason = ai_reply_is_safe(reply)
        if not safe:
            print(f"[GUARD] discarded model reply - {reason}")
            base.reply = escalation_reply(intent, None, _hinglish(norm))
            base.source = "ESCALATION"
            base.escalated = True
            base.covered = False
            base.confidence = 0.2
            base.resolution = Resolution.ESCALATED
            base.gap_topic = gap_topic(service, intent)
            return base

    base.reply = reply
    base.source = "AI" if ok else "RULE"
    base.confidence = 0.3 if ok else 0.0
    base.covered = False
    base.resolution = Resolution.ANSWERED if ok else Resolution.UNRESOLVED
    if not ok:
        base.gap_topic = gap_topic(service, intent)
    return base


# ===========================================================================
# THE SALES COMPOSER
# ===========================================================================
# Builds a natural reply for product and service questions out of VERIFIED
# knowledge only. This replaces the two failure modes the owner reported:
# answering with an unrelated FAQ, and answering a genuine buying signal
# ("bhai mats chahiye") with "Thanks for messaging".
#
# The shape, per the brief:
#     1. direct answer        - do we do this, for your car
#     2. verified detail      - only what an approved FAQ actually states
#     3. one relevant upsell  - verified pairings, soft wording
#     4. CTA                  - the full number, once
#
# It is deliberately NOT a language model. Every sentence is assembled from
# approved text, so it cannot hallucinate, and it answers in about a
# millisecond instead of the ~65 seconds the owner measured.


def _hinglish(norm: str) -> bool:
    """Rough check for a Hinglish/Hindi message, to mirror the customer."""
    markers = ["hai", "hain", "chahiye", "karwana", "karwani", "karna",
               "karni", "kitne", "kitna", "bhai", "mera", "meri", "aur",
               "kya", "hoga", "milega", "batao", "krna", "ke liye", "ke",
               "ki", "mein", "me", "ko", "achha", "acha", "bhi", "sakta",
               "sakti", "dena", "do", "lena", "karein", "wala", "wale",
               "haan", "han", "nahi", "nhi", "kal", "aaj", "baje", "subah",
               "shaam", "abhi", "kab", "kaise", "konsa", "kaunsa", "theek",
               "thik", "bhejo", "bata", "batao", "karo", "lijiye", "dijiye",
               "hoon", "hu", "aap", "aapki", "aapka", "hum", "ji"]
    return sum(1 for m in markers if re.search(r"\b" + m + r"\b", norm)) >= 2


def _cta(hinglish: bool) -> str:
    if hinglish:
        return (f"Exact option aur price ke liye {PHONE} par WhatsApp ya "
                "call kar lijiye - team aapki car ke according confirm kar "
                "degi.")
    return (f"For the exact option and price, WhatsApp or call us on "
            f"{PHONE} and our team will confirm what suits your car.")


GFX_KEYS = ("gfx", "gfx_pro", "gfx_normal")


def _car_phrase(brand: Optional[str], model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    if model.lower() in ("bmw", "mg", "vw", "xuv", "xuv700", "xuv300"):
        model = model.upper()
    if brand and brand.lower() not in model.lower() and brand.lower() != "bmw":
        return f"{brand} {model}"
    return model


def _bullets(items: List[str]) -> str:
    return "\n".join("• " + i for i in items)


def compose_gfx_reply(product: str, brand: Optional[str], model: Optional[str],
                      intent: str, norm: str) -> str:
    """Answer any GFX question from the owner-verified GFX knowledge.

    Structure per the final brief: direct answer -> verified benefits ->
    variant difference when relevant -> price/availability status -> CTA.
    Every sentence is assembled from knowledge.py; nothing is generated.

    No price is ever stated (none is verified). No live stock is ever
    claimed (no stock source exists) - "current availability" is always the
    team's to confirm.
    """
    hin = _hinglish(norm)
    car = _car_phrase(brand, model)
    pro, normal = kb.PRODUCTS["gfx_pro"], kb.PRODUCTS["gfx_normal"]
    pro_b = pro["benefits_hi" if hin else "benefits"]
    normal_b = normal["benefits_hi" if hin else "benefits"]

    # "normal wale nahi, gfx pro wale" names both variants but rules one
    # out - that is a Pro request, not a comparison.
    rules_out_normal = contains_any(norm, ["normal wale nahi", "normal nahi",
                                           "not normal", "not the normal",
                                           "normal nhi"])
    if rules_out_normal:
        product = "gfx_pro"
    asks_compare = not rules_out_normal and (
        intent == Intent.SERVICE_COMPARISON or (
            contains_any(norm, ["normal"]) and contains_any(norm, ["pro"])))
    asks_live = contains_any(norm, ["right now", "abhi", "currently",
                                    "in stock", "stock", "aaj", "ready"])
    variant = ("GFX Pro/Lifelong" if product == "gfx_pro"
               else "GFX Normal" if product == "gfx_normal" else "GFX")
    for_car = f" for your {car}" if car else ""
    for_car_hi = f" aapki {car} ke liye" if car else ""

    # ---- CTA (single, complete number) -----------------------------------
    if hin:
        cta = (f"Exact price aur current availability confirm karne ke liye "
               f"call/WhatsApp karein: {PHONE} 🚗")
    else:
        cta = (f"For the exact fitting, price and current availability"
               f"{for_car}, call or WhatsApp us on {PHONE}. 🚗")

    # ---- Comparison ------------------------------------------------------
    if asks_compare:
        if hin:
            body = (
                "GFX Normal aur GFX Pro/Lifelong dono Car Trends par available "
                "hain.\n\nGFX Pro/Lifelong premium option hai - vehicle-specific "
                "molded fit, extensive coverage, raised edges, durable TPV/TPE "
                "material aur easy cleaning.\n\nGFX Normal simple aur "
                "budget-friendly option hai - lightweight, flexible aur clean "
                "karna easy.\n\nAapki car ke liye available variant aur exact "
                f"price team confirm kar degi: {PHONE}")
        else:
            body = (
                "GFX Normal and GFX Pro/Lifelong are both available at Car "
                "Trends.\n\nGFX Pro/Lifelong is the more premium option - "
                "vehicle-specific molded fit, extensive coverage, raised edges, "
                "durable TPV/TPE construction and easy cleaning.\n\nGFX Normal is "
                "the simpler option for customers looking for a more "
                "budget-friendly mat - lightweight, flexible and easy to clean."
                "\n\nFor your car model, our team can confirm the available "
                f"variant and exact price on {PHONE}.")
        return body

    # ---- Price: no verified figure exists, so status + CTA only ----------
    if intent == Intent.PRICE_INQUIRY:
        if hin:
            return (f"{variant} mats{for_car_hi} available option hain. Exact "
                    "price car ke model aur variant par depend karta hai - "
                    f"team confirm kar degi. Call/WhatsApp: {PHONE} 🚗")
        return (f"We offer {variant} mats{for_car}. The exact price depends on "
                "the car model and variant, so our team will confirm it for "
                f"you - call or WhatsApp {PHONE}. 🚗")

    # ---- Live-stock question: never claim stock -------------------------
    if asks_live:
        if hin:
            return (f"{variant} mats hum offer karte hain{for_car_hi}. Aapke "
                    "exact variant ka current stock team confirm kar degi - "
                    f"call/WhatsApp: {PHONE} 🚗")
        return (f"We do offer {variant} mats{for_car}. Current stock for your "
                "exact variant is something our team confirms - please call "
                f"or WhatsApp {PHONE}. 🚗")

    # ---- GFX Pro ---------------------------------------------------------
    if product == "gfx_pro":
        if hin:
            head = (f"Bilkul 👍 {car + ' ke liye ' if car else ''}GFX Pro/Lifelong "
                    "mats available option hain - premium vehicle-specific "
                    "molded floor mats.")
            return f"{head}\n\n{_bullets(pro_b)}\n\n{cta}"
        head = (f"GFX Pro/Lifelong mats are a premium vehicle-specific molded "
                f"floor-mat option at Car Trends Car Mall{for_car}. 🚗")
        return f"{head}\n\nThey offer:\n{_bullets(pro_b)}\n\n{cta}"

    # ---- GFX Normal ------------------------------------------------------
    if product == "gfx_normal":
        if hin:
            head = (f"GFX Normal mats{for_car_hi} simple aur budget-friendly "
                    "option hain.")
            return (f"{head}\n\n{_bullets(normal_b)}\n\nAgar aap custom fit aur "
                    "zyada coverage chahte hain to GFX Pro/Lifelong bhi "
                    f"available hai.\n\n{cta}")
        head = (f"GFX Normal mats are the simpler, budget-friendly GFX option"
                f"{for_car}.")
        return (f"{head}\n\n{_bullets(normal_b)}\n\nIf you want a custom "
                "molded fit and fuller coverage, GFX Pro/Lifelong is the other "
                f"variant we offer.\n\n{cta}")

    # ---- "GFX mat" with no variant: explain both, guide --------------------
    if hin:
        head = (f"Haan 👍 GFX mats{for_car_hi} available option hain. Hum do "
                "variants offer karte hain - GFX Normal aur GFX Pro/Lifelong.")
        mid = ("GFX Pro/Lifelong premium option hai: vehicle-specific molded "
               "fit, edge-to-edge coverage, durable TPV/TPE material, raised "
               "edges jo dirt aur spills contain karne mein help karte hain, "
               "anti-skid retention aur easy cleaning. GFX Normal simple, "
               "budget-friendly option hai.")
        return f"{head}\n\n{mid}\n\n{cta}"
    head = (f"Yes 👍 GFX mats are available{for_car}. We offer two variants - "
            "GFX Normal and GFX Pro/Lifelong.")
    mid = ("GFX Pro/Lifelong is the premium option: vehicle-specific molded "
           "fit, edge-to-edge coverage, durable TPV/TPE material, raised edges "
           "to help contain dirt and spills, anti-skid retention and easy "
           "cleaning. GFX Normal is the simpler, budget-friendly option.")
    return f"{head}\n\n{mid}\n\n{cta}"


def compose_product_reply(product_key: str, car_model: Optional[str],
                          intent: str, norm: str) -> Tuple[str, bool]:
    """Answer a product question. Returns (reply, is_verified_product).

    A VERIFIED product gets a real answer describing what we carry, in the
    approved wording. An UNVERIFIED one is understood but never claimed -
    the customer is told the team will confirm, and it is logged as a gap.

    Neither ever asserts live stock. No approved source states what is on
    the shelf today, so the bot does not either.
    """
    p = kb.PRODUCTS[product_key]
    hin = _hinglish(norm)
    label = p["label"]
    for_car = f" for your {car_model}" if car_model else ""

    if p["verified"]:
        if hin:
            opening = f"Haan, {label} humare paas available option hai"
            if p["detail"]:
                opening += f" - hum {p['detail']} rakhte hain"
            opening += "."
            if car_model:
                opening += (f" {car_model} ke liye exact fitting option team "
                            "confirm kar degi.")
        else:
            opening = f"Yes, {label} is something we stock"
            if p["detail"]:
                opening += f" - we carry {p['detail']}"
            opening += "."
            if car_model:
                opening += (f" Our team will confirm the exact fit"
                            f"{for_car}.")
    else:
        # Recognised and understood - but NOT confirmed anywhere. Say so in
        # plain language rather than reciting the same escalation template
        # every time, and without inventing an answer.
        related = p.get("related_verified")
        extra = ""
        if related and kb.PRODUCTS[related]["detail"]:
            rp = kb.PRODUCTS[related]
            extra = (f" {rp['detail'].capitalize()} hum zaroor rakhte hain."
                     if hin else
                     f" We do carry {rp['detail']}.")
        if hin:
            opening = (f"{label.capitalize()} ke liye main guess nahi karunga"
                       f".{extra} Team turant confirm kar degi ki ye "
                       f"{'aapki ' + car_model + ' ke liye ' if car_model else ''}"
                       "available hai ya nahi.")
        else:
            opening = (f"I'd rather check than guess on {label}.{extra} "
                       "Our team can confirm availability"
                       f"{for_car} straight away.")

    return opening, bool(p["verified"])


def compose_service_reply(service: str, car_model: Optional[str],
                          norm: str) -> str:
    """A short verified answer for a service the customer named."""
    hin = _hinglish(norm)
    label = kb.SERVICE_LABELS.get(service, service.replace("_", " ").lower())
    if hin:
        base = f"Haan, {label} hum karte hain."
        if car_model:
            base += f" Aapki {car_model} ke liye team detail confirm kar degi."
    else:
        base = f"Yes, we do {label}."
        if car_model:
            base += f" Our team will confirm the details for your {car_model}."
    return base


def escalation_reply(intent: str, note: Optional[str], hinglish: bool,
                     topic: Optional[str] = None) -> str:
    """Natural "we'll confirm that for you" wording.

    The old version pasted the same two sentences onto everything, so a
    price question came back as "Prices depend on car model and color. Send
    details to 6367857737. I don't want to give you incorrect information.
    Our team can confirm this for you. You can call or WhatsApp us on
    6367857737." - the number twice and the same template every time.

    An approved note is already a complete answer, so it is used as-is.
    """
    if note:
        note = note.strip()
        if PHONE in note:
            return note
        return (f"{note} {PHONE} par WhatsApp kar lijiye."
                if hinglish else f"{note} WhatsApp or call us on {PHONE}.")

    what = {
        Intent.PRICE_INQUIRY: ("exact price", "exact price"),
        Intent.WARRANTY: ("warranty details", "warranty ki details"),
        Intent.DURATION: ("how long it takes", "kitna time lagega"),
        Intent.AVAILABILITY_REQUEST: ("availability", "availability"),
        Intent.OFFER_DISCOUNT: ("current offers", "current offers"),
        Intent.PICKUP_DROP: ("pickup and drop", "pickup/drop"),
        Intent.SERVICE_COMPARISON: ("the right option",
                                    "aapke liye sahi option"),
    }.get(intent, ("this", "ye"))

    if hinglish:
        return (f"{what[1].capitalize()} team confirm kar degi - "
                f"{PHONE} par WhatsApp ya call kar lijiye.")
    return (f"Our team will confirm {what[0]} for you - WhatsApp or call "
            f"them on {PHONE}.")


def compose_topic_recommendation(service: Optional[str], issue: Optional[str],
                                 brand: Optional[str], model: Optional[str],
                                 norm: str) -> Optional[str]:
    """Advice INSIDE the conversation's current topic, from verified facts.

    "What would you recommend?" after a scratches conversation used to get
    the generic accessories list. Returns None when there is no service
    topic, so the generic recommendation still handles cold questions.
    """
    if not service or service in (Service.ACCESSORIES, Service.AUDIO,
                                  Service.LOCATION, Service.HOURS,
                                  Service.CONTACT, Service.GENERAL):
        return None
    hin = _hinglish(norm)
    car = _car_phrase(brand, model)
    for_car = f" on your {car}" if car else ""
    for_car_hi = f" aapki {car} ke liye" if car else ""

    if issue == "the scratches" or service in (Service.DENTING, Service.PAINTING):
        # FAQ 35 (minor scratches: compounding/polishing/paint correction)
        # and FAQ 36 (full denting-painting, Jetstar booth).
        if hin:
            return (f"Scratches{for_car_hi} ke liye do option hote hain - "
                    "minor scratches compounding, polishing aur paint "
                    "correction se theek ho jaate hain, aur deep damage ke "
                    "liye hum in-house Jetstar booth mein denting-painting "
                    "karte hain. Sahi option team dekh kar batayegi - "
                    f"call/WhatsApp {PHONE}.")
        return (f"For scratches{for_car} there are two routes - minor "
                "scratches can be removed with compounding, polishing and "
                "paint correction, while deeper damage is handled by "
                "denting-painting in our in-house Jetstar booth. Our team "
                "will check which one your car needs - call or WhatsApp "
                f"{PHONE}.")
    if service in (Service.PPF, Service.CERAMIC, Service.DETAILING):
        # FAQ 28 (both offered), FAQ 29 (prep before either).
        if hin:
            return (f"Paint protection{for_car_hi} PPF aur ceramic coating "
                    "dono hum karte hain, aur zaroorat ho to pehle "
                    "compounding/polishing se shine wapas laate hain. Konsa "
                    f"suit karega, team confirm kar degi - call/WhatsApp {PHONE}.")
        return (f"For paint protection{for_car} we offer both PPF and ceramic "
                "coating, with compounding and polishing first if the paint "
                "needs it. Our team will confirm which suits your car - call "
                f"or WhatsApp {PHONE}.")
    if service in (Service.MECHANICAL, Service.SUSPENSION, Service.TYRES):
        # FAQ 56.
        if hin:
            return (f"Service{for_car_hi} hum top-to-bottom karte hain - "
                    "engine work, brake pads/discs, AC, catalytic converter "
                    "aur O2 sensor cleaning. Car dekh kar team exact "
                    f"recommendation degi - call/WhatsApp {PHONE}.")
        return (f"For service{for_car} we do top-to-bottom work - engine, "
                "brake pads/discs, AC, catalytic converter and O2 sensor "
                "cleaning. Our team will recommend exactly what is needed "
                f"after a check - call or WhatsApp {PHONE}.")
    return None


def compose_recommendation(car_model: Optional[str], norm: str) -> str:
    """Answer "what should I get?" without ever saying "best".

    Rule 5: "best" is a claim the business has not verified, so the wording
    stays at "good option" / "achha option". Only verified categories are
    listed.
    """
    hin = _hinglish(norm)
    listed = ("7D floor mats, seat covers, wiper blades, car vacuums, "
              "speakers and Apple CarPlay, plus our own engine oil and "
              "coolant")
    if hin:
        out = (f"Aapki requirement ke according achha option suggest kar "
               f"sakte hain. Humare accessories range mein {listed} "
               "available hain.")
        if car_model:
            out += (f" {car_model} ke liye kya chahiye - interior, audio "
                    "ya care products?")
        else:
            out += " Aap apni car ka model aur requirement bata dijiye."
    else:
        out = (f"Happy to help you choose. From our accessories range we "
               f"carry {listed}.")
        if car_model:
            out += (f" What are you after for your {car_model} - interior, "
                    "audio, or car care?")
        else:
            out += " Tell me your car model and what you're after."
    return out


def _finish_sales_reply(a: "Answer", norm: str) -> str:
    """Add a single CTA if the reply does not already carry the number.

    Cross-selling was removed in the final production pass on the owner's
    instruction: the bot answers ONLY the product the customer asked about
    and never volunteers another. Kept as one function so the phone number
    can never appear twice in one message.
    """
    if PHONE in a.reply:
        return a.reply
    return f"{a.reply} {_cta(_hinglish(norm))}"


# ===========================================================================
# MULTI-QUESTION MESSAGES
# ===========================================================================
def detect_asks(norm: str, product: Optional[str]) -> List[str]:
    """The distinct things one message asks for, in a sensible answer order."""
    asks: List[str] = []
    if contains_any(norm, ["scratch", "scratches", "kharoch"]):
        asks.append("scratch")
    elif contains_any(norm, ["dent", "dents"]):
        asks.append("dent")
    if contains_any(norm, ["dull", "shine", "chamak", "faded"]):
        asks.append("dull")
    if contains_any(norm, ["ppf"]):
        asks.append("ppf")
    if contains_any(norm, ["ceramic"]):
        asks.append("ceramic")
    if product in GFX_KEYS or contains_any(norm, ["gfx"]):
        asks.append("gfx")
    if contains_any(norm, RECOMMEND_WORDS) or contains_any(
            norm, ["karwau ya", "ya ceramic", "ya ppf", "best solution"]):
        asks.append("recommend")
    duration = contains_any(norm, DURATION_WORDS) or contains_any(norm, ["kitne din"])
    # "kitna time lagega" is a DURATION question; the bare "kitna" in it must
    # not also count as a price ask, or two-part questions get mistaken for
    # three-part ones and routed to the multi-answer composer.
    strong_price = contains_any(norm, ["price", "cost", "rate", "approx",
                                       "kitna lagega", "kitna padega",
                                       "kitne ka", "kitni ka", "how much",
                                       "budget", "charges", "paisa", "paise",
                                       "quote", "estimate"])
    weak_price = contains_any(norm, ["kitna", "kitne", "lagega", "padega"])
    if strong_price or (weak_price and not duration):
        asks.append("price")
    if duration:
        asks.append("duration")
    if contains_any(norm, STRONG_BOOKING_WORDS) or (
            extract_preferred_day(norm) and contains_any(
                norm, ["drop", "de du", "de dun", "aa", "aau", "laau", "la du",
                       "dun", "book"])):
        asks.append("booking")
    if contains_any(norm, ["location", "address", "kaha ho", "kahan ho",
                           "where are you", "map bhej", "location bhej",
                           "address bhej", "directions", "kidhar ho",
                           "shop kaha", "shop kahan", "kaha h", "kaha hai",
                           "kahan hai", "kahan h", "where is your shop",
                           "where is the shop", "where exactly", "exactly kaha",
                           "shop kidhar", "kidhar hai", "kidhar h",
                           "shop where", "where h", "where hai", "exactly where",
                           "where is", "where r u", "where are u", "where u"]):
        asks.append("location")
    if contains_any(norm, HOURS_STRONG_WORDS) or contains_any(
            norm, ["kitne baje", "kab tak khule", "kab tak open"]):
        asks.append("hours")
    return asks


def compose_multi_reply(asks: List[str], brand: Optional[str],
                        model: Optional[str], year: Optional[str],
                        norm: str) -> str:
    """One reply, one line per question, only verified facts, number once."""
    hin = _hinglish(norm)
    car = _car_phrase(brand, model)
    car_full = f"{year} {car}" if (car and year) else car
    already_ceramic = contains_any(norm, ["already ceramic", "ceramic hai",
                                          "ceramic already", "pehle se ceramic"])
    no_ppf = contains_any(norm, ["ppf nahi", "no ppf", "without ppf", "not ppf"])
    lines: List[str] = []

    head = (f"{car_full} ke liye ek-ek karke:" if (hin and car_full) else
            "Ek-ek karke:" if hin else
            f"For your {car_full}, one by one:" if car_full else "One by one:")
    lines.append(head)

    if "scratch" in asks:
        lines.append(
            "• Scratches: minor scratches compounding, polishing aur paint "
            "correction se theek ho jaate hain; deep damage ke liye "
            "denting-painting (in-house Jetstar booth). Team dekh kar sahi "
            "option batayegi." if hin else
            "• Scratches: minor scratches come out with compounding, polishing "
            "and paint correction; deeper damage is handled by denting-painting "
            "in our in-house Jetstar booth. Our team will check which you need.")
    elif "dent" in asks:
        lines.append("• Dent: denting-painting in-house Jetstar booth mein hoti "
                     "hai; team damage dekh kar batayegi." if hin else
                     "• Dent: denting-painting is done in our in-house Jetstar "
                     "booth; the team will assess the damage.")
    if "dull" in asks:
        lines.append("• Dull paint: compounding aur polishing se shine wapas "
                     "aati hai." if hin else
                     "• Dull paint: compounding and polishing bring the shine back.")
    if "ppf" in asks or "ceramic" in asks or "recommend" in asks:
        if already_ceramic:
            lines.append(
                "• PPF ya ceramic: dono hum karte hain. Pehle se ceramic hai to "
                "PPF ka option team car dekh kar confirm karegi - main yahan se "
                "haan/na nahi bolunga." if hin else
                "• PPF or ceramic: we do both. Since you already have ceramic, "
                "whether PPF is the right next step is something our team will "
                "confirm after seeing the car - I won't guess that here.")
        else:
            lines.append(
                "• PPF ya ceramic: dono hum karte hain - PPF protection film hai "
                "aur ceramic coating compounding/polishing ke saath. Konsa suit "
                "karega, team car dekh kar batayegi." if hin else
                "• PPF or ceramic: we do both - PPF is a protection film, ceramic "
                "coating comes with compounding and polishing. Our team will "
                "recommend the right one after seeing the car.")
    if "gfx" in asks:
        lines.append(
            f"• GFX Pro/Lifelong mats{(' ' + car + ' ke liye') if car else ''} "
            "available option hain - vehicle-specific molded fit, raised edges, "
            "TPV/TPE material, easy cleaning." if hin else
            f"• GFX Pro/Lifelong mats{(' for the ' + car) if car else ''} are "
            "available - vehicle-specific molded fit, raised edges, TPV/TPE "
            "material, easy cleaning.")
    if "price" in asks:
        lines.append("• Price: exact ya approx price yahan se nahi de sakta - "
                     "car aur package par depend karta hai, team confirm karegi."
                     if hin else
                     "• Price: I can't give an exact or approximate figure here - "
                     "it depends on the car and package; our team will confirm.")
    if "duration" in asks:
        lines.append("• Kitne din: team confirm karegi." if hin else
                     "• Time required: our team will confirm.")
    if "booking" in asks:
        day = extract_preferred_day(norm)
        lines.append(
            f"• {'Kal' if day == 'tomorrow' else 'Drop'}: WhatsApp par apna "
            "preferred time bhej dijiye - slot team confirm karegi, yahan se "
            "confirm nahi hota." if hin else
            f"• {'Tomorrow' if day == 'tomorrow' else 'Drop-off'}: send your "
            "preferred time on WhatsApp - the slot is confirmed by our team, "
            "not here.")
    if "location" in asks:
        lines.append(f"• Location: {BUSINESS['address_short']}. Google Maps: "
                     f"{BUSINESS['maps_link']}")
    if "hours" in asks:
        lines.append(f"• Timing: {BUSINESS['hours_sentence']}")
    lines.append(f"Sab confirm karne ke liye call/WhatsApp: {PHONE} 🚗" if hin
                 else f"To confirm everything, call or WhatsApp {PHONE} 🚗")
    return "\n".join(lines)


# ===========================================================================
# PENDING INTENT - issue extraction and continuation
# ===========================================================================
# The customer's stated PROBLEM, kept as a short label so the follow-up can
# say "for the scratches on your Seltos" instead of "for denting".
ISSUE_WORDS: List[Tuple[str, List[str]]] = [
    ("the scratches", ["scratch", "scratches", "kharoch"]),
    ("the dent", ["dent", "dents", "dented"]),
    ("the AC problem", ["ac", "air conditioner", "cooling"]),
    ("the mileage drop", ["mileage", "average"]),
    ("the engine noise", ["engine noise", "engine se aawaz", "engine se awaz",
                          "engine sound", "engine me awaz", "engine me aawaz"]),
    ("the battery", ["battery"]),
    ("the brakes", ["brake", "brakes"]),
    ("the suspension", ["suspension", "shocker", "shock absorber"]),
    ("the paint", ["paint", "painting", "repaint"]),
    # a noise with no named part - kept generic so it never renames a
    # suspension/brake problem into an engine one
    ("the noise", ["aawaz", "awaz", "noise", "sound"]),
]


def extract_issue(norm: str) -> Optional[str]:
    for label, words in ISSUE_WORDS:
        if contains_any(norm, words):
            return label
    return None


# Services where the year/variant changes the quote, so it is worth one more
# question once the model is known.
QUOTE_SERVICES = {Service.PPF, Service.CERAMIC, Service.DETAILING}

_MODEL_ASK = re.compile(
    r"[^.!?]*(car model|model year|your model|exact model|send us your|"
    r"reply with your|tell me your|share your car|which car|konsi car|"
    r"car ka model)[^.!?]*[.!?]", re.I)


def _strip_model_asks(text: str) -> str:
    """Remove "please tell me your car model" sentences from a saved reply."""
    return re.sub(r"\s{2,}", " ", _MODEL_ASK.sub("", text)).strip()


def compose_both_reply(frame, car: Optional[str], hin: bool) -> str:
    """PPF + ceramic questions, from FAQ 28/29 only - no compatibility claim."""
    has_ppf = frame.facts.get("has_ppf")
    has_cer = frame.facts.get("has_ceramic_coating") or frame.facts.get("has_ceramic")
    have = ("PPF" if has_ppf else "ceramic coating" if has_cer else None)
    for_car = f" on your {car}" if car else ""
    for_car_hi = f" aapki {car} par" if car else ""
    if hin:
        base = ("Haan - PPF aur ceramic coating dono hum karte hain, aur "
                "zaroorat ho to pehle compounding/polishing bhi.")
        tail = (f" Aapke paas pehle se {have} hai, to doosra{for_car_hi} kaise "
                "lagega ye team car dekh kar confirm karegi - main yahan se "
                f"haan/na nahi kahunga. Call/WhatsApp {PHONE}."
                if have else
                f" Aapki car ke liye konsa pehle, team confirm karegi - "
                f"call/WhatsApp {PHONE}.")
        return base + tail
    base = ("Yes - we do both PPF and ceramic coating, with compounding and "
            "polishing first if the paint needs it.")
    tail = (f" Since you already have {have}, how the other one goes{for_car} "
            "is something our team confirms after seeing the car - I won't "
            f"guess that here. Call or WhatsApp {PHONE}."
            if have else
            f" Which comes first for your car, our team will confirm - call or "
            f"WhatsApp {PHONE}.")
    return base + tail


def _drop_other_car_sentences(text: str, model: Optional[str]) -> str:
    """Remove sentences that name a car other than the customer's current one.

    Safety net for remembered text: after "not fortuner, its innova" no
    sentence about a Fortuner may survive into the next reply.
    """
    if not text or not model:
        return text
    mine = set(model.lower().split())
    kept = []
    for sentence in re.split("(?<=[.!?]) ", text):
        words = set(re.findall("[a-z0-9]+", sentence.lower()))
        named = [m for m in _CAR_MODELS_SORTED if set(m.split()) <= words]
        # three or more models is a general list ("models like Creta, i10,
        # WagonR..."), not a sentence about somebody else's car
        stale = (0 < len(named) < 3
                 and not any(set(m.split()) & mine for m in named))
        if not stale:
            kept.append(sentence)
    return " ".join(kept)


def compose_continuation(state: Dict[str, Any], brand: Optional[str],
                         model: Optional[str], year: Optional[str],
                         norm: str, a: "Answer",
                         frame=None) -> Tuple[str, Optional[str]]:
    """Resume the original request now that a slot has been filled.

    Returns (reply, next_slot). Built only from what the bot already said
    (its own earlier verified reply) plus the verified composers - nothing
    new is asserted about the business.
    """
    original = state.get("original_message") or ""
    hin = _hinglish(normalise(original)) or _hinglish(norm)
    car = _car_phrase(brand, model) or "car"
    car_full = f"{year} {car}" if year else car
    product = (frame.product if frame is not None else None) or state.get("product")
    service = (frame.service if frame is not None else None) or state.get("service") or a.service
    issue = (frame.issue if frame is not None else None) or state.get("issue")

    # "Innova Crysta 2019" after a five-question message answers all five
    # for the Innova - not just the last product the message mentioned.
    if a.answer_to_pending and original:
        o_norm = normalise(original)
        o_asks = detect_asks(o_norm, product)
        o_sub = [x for x in o_asks if x not in ("ppf", "ceramic", "recommend")]
        if (len(o_asks) >= 3 and o_sub) or (len(o_asks) == 2 and "location" in o_asks):
            return compose_multi_reply(o_asks, brand, model, year, o_norm), None

    # "I already have PPF" / "Can I do both?" inside a PPF-or-ceramic topic.
    if frame is not None and (frame.both or frame.ownership_service) \
            and service in (Service.PPF, Service.CERAMIC, Service.DETAILING):
        cp = _car_phrase(brand, model)
        return compose_both_reply(frame, cp, hin), None

    # A product request resumes with the product answer for that car.
    if product in GFX_KEYS:
        return compose_gfx_reply(product, brand, model, a.intent,
                                 normalise(original)), None
    if product:
        reply, _ = compose_product_reply(product, model, a.intent,
                                         normalise(original))
        if PHONE not in reply:
            reply = f"{reply} {_cta(hin)}"
        return reply, None

    label = issue or kb.SERVICE_LABELS.get(service) or (
        "the booking" if state.get("intent") == Intent.BOOKING_REQUEST else "this")
    core = _drop_other_car_sentences(
        _strip_model_asks(state.get("core_reply") or service_intro(service)), model)
    next_slot = ("car_year" if (service in QUOTE_SERVICES and model and not year)
                 else None)

    if issue == "the scratches":
        extra = (" Team check kar legi ki compounding/polishing se theek "
                 "hoga ya denting-painting chahiye." if hin else
                 " Our team can check whether compounding/polishing or "
                 "denting-painting is the right fix.")
    else:
        extra = ""

    if hin:
        hi_label = label[4:] if label.startswith("the ") else label
        head = f"Thanks! Aapki {car_full} par {hi_label} ke liye - "
        if next_slot:
            tail = (f" Konsa year/variant hai? Isse team exact quote de "
                    f"payegi - call/WhatsApp {PHONE}.")
        else:
            tail = f" Exact assessment ke liye call/WhatsApp karein: {PHONE}."
    else:
        head = f"Thanks! For {label} on your {car_full} - "
        if next_slot:
            tail = (f" Which year/variant is it? That helps our team give an "
                    f"exact quote - call or WhatsApp {PHONE}.")
        else:
            tail = (f" For an exact assessment, call or WhatsApp us on "
                    f"{PHONE}.")

    # "swift ka price?" as a correction: the car is acknowledged AND the
    # price question is handed over - never silently replaced by the intro.
    if a.intent in NEVER_GUESS and not verified_price_faq(service, model):
        tail = " " + escalation_reply(a.intent, None, hin) + (
            (" Konsa year/variant hai?" if hin else " Which year/variant is it?")
            if next_slot else "")
    body = core[0].lower() + core[1:] if core else ""
    if PHONE in body:
        tail = (f" Konsa year/variant hai?" if hin and next_slot else
                f" Which year/variant is it?" if next_slot else "")
    return (head + body + extra + tail).replace("  ", " ").strip(), next_slot


# ===========================================================================
# LEAD COLLECTION
# ===========================================================================
# When a customer is clearly ready to buy or book but we still do not know
# which car they drive, one short follow-up question is added. It asks only
# for what is actually missing and what actually matters - the model, and
# photos when the job is visual.
#
# Three deliberate restraints:
#   * nothing is asked when the approved answer ALREADY asks for the model,
#     otherwise the customer is asked twice in the same message;
#   * nothing is ever asked on an escalation - the customer is being handed
#     to a human, and a follow-up question would muddy that;
#   * no booking is ever implied. This asks for details, it does not confirm
#     an appointment, because nothing here can actually make one.

# Services where a photo genuinely helps the team quote.
VISUAL_SERVICES = {Service.DENTING, Service.PAINTING, Service.DETAILING,
                   Service.PPF, Service.CERAMIC}

# Phrases meaning "we already asked for the car" - checked case-insensitively.
_ALREADY_ASKING = ["car model", "your model", "model year", "exact model",
                   "model/year", "your car", "your requirements",
                   "your thar", "your creta", "model and requirements"]


_ASK_PHRASES = [
    "reply with your car model", "tell me your car model",
    "send your car model", "send us your car model", "share your car model",
    "car model year", "send your exact model", "your car model for",
    "let us know your car model", "with your exact car model",
    "with your car model",           # the booking line's own ask
    "send us your requirements", "your thar's details", "which car model",
    "aapki car ka model bata", "car ka model bata",
]


def _asks_for_model(reply: str) -> bool:
    """True only when the reply genuinely ASKS for the car.

    The old check matched "your car" anywhere, so a reply that merely said
    "our team will check which one your car needs" was treated as a request
    for the model - and got "I've noted your Seltos" bolted on.
    """
    low = reply.lower()
    return any(p in low for p in _ASK_PHRASES)


def _is_generic(reply: str) -> bool:
    """The catch-all "thanks for messaging, please call us" reply."""
    return reply.lower().startswith("thanks for messaging car trends")


def lead_followup(a: "Answer") -> str:
    """The extra sentence to append, or "" when nothing should be added."""
    if a.escalated or a.source == "ESCALATION" or _is_generic(a.reply):
        return ""

    # On a booking request the booking line has already been personalised
    # with the car ("To book your Creta in..."), so an acknowledgement here
    # would repeat both the car and the phone number.
    if a.intent == Intent.BOOKING_REQUEST and a.car_model:
        return ""
    if a.answer_to_pending:              # we are continuing, not re-asking
        return ""
    if a.product in GFX_KEYS and a.car_model:
        return ""                       # composed reply already names the car

    # The customer already told us the car, but the approved answer still
    # ends with "reply with your car model". Repeating a question they just
    # answered reads badly, and the approved wording may not be altered - so
    # acknowledge what we know instead.
    if a.car_model and _asks_for_model(a.reply):
        if PHONE in a.reply:             # the CTA is already there
            return f"I've noted your {a.car_model} - no need to send it again."
        return (f"I've noted your {a.car_model} - our team will confirm the "
                f"exact details for you on WhatsApp {PHONE}.")

    # Ask for the car whenever the answer depends on it - not only when the
    # buying signal is strong. "it has too much scratches" is LOW buying
    # intent, yet nothing useful can be said about the repair without the
    # car. Asking is also what creates the pending slot the next message
    # will fill.
    if a.service in (None, Service.LOCATION, Service.HOURS, Service.CONTACT,
                     Service.GENERAL, Service.MEMBERSHIP, Service.USED_CARS):
        return ""
    if a.intent in (Intent.COMPLAINT, Intent.WARRANTY, Intent.DURATION,
                    Intent.SERVICE_COMPARISON, Intent.RECOMMENDATION,
                    Intent.GENERAL_INFORMATION, Intent.LOCATION,
                    Intent.HOURS, Intent.CONTACT, Intent.PICKUP_DROP,
                    Intent.OFFER_DISCOUNT, Intent.MEMBERSHIP_INQUIRY,
                    Intent.USED_CAR_INQUIRY):
        return ""
    if a.car_model:                      # we already know the car
        return ""
    if _asks_for_model(a.reply):         # the answer already asks for it
        return ""

    hin = _hinglish(normalise(a.reply)) and False  # reply language is mixed
    if a.service in VISUAL_SERVICES:
        return ("Could you share your car model and a couple of photos? "
                "Our team will guide you on the next step.")
    return "Could you share your car model so our team can help you further?"


# ===========================================================================
# LEAD DETECTION
# ===========================================================================
def lead_status_for(a: Answer) -> Optional[str]:
    """Decide the lead status implied by one answered message.

    Deliberately conservative: BOOKED is never set from a chat message,
    because nothing here can actually confirm a booking.
    """
    if a.intent == Intent.BOOKING_REQUEST or a.buying_intent == "HIGH":
        return LeadStatus.BOOKING_REQUESTED
    if a.escalated:
        return LeadStatus.HUMAN_REQUIRED
    if a.intent == Intent.PRICE_INQUIRY:
        return LeadStatus.PRICE_REQUESTED
    if a.buying_intent == "MEDIUM":
        return LeadStatus.INTERESTED
    return None


def is_lead(a: Answer) -> bool:
    """True when this exchange is worth recording as a sales lead."""
    return lead_status_for(a) is not None and a.service not in (
        None, Service.LOCATION, Service.HOURS, Service.CONTACT)


# ===========================================================================
# PERSISTENCE - called by both front doors
# ===========================================================================
_CUSTOMER_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _customer_lock(customer_identifier: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _CUSTOMER_LOCKS.setdefault(customer_identifier, threading.Lock())


def process(customer_identifier: str, message: str,
            use_ai: bool = True, persist: bool = True) -> Answer:
    """Full production path, serialised PER CUSTOMER.

    Two rapid messages from the same customer ("ppf karwana hai" then
    "creta 2021") run one after the other, so the second sees the state
    the first wrote - never a lost update.
    """
    with _customer_lock(customer_identifier):
        return _process_unlocked(customer_identifier, message, use_ai, persist)


def _process_unlocked(customer_identifier: str, message: str,
                      use_ai: bool = True, persist: bool = True) -> Answer:
    """Full production path: context -> answer -> store -> analytics.

    Returns the same Answer object as answer(), after everything has been
    written to the database.
    """
    if not persist:
        return answer(message, None, use_ai=use_ai)

    conversation_id = db.get_or_create_conversation(customer_identifier)
    db.add_message(conversation_id, "IN", message)

    started = time.time()
    a = answer(message, conversation_id, use_ai=use_ai)

    # What the topic remembers is the approved answer itself - never the
    # booking sentence or car phrase added around it, which would freeze a
    # car name into the memory ("To book your Fortuner in" after a
    # correction to Innova).
    core_reply = a.core_reply or (
        a.reply if (a.message_type == MT.NEW_TOPIC
                    and a.source in ("FAQ", "RULE", "MANAGER")
                    and not a.escalated and not _is_generic(a.reply)
                    and a.keep_core
                    and a.frame is not None and a.frame.has_topic) else "")

    # Ask for the one detail we still need, when it is worth asking.
    followup = lead_followup(a)
    if followup:
        a.reply = f"{a.reply} {followup}"

    # ---- persist dialogue state ------------------------------------------
    # One transition table, keyed by the message type decided in answer().
    try:
        prev = db.get_state(conversation_id) or {}
        if a.frame is not None:
            state = dialogue.transition(
                prev, a.frame, a.message_type, message, core_reply,
                a.next_slot, bool(followup) or _asks_for_model(a.reply))
        else:                              # answer() bypassed (should not happen)
            state = prev
        db.set_state(conversation_id, state)
    except Exception as error:
        print(f"[STATE] could not save state: {error!r}")

    elapsed_ms = int((time.time() - started) * 1000)

    db.add_message(conversation_id, "OUT", a.reply, source=a.source,
                   response_time_ms=elapsed_ms)

    status = lead_status_for(a)
    db.upsert_intelligence(
        conversation_id,
        primary_intent=a.intent,
        service=a.service,
        sub_topic=a.car_model,
        buying_intent=a.buying_intent,
        lead_status=status,
        booking_status=("REQUESTED"
                        if a.intent == Intent.BOOKING_REQUEST else None),
        human_escalation=1 if a.escalated else 0,
        bot_confidence=a.confidence,
        knowledge_base_coverage=1 if a.covered else 0,
        resolution_status=a.resolution,
    )

    if a.gap_topic:
        db.record_knowledge_gap(a.gap_topic, message,
                                category=a.intent, service=a.service)

    # Enrich an EXISTING lead with anything new we have learned, even when
    # this message on its own would not have created one. Otherwise the
    # customer answering "car model is Fortuner" after a booking request
    # never got the car onto their lead record.
    if not is_lead(a) and db.get_lead(conversation_id) is not None:
        if a.car_model or a.preferred_day or a.service:
            db.upsert_lead(
                conversation_id, customer_identifier,
                car_brand=a.car_brand, car_model=a.car_model,
                variant_year=a.car_year, preferred_date=a.preferred_day,
                service=a.service)

    if is_lead(a):
        db.upsert_lead(
            conversation_id, customer_identifier,
            car_brand=a.car_brand, car_model=a.car_model,
            variant_year=a.car_year, service=a.service,
            requirement=message[:400], status=status or LeadStatus.NEW,
            preferred_date=a.preferred_day,
            pickup_drop=1 if a.intent == Intent.PICKUP_DROP else 0,
        )

    return a


# ===========================================================================
# LATE INITIALISATION
# ===========================================================================
# The FAQ index was first built while this module was still importing, before
# the fuzzy vocabulary could exist. Rebuild both now so the index tokens are
# normalised exactly the way live messages will be.
_build_fuzzy_vocab()
_build_faq_index()
