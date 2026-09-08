"""Instagram Reel / post COMMENT automation - an ENTRY POINT into the existing brain.

This module is deliberately small. It does not detect intents, keep memory,
look up knowledge or decide what is verified: every comment is answered by
the same brain.process() that answers a DM, under a conversation id that
isolates each commenter on each Reel. What this module adds is the one thing
a comment needs and a DM does not - a RESPONSE POLICY for a public surface:

  * the public reply is short, generic and safe - it never repeats the
    customer's car, never quotes the private conversation, never states a
    price/stock/warranty/slot the brain did not verify, never opens with
    "Yes" under a figure the customer quoted, never pitches something that
    was not asked about;
  * the full DM-style answer (the brain's own reply) is handed back so the
    server can send it as a PRIVATE reply where Meta allows one;
  * content-free comments (emoji, spam, links, handles) get no reply at all.

    Reel comment -> Meta webhook (field "comments") -> bot.extract_comments()
      -> comments.handle_comment()  -> brain.process(conversation id, text)
      -> comments.public_reply()    -> public comment reply
      -> answer.reply               -> private reply (Meta "private replies")
"""
import re
from typing import Any, Dict, Optional

import brain
import config
import knowledge as kb
from brain import Intent, Service, contains_any

PHONE = kb.PHONE
BUSINESS = kb.BUSINESS

# Intents whose answer is a fact the brain may not know - the public reply
# never states them, it points to the DM where the team confirms.
_UNKNOWN_FACT_INTENTS = {
    Intent.PRICE_INQUIRY, Intent.WARRANTY, Intent.DURATION,
    Intent.AVAILABILITY_REQUEST, Intent.OFFER_DISCOUNT,
    Intent.SERVICE_COMPARISON, Intent.RECOMMENDATION, Intent.PICKUP_DROP,
}
# Questions about the Reel itself - the DM brain has no notion of "this reel".
_REEL_CAR_WORDS = ["reel wali car", "reel me car", "reel mein car", "video wali car",
                   "video me car", "which car is this", "what car is this",
                   "ye car konsi", "yeh car konsi", "ye konsi car", "is car ka naam",
                   "car ka naam kya", "kaunsi car hai ye", "konsi car hai ye"]
_SPAM = re.compile(r"(https?://|www\.|\.ly/|\.com\b|@[a-z0-9_.]{3,}|follow (me|us|back)|free followers|"
                   r"promo ?code|earn money|crypto|forex)", re.I)
_MEMBERSHIP_WORDS = ["membership", "member", "gold card", "gold member", "gold plan"]
_USED_WORDS = ["used car", "used cars", "second hand", "2nd hand", "purani", "puraani",
               "old car", "pre owned", "pre-owned", "resale"]
_FINANCE_WORDS = ["emi", "finance", "loan", "downpayment", "down payment", "installment",
                  "instalment", "kist"]
# "ceramic 9H hai na?", "ppf peel to nahi hogi?", "kal pakka?" - the customer
# asserts a property and asks us to confirm it: never a public "Yes".
_CLAIM_WORDS = ["hai na", "hai naa", "hain na", "right?", "correct?", "pakka", "pukka",
                "guarantee", "guaranteed", "sure?", "confirm?", "to nahi", "toh nahi",
                "nahi hogi", "nahi hoga", "nahi hota", "9h", "10h", "lifetime",
                "permanent", "original hai", "genuine hai", "asli hai"]


def conversation_key(media_id: str, commenter_id: str) -> str:
    """One conversation per commenter per Reel/post.

    Reel A and Reel B never share memory, and two commenters on the same Reel
    never share memory - exactly like two DM customers. The "comment:" prefix
    keeps these apart from DM conversations for the same Instagram user.
    """
    return f"comment:{media_id}:{commenter_id}"


def _label(a: "brain.Answer") -> Optional[str]:
    """What the customer asked about, as a short public noun phrase."""
    if a.product and a.product in kb.PRODUCTS:
        return kb.PRODUCTS[a.product]["label"]
    if a.service and a.service not in (Service.LOCATION, Service.HOURS, Service.CONTACT):
        return kb.SERVICE_LABELS.get(a.service)
    return None


def _cap(label: str) -> str:
    """Capitalise a label that starts a clause ('ceramic coating' -> 'Ceramic coating')."""
    return label[:1].upper() + label[1:] if label else label


def _thing(intent: str, hin: bool, svc: Optional[str]) -> str:
    if intent == Intent.WARRANTY and svc == Service.MEMBERSHIP:
        return "validity"
    return {
        Intent.PRICE_INQUIRY: ("price", "the exact price"),
        Intent.WARRANTY: ("warranty details", "the warranty details"),
        Intent.DURATION: ("time required", "the time required"),
        Intent.AVAILABILITY_REQUEST: ("availability", "availability"),
        Intent.OFFER_DISCOUNT: ("current offers", "the current offers"),
        Intent.SERVICE_COMPARISON: ("sahi option", "the right option"),
        Intent.RECOMMENDATION: ("sahi option", "the right option"),
        Intent.PICKUP_DROP: ("pickup/drop", "pickup and drop"),
        Intent.BOOKING_REQUEST: ("slot", "the slot"),
    }.get(intent, ("details", "the details"))[1 if not hin else 0]


def is_content_free(comment_text: str, seen: "brain.dialogue.Perception") -> bool:
    """Emoji-only, spam, links, handles, or nothing that names a topic, a car
    or a question: a business account should not answer these in public."""
    if _SPAM.search(comment_text):
        return True
    norm = seen.norm.strip()
    if not norm or not re.search("[a-z]", norm):
        return True                                  # emoji / punctuation only
    has_question = "?" in comment_text or seen.has_followup or bool(seen.asks)
    names_anything = seen.names_topic or bool(seen.model) or seen.is_side \
        or seen.intent not in (Intent.UNKNOWN, Intent.OTHER)
    return not (has_question or names_anything)


def public_reply(a: "brain.Answer", comment_text: str) -> str:
    """The short PUBLIC reply for a comment, built only from the Answer's
    verified fields. Nothing customer-specific, nothing unverified."""
    seen = brain.perceive(comment_text)
    norm = seen.norm
    # The conversation's language, not just this one word's ("wagonr").
    lang = (a.frame.language if a.frame is not None else None) or seen.language
    hin = lang == "hi"
    svc, intent = a.service, a.intent
    label = _label(a)
    # A too-short comment ("kitna?", "50% discount?") is answered by the
    # brain with a clarifier and intent OTHER; the comment's own question
    # still decides the public wording. Perception only - no memory.
    if intent in (Intent.OTHER, Intent.UNKNOWN) and seen.intent in _UNKNOWN_FACT_INTENTS:
        intent = seen.intent
    # A figure in the comment ("ceramic 15000 me?") makes it a price question:
    # a public "Yes" under a quoted figure reads as accepting it.
    if brain.mentions_price_figure(norm) and intent not in (
            Intent.COMPLAINT, Intent.LOCATION, Intent.HOURS, Intent.CONTACT):
        intent = Intent.PRICE_INQUIRY
    car_known = bool(a.car_model)
    both = Service.PPF in seen.services and Service.CERAMIC in seen.services
    finance = contains_any(norm, _FINANCE_WORDS)
    claim = (contains_any(norm, _CLAIM_WORDS) or contains_any(comment_text.lower(), _CLAIM_WORDS)
             or comment_text.strip().lower().endswith(" na?") or comment_text.strip().lower().endswith(" na"))
    if claim and intent not in (Intent.COMPLAINT, Intent.LOCATION, Intent.HOURS, Intent.CONTACT,
                                Intent.BOOKING_REQUEST) and intent not in _UNKNOWN_FACT_INTENTS:
        intent = Intent.AVAILABILITY_REQUEST      # "is it X?" is a fact to confirm, not a yes
    if intent == Intent.PICKUP_DROP:
        label = None                     # "home service" is not a service label
    booking = (intent == Intent.BOOKING_REQUEST
               or a.resolution == brain.Resolution.BOOKING_REQUESTED)

    # 0. A question about the Reel's own car - not something the brain knows.
    if contains_any(norm, brain.REEL_CAR_WORDS) or contains_any(comment_text.lower(), brain.REEL_CAR_WORDS):
        text = ("Hi! DM karein, team is car ki details share kar degi." if hin else
                "Hi! DM us and our team will share the details of this car.")

    # 1. A complaint never gets a sales line in public.
    elif intent == Intent.COMPLAINT:
        text = ("Sorry to hear that - please DM us, hamari team turant dekh legi."
                if hin else
                "We're sorry to hear that - please DM us and our team will sort it out right away.")

    # 2. A booking wish beats the hours line: never confirm anything in public.
    elif booking:
        ask = ("apna preferred day" if car_known else "apna car model aur preferred day") if hin \
            else ("your preferred day" if car_known else "your car model and preferred day")
        text = (f"Hi! {_cap(label) + ' ke liye ' if label else ''}{ask} DM karein - slot hamari "
                "team confirm karegi." if hin else
                f"Hi! Please DM us {ask}{' for ' + label if label else ''} - our team will "
                "confirm the slot there.")

    # 3. Business facts that are public anyway.
    elif intent == Intent.LOCATION or svc == Service.LOCATION:
        # Link-free on purpose: Instagram's spam controls may hide comments
        # with links. The map link goes in the private reply.
        text = (f"Hi! Hum {BUSINESS['address_short']} par hain 📍 - map link ke liye DM karein."
                if hin else
                f"Hi! We're at {BUSINESS['address_short']} 📍 - DM us for the map link.")
    elif intent == Intent.HOURS or svc == Service.HOURS:
        text = f"Hi! {BUSINESS['hours_sentence']}"
    elif intent == Intent.CONTACT or svc == Service.CONTACT:
        text = (f"Hi! {PHONE} par call ya WhatsApp kar lijiye 📞" if hin else
                f"Hi! Call or WhatsApp us on {PHONE} 📞")

    # 4. The only two verified prices - stated only when the brain stated
    #    them AND the comment actually asked about that thing (no pivoting
    #    a free-extinguisher or EMI question into a price pitch).
    elif finance:
        text = ("Hi! Finance/EMI options ke liye DM karein - team wahin details confirm karegi."
                if hin else
                "Hi! Please DM us for the finance/EMI options - our team will confirm the details there.")
    elif (svc == Service.MEMBERSHIP and "2,999" in a.reply and not a.escalated
          and contains_any(norm, _MEMBERSHIP_WORDS)
          and intent in (Intent.PRICE_INQUIRY, Intent.MEMBERSHIP_INQUIRY)):
        text = ("Hi! Gold Membership Rs 2,999 ki hai - full benefits ke liye DM karein."
                if hin else
                "Hi! Gold Membership is Rs 2,999 - DM us for the full benefits.")
    elif (svc == Service.USED_CARS and "99,000" in a.reply and not a.escalated
          and contains_any(norm, _USED_WORDS)
          and intent in (Intent.PRICE_INQUIRY, Intent.USED_CAR_INQUIRY)):
        text = ("Hi! Used cars Rs 99,000 se start hoti hain - current options ke liye DM karein."
                if hin else
                "Hi! Used cars start from Rs 99,000 - DM us for the current options.")

    # 5. PPF-or-ceramic: we do both, the team advises.
    elif both and intent in (Intent.SERVICE_COMPARISON, Intent.RECOMMENDATION,
                             Intent.SERVICE_INQUIRY, Intent.OTHER, Intent.UNKNOWN):
        text = ("Hi! PPF aur ceramic coating dono hum karte hain - apna car model DM "
                "karein, team sahi option suggest karegi." if hin else
                "Hi! We do both PPF and ceramic coating - DM us your car model and "
                "our team will suggest the right one.")

    # 6. Facts the brain did not verify: point to the DM, state nothing.
    elif a.escalated or intent in _UNKNOWN_FACT_INTENTS or svc in (Service.MEMBERSHIP, Service.USED_CARS):
        thing = _thing(intent, hin, svc)
        need_car = (not car_known and svc not in (Service.MEMBERSHIP, Service.USED_CARS)
                    and intent != Intent.PICKUP_DROP)
        if label and need_car:
            text = (f"Hi! {_cap(label)} ke liye apna car model DM karein - {thing} team wahin confirm karegi."
                    if hin else
                    f"Hi! For {label}, please DM us your car model - our team will confirm {thing} there.")
        elif label:
            text = (f"Hi! {_cap(label)} ke liye DM karein - {thing} team wahin confirm karegi."
                    if hin else
                    f"Hi! For {label}, please DM us - our team will confirm {thing} there.")
        elif need_car:
            text = (f"Hi! Apna car model aur requirement DM karein - {thing} team wahin confirm karegi."
                    if hin else
                    f"Hi! Please DM us your car model and what you need - our team will confirm {thing} there.")
        else:
            text = (f"Hi! Kya chahiye DM karein - {thing} team wahin confirm karegi."
                    if hin else
                    f"Hi! Please DM us what you need - our team will confirm {thing} there.")

    # 7. Something we verifiably do / carry - answered, not pitched.
    elif label:
        verb_en = "have" if a.product else "do"
        verb_hi = "rakhte" if a.product else "karte"
        if car_known:
            text = (f"Hi! Haan, {label} hum aapki car ke liye {verb_hi} hain - details ke liye DM karein."
                    if hin else
                    f"Hi! Yes, we {verb_en} {label} for your car - DM us for the details.")
        else:
            text = (f"Hi! Haan, {label} hum {verb_hi} hain - apna car model DM karein, details wahin milengi."
                    if hin else
                    f"Hi! Yes, we {verb_en} {label}. Please DM us your car model for the details.")

    # 8. Too little to go on - and never ask for a car the brain already has.
    elif car_known:
        text = ("Hi! Kya chahiye DM karein - hum turant help karenge."
                if hin else
                "Hi! Please DM us what you need for your car - we'll help right away.")
    else:
        text = ("Hi! Apna car model aur kya chahiye DM karein - hum turant help karenge."
                if hin else
                "Hi! Please DM us your car model and what you need - we'll help right away.")

    return _tidy(text)


def _tidy(text: str) -> str:
    """One line, capped, and the business number at most once."""
    text = " ".join(text.split())
    if text.count(PHONE) > 1:
        first = text.index(PHONE) + len(PHONE)
        text = text[:first] + text[first:].replace(PHONE, "our number")
    limit = config.COMMENT_MAX_LENGTH
    if len(text) > limit:
        text = text[:limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def handle_comment(comment_id: str, commenter_id: str, media_id: str,
                   text: str, use_ai: bool = True) -> Dict[str, Any]:
    """Answer one comment with the existing brain and shape the two replies.

    Returns {"answer": Answer|None, "public": str, "private": str,
             "conversation": str, "skipped": bool}. Empty strings mean "send
    nothing". The caller decides how to deliver (public reply / private
    reply). Duplicate protection lives in bot.py's webhook (the single
    production caller) so that a comment id is claimed exactly once.
    """
    key = conversation_key(media_id, commenter_id)
    seen = brain.perceive(text)
    if is_content_free(text, seen):
        print(f"[COMMENT] content-free comment on {media_id} - no reply.")
        return {"answer": None, "public": "", "private": "", "conversation": key,
                "comment_id": comment_id, "skipped": True}
    answer = brain.process(key, text, use_ai=use_ai)
    private = answer.reply                 # the full DM-style answer (the brain
                                           # hands a "which car is this?" over itself)
    if answer.intent == Intent.LOCATION or answer.service == Service.LOCATION:
        private = brain.LOCATION_REPLY     # verified address WITH the map link
    return {
        "answer": answer,
        "public": public_reply(answer, text),
        "private": private,
        "conversation": key,
        "comment_id": comment_id,
        "skipped": False,
    }
