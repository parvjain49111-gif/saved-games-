"""
=============================================================================
 Car Trends Car Mall - DIALOGUE STATE LAYER
=============================================================================

 WHY THIS MODULE EXISTS
 ----------------------
 Every memory bug the bot has had - forgetting why it asked for the car,
 restarting after a one-word answer, "price?" becoming a new enquiry,
 "I already have PPF" becoming a PPF request - came from the same defect:
 the TYPE of the incoming message was never decided. Each of ~30 reply
 branches guessed for itself whether to trust the message, the saved
 state, or a scan of old messages, and the persistence code guessed again.

 This module makes that decision exactly once, in three pure steps:

     Perception   (what THIS message says, on its own)
         |
         v
     classify()   -> one MessageType         decided ONCE, by ordered rules
         |
         v
     resolve()    -> one Frame               the resolved meaning: topic +
         |                                   car + facts, from message+state
         v
     ...brain.py composes the reply from the Frame only...
         |
         v
     transition() -> next persisted state    a table keyed by MessageType

 Priority when resolving (the owner's rule):
     1. current message  2. pending slot / pending intent
     3. active conversation state  4. verified knowledge  5. AI wording

 Nothing here imports brain.py or touches the database: it is data in,
 data out, so it can be tested exhaustively without a conversation.
=============================================================================
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# MESSAGE TYPES
# ---------------------------------------------------------------------------
class MessageType:
    SLOT_ANSWER = "SLOT_ANSWER"        # answers the slot the bot asked for
    CONTINUATION = "CONTINUATION"      # more about the current topic
    NEW_TOPIC = "NEW_TOPIC"            # introduces a service/product/problem
    CORRECTION = "CORRECTION"          # replaces the car/year, keeps the topic
    SIDE_QUESTION = "SIDE_QUESTION"    # location/hours/contact aside
    AMBIGUOUS = "AMBIGUOUS"            # too little to act on - ask
    CANCEL = "CANCEL"                  # "forget that"


# Intents that ASK something about the current topic rather than name a new
# one. "How much?" is a price question about whatever we were discussing.
FOLLOWUP_INTENTS = {
    "PRICE_INQUIRY", "DURATION", "AVAILABILITY_REQUEST", "BOOKING_REQUEST",
    "RECOMMENDATION", "SERVICE_COMPARISON", "WARRANTY", "OFFER_DISCOUNT",
    "PICKUP_DROP",
}

SIDE_SERVICES = {"LOCATION", "CONTACT", "HOURS"}
GFX_FAMILY = {"gfx", "gfx_pro", "gfx_normal"}


# ---------------------------------------------------------------------------
# PERCEPTION - everything the current message says on its own
# ---------------------------------------------------------------------------
@dataclass
class Perception:
    text: str
    norm: str
    n_tokens: int
    service: Optional[str] = None
    product: Optional[str] = None
    intent: str = "UNKNOWN"
    brand: Optional[str] = None
    model: Optional[str] = None
    year: Optional[str] = None
    issue: Optional[str] = None
    day: Optional[str] = None
    language: str = "en"
    is_affirm: bool = False
    is_cancel: bool = False
    is_side: bool = False
    is_complaint: bool = False
    is_hours_question: bool = False           # "are you open on sunday?"
    negated_services: tuple = ()              # services the message rules OUT
    services: tuple = ()                      # every service the message names
    both: bool = False
    ownership_service: Optional[str] = None   # "I already have PPF"
    gfx_refinement: Optional[str] = None      # "Pro." / "normal"
    asks: List[str] = field(default_factory=list)

    @property
    def names_topic(self) -> bool:
        """Does the message itself introduce a service, product or problem?"""
        return bool(self.product or self.issue or self.gfx_refinement or (
            self.service and self.service not in SIDE_SERVICES))

    @property
    def has_followup(self) -> bool:
        return self.intent in FOLLOWUP_INTENTS


# ---------------------------------------------------------------------------
# FRAME - the resolved meaning of the turn
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    mtype: str
    intent: str = "UNKNOWN"
    service: Optional[str] = None
    product: Optional[str] = None
    issue: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    year: Optional[str] = None
    language: str = "en"
    facts: Dict[str, Any] = field(default_factory=dict)  # has_ppf, has_ceramic
    original_message: str = ""
    core_reply: str = ""
    pending_slot: Optional[str] = None      # what was pending BEFORE this turn
    topic_intent: str = "UNKNOWN"           # the topic's own intent (state)
    both: bool = False
    ownership_service: Optional[str] = None
    model_named: bool = False               # the car came from THIS message

    @property
    def has_topic(self) -> bool:
        return bool(self.service or self.product or self.issue)


# ---------------------------------------------------------------------------
# CLASSIFY - decided once, by ordered rules about message STRUCTURE
# ---------------------------------------------------------------------------
def _family(service: str) -> set:
    """Services that are one topic to the customer."""
    if service in ("DENTING", "PAINTING"):
        return {"DENTING", "PAINTING"}
    return {service}


def classify(p: Perception, state: Dict[str, Any]) -> str:
    topic = bool(state.get("intent") or state.get("service")
                 or state.get("product") or state.get("issue"))
    pending = state.get("pending_slot")
    # A slot PROPOSAL is "haan", "ok", or a day/time offered on its own -
    # not "are you open on Sunday?" (an hours question that mentions a day)
    # and not "ok adress bhejo" (an aside that starts with "ok").
    pure_affirm = p.is_affirm and p.n_tokens <= 2 and not p.service
    proposes_slot = (pure_affirm
                     or (bool(p.day) and not p.is_hours_question
                         and p.service in (None, "HOURS")))

    # 1. "Forget that" - only when nothing new is named in the same breath.
    if p.is_cancel and not p.names_topic:
        return MessageType.CANCEL

    # 2. Location / hours / contact asides. Inside a live conversation,
    #    "haan kal 11 baje" also looks like an hours question because of
    #    "baje"; a proposed day/time with a topic is a continuation.
    if p.is_side and not p.names_topic and not (topic and proposes_slot):
        return MessageType.SIDE_QUESTION

    # 3. No conversation yet.
    if not topic:
        if p.names_topic or p.is_complaint:
            return MessageType.NEW_TOPIC
        if p.model and p.intent in ("UNKNOWN", "OTHER"):
            return MessageType.NEW_TOPIC        # "I have a Creta." - car only
        if p.intent == "BOOKING_REQUEST":
            return MessageType.NEW_TOPIC        # generic booking info is safe
        # Ambiguity is a property of SHORT messages - "price?", "yes", "for
        # my car". A full sentence that names nothing we recognise is still
        # a real question and must reach the FAQ matcher / model, which
        # handle the long tail ("laptop rakhne ke liye piche kuch hai?").
        if p.n_tokens <= 3:
            return MessageType.AMBIGUOUS
        return MessageType.NEW_TOPIC

    # 4. A statement that the customer already HAS a service is a fact about
    #    the current topic, never a request for that service.
    if p.ownership_service:
        return MessageType.CONTINUATION

    # 4b. The customer ruled the current service OUT ("paint nahi, ceramic
    #     better hoga na?"): naming another service is a new topic, ruling
    #     it out alone is a cancel. Denting and painting are one family.
    cur = state.get("service")
    if cur and any(n in _family(cur) for n in p.negated_services):
        return MessageType.NEW_TOPIC if p.names_topic else MessageType.CANCEL

    # 5. A comparison or "both" question spans two services by nature; it
    #    belongs to the current conversation ONLY if it involves the current
    #    service - "ppf aur ceramic dono karte ho?" in a denting topic is a
    #    new topic.
    if p.intent == "SERVICE_COMPARISON" or p.both:
        cur = state.get("service")
        if not cur or not p.services or any(x in _family(cur) for x in p.services):
            return MessageType.CONTINUATION
        return MessageType.NEW_TOPIC

    # 6. A different service, product or problem = new topic. Refining a
    #    GFX variant ("Pro.") stays inside the GFX topic.
    same_gfx_family = (p.product in GFX_FAMILY
                       and state.get("product") in GFX_FAMILY)
    if p.product and p.product != state.get("product") and not same_gfx_family:
        return MessageType.NEW_TOPIC
    if p.issue and p.issue != state.get("issue"):
        return MessageType.NEW_TOPIC
    if (p.service and p.service not in SIDE_SERVICES
            and p.service not in _family(state.get("service") or "")
            and not (p.product and same_gfx_family)):
        return MessageType.NEW_TOPIC

    # 7. The car corrected, topic kept.
    if p.model and state.get("car_model") \
            and p.model.lower() != str(state.get("car_model")).lower() \
            and not p.names_topic:
        return MessageType.CORRECTION

    # 8. Filling the slot the bot asked for (or volunteering car/year).
    if pending == "car_model" and p.model:
        return MessageType.SLOT_ANSWER
    if pending == "car_year" and (p.year or p.model):
        return MessageType.SLOT_ANSWER
    if (p.model or p.year) and not p.names_topic \
            and p.intent in ("UNKNOWN", "OTHER"):
        return MessageType.SLOT_ANSWER

    # 9. Everything else with a topic continues it: "How much?", "Full
    #    body.", "haan", "Pro.", "Can I do both?".
    return MessageType.CONTINUATION


# ---------------------------------------------------------------------------
# RESOLVE - one frame from message + state, by message type
# ---------------------------------------------------------------------------
def resolve(p: Perception, state: Dict[str, Any], mtype: str,
            brand_for_model=None) -> Frame:
    """brand_for_model: callable(model) -> brand, supplied by brain.py."""
    f = Frame(mtype=mtype, language=p.language, both=p.both,
              ownership_service=p.ownership_service,
              pending_slot=state.get("pending_slot"),
              topic_intent=state.get("intent") or "UNKNOWN",
              facts=dict(state.get("facts") or {}))

    def carry_car(prefer_message: bool = True) -> None:
        if prefer_message and p.model:
            f.model = p.model
            f.brand = p.brand or (brand_for_model(p.model)
                                  if brand_for_model else None)
            # A different car makes the old year meaningless.
            same = bool(state.get("car_model")) and \
                str(state.get("car_model")).lower() == p.model.lower()
            f.year = p.year or (state.get("car_year") if same else None)
        else:
            f.brand, f.model = state.get("car_brand"), state.get("car_model")
            f.year = p.year or state.get("car_year")

    if mtype == MessageType.CANCEL:
        carry_car(prefer_message=False)
        f.intent = "OTHER"
        return f

    if mtype == MessageType.SIDE_QUESTION:
        f.intent, f.service = p.intent, p.service   # answer the aside
        carry_car(prefer_message=True)               # "kaha ho? creta hai meri"
        return f

    if mtype == MessageType.AMBIGUOUS:
        f.intent = p.intent if p.has_followup else "OTHER"
        carry_car()
        return f

    if mtype == MessageType.NEW_TOPIC:
        f.intent, f.service, f.product, f.issue = (p.intent, p.service,
                                                   p.product, p.issue)
        f.original_message = p.text
        f.facts = {}
        if p.ownership_service:                      # "i already have ppf, ..."
            f.facts["has_" + p.ownership_service.lower()] = True
        # "price?" (too short to act on) and then "for ceramic coating": the
        # question arrived one message before its topic - it is not lost.
        # Only a message that NAMES a topic may inherit it; "creta" alone
        # stays a car, not a price topic.
        if (p.names_topic
                and state.get("last_message_type") == MessageType.AMBIGUOUS
                and state.get("pending_intent") in FOLLOWUP_INTENTS
                and p.intent in ("UNKNOWN", "OTHER", "SERVICE_INQUIRY",
                                 "ACCESSORY_INQUIRY", "GENERAL_INFORMATION",
                                 "USED_CAR_INQUIRY", "MEMBERSHIP_INQUIRY")):
            f.intent = state["pending_intent"]
        carry_car()                                  # same customer, same car
        return f

    # ---- SLOT_ANSWER / CORRECTION / CONTINUATION: the topic is the state's
    f.service = state.get("service")
    f.product = state.get("product")
    f.issue = state.get("issue")
    f.original_message = state.get("original_message") or p.text
    f.core_reply = state.get("core_reply") or ""
    # A one-word turn ("wagonr", "haan") has no language of its own: keep the
    # conversation's, so every reply stays in the customer's language.
    if p.n_tokens <= 3 and state.get("language"):
        f.language = state["language"]

    # "Pro." narrows the variant - also when the customer only said "mats"
    # so far, because the only Pro/Normal mats we sell are the GFX ones.
    if p.gfx_refinement and f.product in GFX_FAMILY | {"floor_mats"}:
        f.product = p.gfx_refinement
    elif p.product and p.product in GFX_FAMILY and f.product in GFX_FAMILY:
        f.product = p.product

    if p.ownership_service:
        f.facts["has_" + p.ownership_service.lower()] = True

    # The message's own question wins; otherwise the topic's intent.
    if p.has_followup or p.intent == "COMPLAINT":
        f.intent = p.intent
    else:
        f.intent = state.get("intent") or "SERVICE_INQUIRY"

    carry_car(prefer_message=True)
    return f


# ---------------------------------------------------------------------------
# TRANSITION - the next persisted state, by message type
# ---------------------------------------------------------------------------
def transition(prev: Dict[str, Any], f: Frame, mtype: str, message: str,
               core_reply: str, next_slot: Optional[str],
               reply_asks_model: bool) -> Dict[str, Any]:
    prev = dict(prev or {})

    if mtype == MessageType.CANCEL:
        state: Dict[str, Any] = {}
    elif mtype in (MessageType.SIDE_QUESTION, MessageType.AMBIGUOUS):
        state = prev                                  # topic untouched
    elif mtype == MessageType.NEW_TOPIC:
        # A car-only message ("I have an Alto.") is not a topic: storing
        # UNKNOWN as the intent would make the next "price?" look like a
        # continuation of nothing instead of an ambiguous question.
        topic_intent = f.intent if f.intent not in ("UNKNOWN", "OTHER") else None
        state = {"intent": topic_intent, "service": f.service,
                 "product": f.product, "issue": f.issue,
                 "original_message": message, "core_reply": core_reply,
                 "facts": dict(f.facts)}
    else:                                             # slot / correction / continuation
        state = prev
        state["service"], state["product"], state["issue"] = (
            f.service, f.product, f.issue)
        state["facts"] = f.facts
        # The topic's intent is the ORIGINAL request, not this turn's
        # follow-up question - "How much?" must not turn scratches into a
        # price topic.
        state["intent"] = prev.get("intent") or f.intent
        if not state.get("core_reply"):
            state["core_reply"] = core_reply

    # Car facts: the frame already applied the current-message-wins rule.
    for key, val in (("car_model", f.model), ("car_brand", f.brand),
                     ("car_year", f.year)):
        if val:
            state[key] = val
        elif key == "car_year" and f.model and not f.year \
                and mtype in (MessageType.CORRECTION, MessageType.NEW_TOPIC):
            state.pop(key, None)                      # new car, old year gone

    # Was the car named inside THIS topic (so a price question is about it)?
    state["car_named_in_topic"] = bool(f.model) and (
        bool(f.model_named)
        or (mtype not in (MessageType.NEW_TOPIC, MessageType.CANCEL)
            and bool(prev.get("car_named_in_topic")))
        or (mtype == MessageType.NEW_TOPIC
            and prev.get("last_message_type") == MessageType.AMBIGUOUS
            and bool(prev.get("car_named_in_topic"))))

    # Pending slot for the NEXT turn.
    if mtype in (MessageType.SIDE_QUESTION, MessageType.AMBIGUOUS):
        state["pending_slot"] = prev.get("pending_slot")
    elif next_slot:
        state["pending_slot"] = next_slot
    elif reply_asks_model and not state.get("car_model"):
        state["pending_slot"] = "car_model"
    else:
        state["pending_slot"] = None
    state["pending_intent"] = state.get("intent") if state.get("pending_slot") else None
    state["pending_question"] = (
        {"car_model": "car model", "car_year": "year/variant"}
        .get(state["pending_slot"]) if state["pending_slot"] else None)

    state["language"] = f.language
    state["last_user_message"] = message
    state["last_message_type"] = mtype
    # The question behind a too-short message ("price?") waits ONE turn for
    # its topic ("for ceramic coating"); anything else clears it.
    if mtype == MessageType.AMBIGUOUS:
        state["pending_intent"] = f.intent if f.intent in FOLLOWUP_INTENTS else None
    else:
        state.pop("pending_intent", None)
    return state
