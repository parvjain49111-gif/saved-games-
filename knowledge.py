"""
=============================================================================
 Car Trends Car Mall - CENTRALISED KNOWLEDGE LAYER
=============================================================================

 This module is the single source of business truth. Nothing anywhere else
 in the project is allowed to state a business fact.

 KNOWLEDGE PRIORITY (highest wins):

     1. MANAGER_DATA        - manager-approved answers (added later)
     2. APPROVED_FAQS       - the 99 approved FAQ entries
     3. BUSINESS            - verified core business information
     4. conversation context
     5. safe intent understanding
     6. the language model, and ONLY for wording - never for facts

 THE MISSING-INFORMATION RULE
 ----------------------------
 An FAQ whose confidence is "Missing Information" is NOT an answer. It is a
 recorded admission that we do not know. Those entries carry
 confirmed=False and escalation_required=True, and the bot must hand the
 customer to the human team rather than guess.

 ADDING KNOWLEDGE LATER
 ----------------------
 * A new approved FAQ  -> append one dict to APPROVED_FAQS.
 * Manager answers     -> fill in the matching MANAGER_DATA section.
 Neither requires a single change to the chatbot logic.
=============================================================================
"""

from typing import Any, Dict, List, Optional

# ===========================================================================
# SERVICE AND INTENT TAXONOMY
# ===========================================================================
# Plain string constants rather than enums so they survive being written to
# the database and read back without conversion.


class Service:
    LOCATION = "LOCATION"
    HOURS = "HOURS"
    CONTACT = "CONTACT"
    PPF = "PPF"
    CERAMIC = "CERAMIC_COATING"
    DETAILING = "DETAILING"
    CAR_WASH = "CAR_WASH"
    DENTING = "DENTING"
    PAINTING = "PAINTING"
    ALLOY_SALES = "ALLOY_WHEELS"
    MECHANICAL = "MECHANICAL"
    TYRES = "TYRES"
    SUSPENSION = "SUSPENSION"
    ACCESSORIES = "ACCESSORIES"
    AUDIO = "AUDIO"
    SPARE_PARTS = "SPARE_PARTS"
    USED_CARS = "USED_CARS"
    MEMBERSHIP = "GOLD_MEMBERSHIP"
    GENERAL = "GENERAL"


class Intent:
    LOCATION = "LOCATION"
    CONTACT = "CONTACT"
    HOURS = "HOURS"
    PRICE_INQUIRY = "PRICE_INQUIRY"
    SERVICE_INQUIRY = "SERVICE_INQUIRY"
    BOOKING_REQUEST = "BOOKING_REQUEST"
    SERVICE_COMPARISON = "SERVICE_COMPARISON"
    AVAILABILITY_REQUEST = "AVAILABILITY_REQUEST"
    PICKUP_DROP = "PICKUP_DROP"
    WARRANTY = "WARRANTY"
    AFTERCARE = "AFTERCARE"
    OFFER_DISCOUNT = "OFFER_DISCOUNT"
    COMPLAINT = "COMPLAINT"
    GENERAL_INFORMATION = "GENERAL_INFORMATION"
    ACCESSORY_INQUIRY = "ACCESSORY_INQUIRY"
    USED_CAR_INQUIRY = "USED_CAR_INQUIRY"
    MEMBERSHIP_INQUIRY = "MEMBERSHIP_INQUIRY"
    DURATION = "DURATION"
    RECOMMENDATION = "RECOMMENDATION"
    UNKNOWN = "UNKNOWN"
    OTHER = "OTHER"


class LeadStatus:
    NEW = "NEW"
    QUALIFIED = "QUALIFIED"
    INTERESTED = "INTERESTED"
    PRICE_REQUESTED = "PRICE_REQUESTED"
    BOOKING_REQUESTED = "BOOKING_REQUESTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    BOOKED = "BOOKED"
    COMPLETED = "COMPLETED"
    LOST = "LOST"


class Resolution:
    ANSWERED = "ANSWERED"
    ESCALATED = "ESCALATED"
    UNRESOLVED = "UNRESOLVED"
    UNKNOWN = "UNKNOWN"
    LEAD_CREATED = "LEAD_CREATED"
    BOOKING_REQUESTED = "BOOKING_REQUESTED"


# ===========================================================================
# VERIFIED CORE BUSINESS INFORMATION
# ===========================================================================
# NOTE ON THE ADDRESS  (owner-confirmed, supersedes the FAQ wording)
# -------------------------------------------------------------------
# The FAQ dataset as supplied said Mansarovar. The owner has since
# confirmed that Mansarovar is OUT OF DATE and that the Dholai address
# below is the one and only current address. Every FAQ answer that named
# Mansarovar has been rewritten accordingly - the non-location wording of
# those answers is untouched.
#
# "Mansarovar" is retained ONLY in `former_area`, and only so the bot can
# RECOGNISE a customer asking about the old place and correct them. It must
# never be presented as where we are. See FORMER_AREA_WORDS in brain.py.
BUSINESS: Dict[str, Any] = {
    "name": "Car Trends Car Mall",
    "tagline": "EVERYTHING UNDER ONE ROOF - from dent to detail",
    "address": (
        "Opposite ISKCON Temple, Kharbas Cir Rd, Ganesh Nagar, "
        "Radha Kunj, Dholai, Jaipur, Rajasthan 302020"
    ),
    "address_short": "Opp. ISKCON Temple, Dholai, Jaipur",
    "area": "Dholai, Jaipur",
    # Recognition only - NEVER shown as the current address.
    "former_area": "Mansarovar",
    "maps_link": "https://share.google/wpdr1sLrEPRxZGtFD",
    "phone": "6367857737",
    "days": "7 days a week, Monday through Sunday",
    "hours": "10:00 AM to 8:00 PM",
    "hours_sentence": (
        "We are open 7 days a week, Monday through Sunday, "
        "from 10:00 AM to 8:00 PM."
    ),
    "brands": "Blaupunkt, Moco and other major automotive accessory brands",
    "category": "car accessories, car upgrades and car care products",
    "has_website": False,
}

PHONE: str = BUSINESS["phone"]


# ===========================================================================
# MANAGER DATA - the future knowledge slot
# ===========================================================================
# Deliberately EMPTY. Every key below is a place where manager-approved
# information will be dropped in once the questionnaire comes back. Empty
# means "not known", and the bot escalates rather than guessing.
#
# To add knowledge later, for example:
#     MANAGER_DATA["warranties"]["ppf"] = "5 years on all PPF films."
# The chatbot picks it up immediately - it is consulted before the FAQs.
# ===========================================================================
MANAGER_DATA: Dict[str, Dict[str, str]] = {
    "services": {},
    "pricing": {},
    "warranties": {},
    "packages": {},
    "offers": {},
    "booking_rules": {},
    "pickup_drop": {},
    "products": {},
    "policies": {},
    "other": {},
}

# Maps a MANAGER_DATA section to the (service, intent) pair it answers, so a
# manager answer is found automatically once it is filled in.
MANAGER_LOOKUP_KEYS: Dict[str, str] = {
    Intent.PRICE_INQUIRY: "pricing",
    Intent.WARRANTY: "warranties",
    Intent.OFFER_DISCOUNT: "offers",
    Intent.BOOKING_REQUEST: "booking_rules",
    Intent.PICKUP_DROP: "pickup_drop",
    Intent.MEMBERSHIP_INQUIRY: "packages",
    Intent.ACCESSORY_INQUIRY: "products",
    Intent.SERVICE_INQUIRY: "services",
}


def manager_answer(service: str, intent: str) -> Optional[str]:
    """Look for a manager-approved answer. Returns None when none exists.

    This is checked BEFORE the FAQ list, implementing knowledge priority 1.
    Keys are looked up as "<service>" then "<service>:<intent>", both
    lowercased, so the manager can supply either a broad or a narrow answer.
    """
    section_name = MANAGER_LOOKUP_KEYS.get(intent)
    if not section_name:
        return None

    section = MANAGER_DATA.get(section_name, {})
    if not section:
        return None

    for key in (f"{service}:{intent}".lower(), service.lower()):
        if key in section and section[key]:
            return section[key]
    return None


# ===========================================================================
# APPROVED FAQ KNOWLEDGE BASE - 99 entries
# ===========================================================================
# Answers are stored EXACTLY as approved. The language model is permitted to
# soften the wording for Instagram, but never to change the facts.
#
# Fields:
#   id                  stable number, matches the brief
#   question            the canonical phrasing
#   answer              approved text - None when the information is missing
#   note                guidance attached to a missing-information entry
#   category            business category from the brief
#   source              where the information came from
#   confirmed           True only for CONFIRMED entries
#   escalation_required True for every missing-information entry
#   service / intent    used for matching and analytics
# ===========================================================================


def _faq(fid, question, answer, category, source, service, intent,
         confirmed=True, note=None):
    """Build one FAQ record and derive the escalation flag from confidence."""
    return {
        "id": fid,
        "question": question,
        "answer": answer,
        "note": note,
        "category": category,
        "source": source,
        "confirmed": confirmed,
        "escalation_required": not confirmed,
        "service": service,
        "intent": intent,
    }


_LOC_ANSWER = ("We are located opposite the ISKCON Temple, Kharbas Cir Rd, "
               "Ganesh Nagar, Radha Kunj, Dholai, Jaipur, Rajasthan 302020. "
               "You can Call/WhatsApp us at 6367857737.")

APPROVED_FAQS: List[Dict[str, Any]] = [
    # ---------------- CATEGORY 1: LOCATION & CONTACT ----------------
    _faq(1, "Where is your shop located?", _LOC_ANSWER,
         "Location/Contact", "Business Identity & Contact",
         Service.LOCATION, Intent.LOCATION),
    _faq(2, "kaha ho aap", _LOC_ANSWER,
         "Location/Contact", "Business Identity & Contact",
         Service.LOCATION, Intent.LOCATION),
    _faq(3, "send map location bro",
         "Car Trends Car Mall is opposite ISKCON Temple, Dholai, Jaipur. "
         "Comment 'Location' on our reel for the exact map link!",
         "Location/Contact", "Intent: Location/Contact",
         Service.LOCATION, Intent.LOCATION),
    _faq(4, "whats ur mobile no.",
         "You can reach us directly on WhatsApp or via call at 6367857737.",
         "Location/Contact", "Business Identity & Contact",
         Service.CONTACT, Intent.CONTACT),
    # Answer rewritten on owner confirmation: Dholai IS the current shop, so
    # this question is now answered "yes" rather than being redirected.
    _faq(5, "dholai me shop hai kya tumhari?",
         "Yes! Our workshop is in Dholai - Opposite ISKCON Temple, Kharbas "
         "Cir Rd, Ganesh Nagar, Radha Kunj, Jaipur, Rajasthan 302020. "
         "Call/WhatsApp: 6367857737.",
         "Location/Contact", "Business Identity & Contact (Owner Confirmed)",
         Service.LOCATION, Intent.LOCATION),
    _faq(6, "addres plzz",
         "Car Trends Car Mall, Opp. ISKCON Temple, Kharbas Cir Rd, Ganesh "
         "Nagar, Radha Kunj, Dholai, Jaipur, Rajasthan 302020. "
         "Call/WhatsApp: 6367857737.",
         "Location/Contact", "Business Identity & Contact",
         Service.LOCATION, Intent.LOCATION),
    _faq(7, "Can I call right now?",
         "Yes, you can Call or WhatsApp us at 6367857737. We are open 7 days "
         "a week from 10:00 AM to 8:00 PM.",
         "Location/Contact", "Business Identity & Contact / Hours",
         Service.CONTACT, Intent.CONTACT),
    _faq(8, "which area in jaipur?",
         "We are located in Dholai, Jaipur - opposite the ISKCON Temple, "
         "near Kharbas Cir Rd, Ganesh Nagar, Radha Kunj.",
         "Location/Contact", "Business Identity & Contact",
         Service.LOCATION, Intent.LOCATION),
    _faq(9, "bhai apna number de do",
         "Call ya WhatsApp karein: 6367857737. We are located at Dholai, "
         "Jaipur - Opp. ISKCON Temple, Kharbas Cir Rd, Ganesh Nagar.",
         "Location/Contact", "Business Identity & Contact",
         Service.CONTACT, Intent.CONTACT),
    _faq(10, "contact kaise karu?",
         "You can easily reach us via Call or WhatsApp at 6367857737.",
         "Location/Contact", "Business Identity & Contact",
         Service.CONTACT, Intent.CONTACT),
    _faq(11, "do you have branches?", None,
         "Location/Contact", "Missing Information",
         Service.LOCATION, Intent.GENERAL_INFORMATION, confirmed=False),
    _faq(12, "parking space hai waha?", None,
         "Location/Contact", "Missing Information",
         Service.LOCATION, Intent.GENERAL_INFORMATION, confirmed=False),

    # ---------------- CATEGORY 2: HOURS & OPERATIONS ----------------
    _faq(13, "open on sunday?",
         "Yes! We are open 7 days a week, Monday through Sunday, from "
         "10:00 AM to 8:00 PM.",
         "General/Timing", "Business Hours", Service.HOURS, Intent.HOURS),
    _faq(14, "opning tym kya h",
         "We are open every day from 10:00 AM to 8:00 PM.",
         "General/Timing", "Business Hours", Service.HOURS, Intent.HOURS),
    _faq(15, "kitne baje tak khule ho bhai?",
         "We are open until 8:00 PM. Our full timings are 10:00 AM to "
         "8:00 PM, 7 days a week.",
         "General/Timing", "Business Hours", Service.HOURS, Intent.HOURS),
    _faq(16, "do you guys have a website",
         "We do not have a website. Please WhatsApp us directly at "
         "6367857737 for any queries or bookings!",
         "General/Timing", "Business Rules (Updated)",
         Service.GENERAL, Intent.GENERAL_INFORMATION),
    _faq(17, "holiday ke din open rehta hai?",
         "We are officially open 7 days a week, Monday to Sunday. For "
         "specific public and national holidays, please WhatsApp us at "
         "6367857737.",
         "General/Timing", "Business Hours", Service.HOURS, Intent.HOURS),

    # ---------------- CATEGORY 3: PPF & CERAMIC ----------------
    _faq(18, "ppf price",
         "We do complete PPF covering the bonnet, bumper, fender, door, and "
         "roof. Please reply with your car model for exact pricing!",
         "PPF/Ceramic", "Intent: PPF/Ceramic",
         Service.PPF, Intent.PRICE_INQUIRY),
    _faq(19, "ceramik cotin karni h",
         "Yes, we do Ceramic Coating with compounding and polishing. Send "
         "your car model and we will share the price!",
         "PPF/Ceramic", "Intent: PPF/Ceramic",
         Service.CERAMIC, Intent.SERVICE_INQUIRY),
    _faq(20, "creta ppf cost?", None,
         "PPF/Ceramic", "Missing Information (Exact Prices)",
         Service.PPF, Intent.PRICE_INQUIRY, confirmed=False,
         note="Please WhatsApp 6367857737 for your Creta's quote."),
    _faq(21, "what does ppf cover?",
         "Our PPF service covers the bonnet, bumper, fender, doors, and "
         "roof, plus sun and glass protection.",
         "PPF/Ceramic", "Intent: PPF/Ceramic",
         Service.PPF, Intent.SERVICE_INQUIRY),
    _faq(22, "paint correction hota hai?",
         "Yes! We provide full compounding, polishing, and paint correction "
         "services for a flawless finish.",
         "PPF/Ceramic", "Core Services: Paint Protection & Detailing",
         Service.DETAILING, Intent.SERVICE_INQUIRY),
    _faq(23, "warranty kitni h ppf pe", None,
         "PPF/Ceramic", "Missing Information",
         Service.PPF, Intent.WARRANTY, confirmed=False),
    _faq(24, "glass coating available?",
         "Yes, we offer glass coating and sun-proofing as part of our "
         "detailing services. Send your car model for details!",
         "PPF/Ceramic", "Core Services: Paint Protection & Detailing",
         Service.CERAMIC, Intent.AVAILABILITY_REQUEST),
    _faq(25, "kitna time lagega ceramic me?", None,
         "PPF/Ceramic", "Missing Information",
         Service.CERAMIC, Intent.DURATION, confirmed=False),
    _faq(26, "sun proofing hoti hai",
         "Yes, we do sun-proofing and glass protection. Let us know your car "
         "model for a quote!",
         "PPF/Ceramic", "Intent: PPF/Ceramic",
         Service.CERAMIC, Intent.SERVICE_INQUIRY),
    _faq(27, "car wash details",
         "We offer premium car wash and detailing services. (Fun fact: Gold "
         "Members get 6 free car washes!).",
         "PPF/Ceramic", "Core Services / Membership",
         Service.CAR_WASH, Intent.SERVICE_INQUIRY),
    _faq(28, "bmw me ppf karana hai",
         "Yes, we specialize in premium cars like BMWs. We offer full PPF "
         "and ceramic coating. Send your exact model for pricing!",
         "PPF/Ceramic", "Core Services (BMWs shown for PPF)",
         Service.PPF, Intent.SERVICE_INQUIRY),
    _faq(29, "old car pe ppf ho sakta h kya",
         "Yes, we do compounding, polishing, and paint correction which "
         "brings back the shine before applying PPF or Ceramic Coating.",
         "PPF/Ceramic", "Core Services: Paint Protection & Detailing",
         Service.PPF, Intent.SERVICE_INQUIRY),
    _faq(30, "polishing cost", None,
         "PPF/Ceramic", "Missing Information",
         Service.DETAILING, Intent.PRICE_INQUIRY, confirmed=False),
    _faq(31, "ceramic cotng aur ppf me kya diff h", None,
         "PPF/Ceramic", "Missing Information",
         Service.CERAMIC, Intent.SERVICE_COMPARISON, confirmed=False),
    # (The brief's "Detailed technical explanation missing from DB" is
    #  internal commentary, not customer guidance - so it is not a note.)
    _faq(32, "detailing price for seltos", None,
         "PPF/Ceramic", "Missing Information",
         Service.DETAILING, Intent.PRICE_INQUIRY, confirmed=False),
    _faq(33, "rubbing polish ho jayegi?",
         "Yes, we provide full compounding, polishing, and paint correction "
         "services.",
         "PPF/Ceramic", "Core Services: Paint Protection & Detailing",
         Service.DETAILING, Intent.SERVICE_INQUIRY),
    _faq(34, "teflon coating vs ceramic", None,
         "PPF/Ceramic", "Missing Information",
         Service.CERAMIC, Intent.SERVICE_COMPARISON, confirmed=False),
    _faq(35, "scratch remove ho jayenge rubbing se?",
         "Yes, minor scratches can be removed with our compounding, "
         "polishing, and paint correction services.",
         "PPF/Ceramic", "Core Services: Paint Protection & Detailing",
         Service.DETAILING, Intent.SERVICE_INQUIRY),

    # ---------------- CATEGORY 4: DENTING, PAINTING, ALLOY ----------------
    _faq(36, "scratch nikal jayega kya bumper se",
         "Yes, we do full denting and painting using our in-house Jetstar "
         "paint booth.",
         "Denting Painting", "Intent: Denting Painting",
         Service.DENTING, Intent.SERVICE_INQUIRY),
    _faq(37, "i need alloy wheels for my alto",
         "Yes, we sell brand-new alloy wheels on advance order! Please "
         "WhatsApp us at 6367857737 with your exact car model and booking "
         "details.",
         "Accessories/Products (Alloy Sales)",
         "Business Rules (Updated: Alloy Wheels)",
         Service.ALLOY_SALES, Intent.ACCESSORY_INQUIRY),
    _faq(38, "do you paint alloy wheels?",
         "Yes! We offer professional alloy wheel painting and gold brake "
         "caliper painting.",
         "Denting Painting", "Core Services: Denting & Painting",
         Service.PAINTING, Intent.SERVICE_INQUIRY),
    _faq(39, "denting painting cost for i10", None,
         "Denting Painting", "Missing Information",
         Service.DENTING, Intent.PRICE_INQUIRY, confirmed=False,
         note="WhatsApp us your car's photos at 6367857737 for an exact "
              "estimate."),
    _faq(40, "fortuner 2014 convert to new model",
         "Yes, we specialize in full body conversions, including upgrading a "
         "2014 Fortuner to the new model shape!",
         "Denting Painting", "Core Services: Denting & Painting",
         Service.DENTING, Intent.SERVICE_INQUIRY),
    _faq(41, "paint booth hai tumhare paas?",
         "Yes, we have a premium in-house Jetstar paint booth for a "
         "factory-level finish.",
         "Denting Painting", "Core Services: Denting & Painting",
         Service.PAINTING, Intent.SERVICE_INQUIRY),
    _faq(42, "brake caliper red paint hoga?",
         "We specialize in gold brake caliper painting, but contact us on "
         "WhatsApp at 6367857737 to confirm other custom colors!",
         "Denting Painting", "Core Services: Denting & Painting",
         Service.PAINTING, Intent.SERVICE_INQUIRY),
    _faq(43, "full body paint price", None,
         "Denting Painting", "Missing Information",
         Service.PAINTING, Intent.PRICE_INQUIRY, confirmed=False,
         note="Prices depend on car model and color. Send details to "
              "6367857737."),
    _faq(44, "kitna time me dent theek hoga", None,
         "Denting Painting", "Missing Information",
         Service.DENTING, Intent.DURATION, confirmed=False),
    _faq(45, "do you sell new alloys for thar",
         "Yes, we sell brand-new alloy wheels on advance order! Please "
         "WhatsApp us at 6367857737 with your Thar's details.",
         "Accessories/Products (Alloy Sales)",
         "Business Rules (Updated: Alloy Wheels)",
         Service.ALLOY_SALES, Intent.ACCESSORY_INQUIRY),
    _faq(46, "colour match guarantee h kya", None,
         "Denting Painting", "Missing Information",
         Service.PAINTING, Intent.WARRANTY, confirmed=False),
    _faq(47, "gaadi pe paint gir gaya hai clean hoga?",
         "We do full paint correction, compounding, and detailing. Bring it "
         "to our Dholai workshop and we will fix it!",
         "Denting Painting / Detailing", "Core Services",
         Service.DETAILING, Intent.SERVICE_INQUIRY),
    _faq(48, "old fortuner modification cost", None,
         "Denting Painting", "Missing Information",
         Service.DENTING, Intent.PRICE_INQUIRY, confirmed=False,
         note="Prices vary based on parts. WhatsApp 6367857737."),
    _faq(49, "denting ke baad paint karna zaruri h?",
         "It depends on the damage. We have an in-house Jetstar paint booth "
         "if painting is required!",
         "Denting Painting", "Core Services: Denting & Painting",
         Service.DENTING, Intent.SERVICE_INQUIRY),
    _faq(50, "can I buy alloy rims only?",
         "Yes, we sell brand-new alloy wheels on advance order! WhatsApp "
         "6367857737 with your car model for details.",
         "Accessories/Products (Alloy Sales)",
         "Business Rules (Updated: Alloy Wheels)",
         Service.ALLOY_SALES, Intent.ACCESSORY_INQUIRY),

    # ---------------- CATEGORY 5: MECHANICAL, AC & ENGINE ----------------
    _faq(51, "engine se aawaz aa rahi h",
         "We can fix that! We do top-to-bottom service including engine "
         "repairs and fixing noisy engines.",
         "Service/Mechanical", "Intent: Service/Mechanical",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(52, "brake pad change cost", None,
         "Service/Mechanical", "Missing Information",
         Service.MECHANICAL, Intent.PRICE_INQUIRY, confirmed=False,
         note="Please send your car model to 6367857737 for exact part "
              "pricing."),
    _faq(53, "mileage drop ho gaya gaadi ka",
         "Mileage drops can be fixed! We do catalytic converter un-choking "
         "and O2 sensor cleaning to restore mileage.",
         "Service/Mechanical", "Core Services: Mechanical & General Service",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(54, "AC cooling nahi kar raha h",
         "We handle full AC service and repairs in-house. Bring your car to "
         "our Dholai workshop!",
         "Service/Mechanical", "Core Services: Mechanical & General Service",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(55, "car battery change karni h",
         "Yes, we do battery replacements. (Note: Battery replacement "
         "service is also included for our Gold Members!).",
         "Service/Mechanical", "Core Services: Mechanical & General Service",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(56, "service me kya kya include hai",
         "We do top-to-bottom service including engine work, brake "
         "pads/discs, AC service, catalytic converter, and O2 sensor "
         "cleaning.",
         "Service/Mechanical", "Intent: Service/Mechanical",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(57, "gaadi pickup aur drop available h?",
         "Free pick-up and drop is a special benefit included with our Gold "
         "Membership (Rs 2,999)!",
         "Membership", "Core Services: Membership Offer",
         Service.MEMBERSHIP, Intent.PICKUP_DROP),
    _faq(58, "catalytic converter clean price", None,
         "Service/Mechanical", "Missing Information",
         Service.MECHANICAL, Intent.PRICE_INQUIRY, confirmed=False),
    _faq(59, "O2 sensor clean karte ho?",
         "Yes, we do O2 sensor cleaning specifically to fix mileage drop "
         "issues.",
         "Service/Mechanical", "Core Services: Mechanical & General Service",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(60, "brake disc replace ho jayega creta ka?",
         "Yes, we do brake disc and brake pad replacements for all models.",
         "Service/Mechanical", "Core Services: Mechanical & General Service",
         Service.MECHANICAL, Intent.SERVICE_INQUIRY),
    _faq(61, "engine overhaul cost", None,
         "Service/Mechanical", "Missing Information",
         Service.MECHANICAL, Intent.PRICE_INQUIRY, confirmed=False),
    _faq(62, "timing belt milti hai kya?",
         "Yes, we have a huge warehouse for spare parts including timing "
         "belts, water pumps, clutch plates, and more!",
         "Service/Mechanical", "Products & Accessories: Spare Parts",
         Service.SPARE_PARTS, Intent.AVAILABILITY_REQUEST),
    _faq(63, "clutch plate change for swift",
         "We stock and replace clutch plates! Send us your exact model year "
         "on WhatsApp at 6367857737 for a quote.",
         "Service/Mechanical", "Products & Accessories: Spare Parts",
         Service.SPARE_PARTS, Intent.SERVICE_INQUIRY),
    _faq(64, "ac gas refill price", None,
         "Service/Mechanical", "Missing Information",
         Service.MECHANICAL, Intent.PRICE_INQUIRY, confirmed=False),
    _faq(65, "general service kitne ki h", None,
         "Service/Mechanical", "Missing Information",
         Service.MECHANICAL, Intent.PRICE_INQUIRY, confirmed=False),
    _faq(66, "suspension awaz kar raha h",
         "We stock suspension arms, shock absorbers, and spring cushion "
         "pads, and can fully repair your suspension in-house!",
         "Service/Mechanical", "Products & Accessories / Tyre & Suspension",
         Service.SUSPENSION, Intent.SERVICE_INQUIRY),
    _faq(67, "original spare parts use karte ho?",
         "Yes, we stock premium spare parts including water pumps, timing "
         "belts, valve lifters, VVT solenoids, and clutch plates.",
         "Service/Mechanical", "Products & Accessories: Spare Parts",
         Service.SPARE_PARTS, Intent.GENERAL_INFORMATION),

    # ---------------- CATEGORY 6: TYRES, ALIGNMENT, SUSPENSION ----------
    _faq(68, "wheel alignment karani h",
         "We do professional wheel alignment and tyre balancing using the "
         "SKY AUTO Jetstar machine.",
         "Tyres & Suspension", "Core Services: Tyre, Suspension & Alignment",
         Service.TYRES, Intent.SERVICE_INQUIRY),
    _faq(69, "ground clearance low h, barish me dikkat h",
         "We install spring cushion pads which are the perfect monsoon "
         "solution for low ground clearance vehicles!",
         "Tyres & Suspension", "Core Services: Tyre, Suspension & Alignment",
         Service.SUSPENSION, Intent.SERVICE_INQUIRY),
    _faq(70, "puncture banate ho?",
         "Yes, we handle puncture repair and tyre balancing in-house.",
         "Tyres & Suspension", "Core Services: Tyre, Suspension & Alignment",
         Service.TYRES, Intent.SERVICE_INQUIRY),
    _faq(71, "shock absorber replace price", None,
         "Tyres & Suspension", "Missing Information",
         Service.SUSPENSION, Intent.PRICE_INQUIRY, confirmed=False,
         note="Please send your car model for part pricing."),

    # ---------------- CATEGORY 7: ACCESSORIES & PARTS ----------------
    _faq(72, "7d mat price for nexon",
         "We are India's biggest accessories store! We have waterproof "
         "Lifelong / GF Lifelong 7D mats. Please tell me your car model year "
         "for the exact price!",
         "Accessories/Products", "Intent: Accessories/Products",
         Service.ACCESSORIES, Intent.PRICE_INQUIRY),
    _faq(73, "which engine oil u use",
         "We have our very own premium brand: CAR TRENDS engine oil "
         "(available in 0W20, 0W16, and 5W30) as well as RED COOLANT!",
         "Accessories/Products", "Products & Accessories: Own Brand",
         Service.ACCESSORIES, Intent.GENERAL_INFORMATION),
    _faq(74, "blaupunkt vacuum cleaner hai?",
         "Yes! We stock the Blaupunkt Vacuum VC 3008 GY 110W.",
         "Accessories/Products", "Products & Accessories: Car Care",
         Service.ACCESSORIES, Intent.AVAILABILITY_REQUEST),
    _faq(75, "seat covers for thar",
         "We provide custom stitched seat covers for all cars. Send us your "
         "requirements!",
         "Accessories/Products", "Products & Accessories: Accessories",
         Service.ACCESSORIES, Intent.ACCESSORY_INQUIRY),
    _faq(76, "jbl speaker price",
         "We stock top audio brands like JBL, Hertz, JL Audio, and Morel. "
         "Tell me your car model and requirements for pricing!",
         "Accessories/Products", "Intent: Accessories/Products",
         Service.AUDIO, Intent.PRICE_INQUIRY),
    _faq(77, "apple car play screen available?",
         "Yes, we sell and install Apple CarPlay systems!",
         "Accessories/Products", "Products & Accessories: Audio",
         Service.AUDIO, Intent.AVAILABILITY_REQUEST),
    _faq(78, "wiper blade change karna h",
         "We have premium wiper blades in stock at our warehouse. Bring your "
         "car in!",
         "Accessories/Products", "Products & Accessories: Car Care",
         Service.ACCESSORIES, Intent.ACCESSORY_INQUIRY),
    _faq(79, "laptop rakhne ke liye piche kuch h seat par?",
         "Yes, we sell back seat organizers designed specifically for "
         "laptops and tablets!",
         "Accessories/Products", "Products & Accessories: Accessories",
         Service.ACCESSORIES, Intent.ACCESSORY_INQUIRY),
    _faq(80, "neck pillow mil jayega?",
         "Yes, we have neck rest pillows in our accessories warehouse.",
         "Accessories/Products", "Products & Accessories: Accessories",
         Service.ACCESSORIES, Intent.AVAILABILITY_REQUEST),
    _faq(81, "wind visor for creta",
         "We stock wind visors, sun shades, and badges for Creta and other "
         "models.",
         "Accessories/Products", "Products & Accessories: Accessories",
         Service.ACCESSORIES, Intent.ACCESSORY_INQUIRY),
    _faq(82, "car perfume price", None,
         "Accessories/Products", "Missing Information",
         Service.ACCESSORIES, Intent.PRICE_INQUIRY, confirmed=False,
         note="We stock them, but exact prices vary. Visit us!"),
    _faq(83, "vvt solenoid problem",
         "We stock replacement spare parts like VVT solenoids, valve "
         "lifters, and water pumps. Bring it to the workshop for a check!",
         "Accessories/Products", "Products & Accessories: Spare Parts",
         Service.SPARE_PARTS, Intent.SERVICE_INQUIRY),
    _faq(84, "do you sell subwoofers?",
         "Yes, we sell subwoofers and premium speakers from brands like "
         "Hertz, JBL, JL Audio, and Morel.",
         "Accessories/Products", "Products & Accessories: Audio",
         Service.AUDIO, Intent.AVAILABILITY_REQUEST),
    _faq(85, "sun shade price for venue", None,
         "Accessories/Products", "Missing Information",
         Service.ACCESSORIES, Intent.PRICE_INQUIRY, confirmed=False,
         note="We have them in stock, reply with your model year for price."),
    _faq(86, "lifelong mat for sonet",
         "Yes, we have waterproof 7D Lifelong / GF Lifelong mats for Sonet "
         "in stock!",
         "Accessories/Products", "Products & Accessories: Mats",
         Service.ACCESSORIES, Intent.AVAILABILITY_REQUEST),
    _faq(87, "discount on accessories?",
         "With our Gold Membership (Rs 2,999) you get special discounts on "
         "accessories, plus lots of free services!",
         "Membership / Accessories", "Core Services: Membership",
         Service.MEMBERSHIP, Intent.OFFER_DISCOUNT),

    # ---------------- CATEGORY 8: USED CARS ----------------
    _faq(88, "second hand car chahiye",
         "We sell used cars starting from Rs 99,000! We have models like "
         "Creta, Jeep Compass, i10, WagonR, and Alto. Visit our lot at "
         "Dholai, Jaipur.",
         "Used Cars", "Intent: Used Cars",
         Service.USED_CARS, Intent.USED_CAR_INQUIRY),
    _faq(89, "finance available on old cars?",
         "Yes! We offer finance options starting from just Rs 30k-40k "
         "downpayment.",
         "Used Cars", "Core Services: Used Car Sales",
         Service.USED_CARS, Intent.USED_CAR_INQUIRY),
    _faq(90, "used car pe guarantee hoti h?",
         "Yes, our used cars come with a warranty, 3 free services, and we "
         "handle all the RTO paperwork on-site!",
         "Used Cars", "Core Services: Used Car Sales",
         Service.USED_CARS, Intent.WARRANTY),
    _faq(91, "rto work kaun karega",
         "We handle all the RTO paperwork on-site for used car sales so you "
         "don't have to worry about it!",
         "Used Cars", "Core Services: Used Car Sales",
         Service.USED_CARS, Intent.USED_CAR_INQUIRY),
    _faq(92, "used jeep compass price", None,
         "Used Cars", "Missing Information",
         Service.USED_CARS, Intent.PRICE_INQUIRY, confirmed=False,
         note="Prices depend on the current stock. Visit our Dholai lot "
              "to check!"),
    _faq(93, "purani creta milegi",
         "Yes, Creta is one of the models we frequently stock in our used "
         "car lot. Visit us at Dholai to check our current inventory!",
         "Used Cars", "Core Services: Used Car Sales",
         Service.USED_CARS, Intent.AVAILABILITY_REQUEST),
    _faq(94, "free service with second hand car?",
         "Yes! Every used car comes with a warranty and 3 free services "
         "included.",
         "Used Cars", "Core Services: Used Car Sales",
         Service.USED_CARS, Intent.USED_CAR_INQUIRY),

    # ---------------- CATEGORY 9: GOLD MEMBERSHIP ----------------
    _faq(95, "gold membership kya hai",
         "Our Gold Membership is Rs 2,999. It includes a free fire "
         "extinguisher, 2 free labour-free services, 6 free car washes, "
         "discounts on accessories/painting, free pickup/drop, AC check, and "
         "more! Benefits total over Rs 9,000!",
         "Membership", "Intent: Membership",
         Service.MEMBERSHIP, Intent.MEMBERSHIP_INQUIRY),
    _faq(96, "2999 offer details",
         "For 2999/-, you get Gold Membership: Free fire extinguisher, 2 "
         "free services, 6 washes, free pickup/drop, AC check, wheel "
         "alignment, battery replacement check, and discounts!",
         "Membership", "Intent: Membership",
         Service.MEMBERSHIP, Intent.MEMBERSHIP_INQUIRY),
    _faq(97, "is washing free in membership?",
         "Yes! Gold Members receive 6 free car washes as part of their "
         "package.",
         "Membership", "Core Services: Membership Offer",
         Service.MEMBERSHIP, Intent.MEMBERSHIP_INQUIRY),
    _faq(98, "free fire extinguisher milega?",
         "Yes, a free auto fire extinguisher worth Rs 2,300 is included when "
         "you buy the Gold Membership for Rs 2,999!",
         "Membership", "Core Services: Membership Offer",
         Service.MEMBERSHIP, Intent.MEMBERSHIP_INQUIRY),
    _faq(99, "membership valid for how long?", None,
         "Membership", "Missing Information",
         Service.MEMBERSHIP, Intent.MEMBERSHIP_INQUIRY, confirmed=False),
]

# Sanity guards - these run at import so a bad edit is caught immediately
# rather than silently changing what customers are told.
assert len(APPROVED_FAQS) == 99, f"expected 99 FAQs, found {len(APPROVED_FAQS)}"
assert all(f["answer"] for f in APPROVED_FAQS if f["confirmed"]), \
    "a confirmed FAQ is missing its answer"
assert all(f["answer"] is None for f in APPROVED_FAQS if not f["confirmed"]), \
    "an unconfirmed FAQ must not carry an answer"

# The address regression guard. Mansarovar is the OLD location and must never
# appear in anything a customer can be shown - not in an answer, not in a
# missing-information note. If this ever fails, someone has reintroduced the
# outdated address and the bot would be sending customers to the wrong place.
_FORMER_AREA = BUSINESS["former_area"].lower()
for _f in APPROVED_FAQS:
    for _field in ("answer", "note"):
        _text = _f.get(_field) or ""
        assert _FORMER_AREA not in _text.lower(), (
            f"FAQ {_f['id']} still names the former area "
            f"'{BUSINESS['former_area']}' in its {_field}"
        )
assert _FORMER_AREA not in BUSINESS["address"].lower(), \
    "BUSINESS['address'] must be the current Dholai address"
assert "dholai" in BUSINESS["address"].lower(), \
    "BUSINESS['address'] must name Dholai"


CONFIRMED_FAQS = [f for f in APPROVED_FAQS if f["confirmed"]]
MISSING_INFO_FAQS = [f for f in APPROVED_FAQS if not f["confirmed"]]


def faq_by_id(fid: int) -> Optional[Dict[str, Any]]:
    for f in APPROVED_FAQS:
        if f["id"] == fid:
            return f
    return None


# ===========================================================================
# VERIFIED PRODUCT CATALOG
# ===========================================================================
# Every entry is traceable to an approved FAQ - the `faqs` field names them.
# Nothing was added from general automotive knowledge.
#
# THE THREE STATES a product can be in (brief section 7):
#   verified=True   we have approved wording saying we carry this CATEGORY.
#                   It still says nothing about what is on the shelf today.
#   verified=False  the customer's word is recognised so we understand them,
#                   but NO approved source confirms we carry it. The reply
#                   must hand over to the team, never claim we have it.
#   stock           NEVER asserted. No approved FAQ states live stock, so the
#                   bot never says "currently in stock" about anything.
#
# `benefit` is only filled in where an approved FAQ states it. Where it is
# None the bot describes no benefit at all, because inventing one is exactly
# the failure mode this catalog exists to prevent.
# ===========================================================================

PRODUCTS: Dict[str, Dict[str, Any]] = {
    # ---------------- verified accessories ----------------
    "floor_mats": {
        "label": "7D floor mats",
        "detail": "waterproof 7D Lifelong / GF Lifelong mats",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [72, 86],
    },
    "seat_covers": {
        "label": "seat covers",
        "detail": "custom stitched seat covers for all cars",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [75],
    },
    "vacuum": {
        "label": "car vacuum cleaner",
        "detail": "the Blaupunkt Vacuum VC 3008 GY 110W",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [74],
    },
    "wipers": {
        "label": "wiper blades",
        "detail": "premium wiper blades at our warehouse",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [78],
    },
    "seat_organizer": {
        "label": "back seat organizer",
        "detail": "back seat organizers designed for laptops and tablets",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [79],
    },
    "neck_pillow": {
        "label": "neck rest pillow",
        "detail": "neck rest pillows in our accessories warehouse",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [80],
    },
    "wind_visor": {
        "label": "wind visors and sun shades",
        "detail": "wind visors, sun shades and badges",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [81],
    },
    "engine_oil": {
        "label": "engine oil",
        "detail": "our own CAR TRENDS engine oil in 0W20, 0W16 and 5W30",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [73],
    },
    "coolant": {
        "label": "coolant",
        "detail": "our own CAR TRENDS RED COOLANT",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [73],
    },
    "perfume": {
        "label": "car perfume",
        "detail": "car perfumes",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [82],
    },
    # ---------------- verified audio ----------------
    "speakers": {
        "label": "speakers",
        "detail": "speakers and subwoofers from JBL, Hertz, JL Audio and Morel",
        "benefit": None,
        "category": Service.AUDIO,
        "verified": True, "faqs": [76, 84],
    },
    "carplay": {
        "label": "Apple CarPlay",
        "detail": "Apple CarPlay systems, which we sell and install",
        "benefit": None,
        "category": Service.AUDIO,
        "verified": True, "faqs": [77],
    },
    # ---------------- verified wheels / parts ----------------
    "alloy_wheels": {
        "label": "alloy wheels",
        "detail": "brand-new alloy wheels on advance order",
        "benefit": None,
        "category": Service.ALLOY_SALES,
        "verified": True, "faqs": [37, 45, 50],
    },
    "spare_parts": {
        "label": "spare parts",
        "detail": "timing belts, water pumps, clutch plates, valve lifters "
                  "and VVT solenoids",
        "benefit": None,
        "category": Service.SPARE_PARTS,
        "verified": True, "faqs": [62, 67, 83],
    },
    "suspension_parts": {
        "label": "suspension parts",
        "detail": "suspension arms, shock absorbers and spring cushion pads",
        "benefit": None,
        "category": Service.SUSPENSION,
        "verified": True, "faqs": [66],
    },

    # ---------------- RECOGNISED BUT NOT VERIFIED ----------------
    # Customers ask for these by name, so the bot must UNDERSTAND them -
    # otherwise it falls back to keyword soup and answers the wrong thing,
    # which is the bug this whole catalog was built to fix.
    #
    # But no approved FAQ says we carry them, so the reply hands over to the
    # team rather than confirming. Each one is logged as a knowledge gap.
    # Move an entry to verified=True the moment the owner confirms it.
    "armrest": {
        "label": "armrest",
        "detail": None, "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": False, "faqs": [],
    },
    # ---------------- GFX MATS (owner-verified, final pass) ----------------
    # Two separate variants. "GFX" on its own means BOTH, and must never be
    # collapsed into the Lifelong FAQ or into generic mats - see
    # always_compose, which routes every GFX question to compose_gfx_reply().
    # Every benefit below is the owner's wording, softened as instructed:
    # "designed to / helps / offers" - never "never cracks", "100%", years.
    "gfx": {
        "label": "GFX mats",
        "detail": "GFX Normal and GFX Pro/Lifelong mats",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [],
        "always_compose": True,
        "variants": ["gfx_normal", "gfx_pro"],
    },
    "gfx_pro": {
        "label": "GFX Pro/Lifelong mats",
        "detail": "premium vehicle-specific molded floor mats",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [],
        "always_compose": True,
        "benefits": [
            "Vehicle-specific molded fit designed to follow your car's footwell",
            "Extensive edge-to-edge floor coverage",
            "Durable TPV/TPE thermoplastic material for everyday use",
            "Raised edges and channels to help contain mud, dirt and spills",
            "Anti-skid backing / retention to help keep the driver's mat in place",
            "Easy to clean",
            "Designed for long-term daily use",
        ],
        "benefits_hi": [
            "Vehicle-specific molded fit jo car ke footwell ke according design hota hai",
            "Edge-to-edge floor coverage",
            "Durable TPV/TPE material daily use ke liye",
            "Raised edges aur channels jo mud, dirt aur spills ko contain karne mein help karte hain",
            "Anti-skid backing / retention jo driver mat ko jagah par rakhne mein help karta hai",
            "Easy cleaning",
            "Long-term daily use ke liye design",
        ],
    },
    "gfx_normal": {
        "label": "GFX Normal mats",
        "detail": "the simpler, budget-friendly GFX option",
        "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": True, "faqs": [],
        "always_compose": True,
        "benefits": [
            "Budget-friendly - generally cheaper upfront than premium molded mats",
            "Lightweight and flexible",
            "Easy to remove and shake out or clean",
            "A commonly available, simple option",
        ],
        "benefits_hi": [
            "Budget-friendly - premium molded mats se generally sasta option",
            "Lightweight aur flexible",
            "Nikaal kar jhaadna ya clean karna easy",
            "Commonly available simple option",
        ],
    },
    "android_system": {
        # Customers ask for "android system"; what we VERIFY is Apple CarPlay
        # (FAQ 77). So this is understood, not claimed - the reply points at
        # the CarPlay we do sell and lets the team confirm Android options.
        "label": "Android infotainment system",
        "detail": None, "benefit": None,
        "category": Service.AUDIO,
        "verified": False, "faqs": [],
        "related_verified": "carplay",
    },
    "car_cover": {
        "label": "car cover",
        "detail": None, "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": False, "faqs": [],
    },
    "mud_flaps": {
        "label": "mud flaps",
        "detail": None, "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": False, "faqs": [],
    },
    "steering_cover": {
        "label": "steering cover",
        "detail": None, "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": False, "faqs": [],
    },
    "dashcam": {
        "label": "dash camera",
        "detail": None, "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": False, "faqs": [],
    },
    "tyres": {
        "label": "tyres",
        "detail": None, "benefit": None,
        "category": Service.TYRES,
        "verified": False, "faqs": [],
        # We verify alignment, balancing and puncture repair (FAQ 68, 70) -
        # NOT that we sell tyres. So when the customer is asking about that
        # WORK rather than about buying a tyre, this entry stands aside and
        # the verified service answer is used.
        "suppress_if": ["balancing", "balance", "alignment", "align",
                        "puncture", "rotation", "repair"],
    },
    "roof_rails": {
        "label": "roof rails",
        "detail": None, "benefit": None,
        "category": Service.ACCESSORIES,
        "verified": False, "faqs": [],
    },
}

# Alias -> product key. Written lowercase; matched on the normalised text.
PRODUCT_ALIASES: Dict[str, str] = {
    "mat": "floor_mats", "mats": "floor_mats", "floor mat": "floor_mats",
    "floor mats": "floor_mats", "car mat": "floor_mats",
    "car mats": "floor_mats", "7d mat": "floor_mats", "7d mats": "floor_mats",
    "lifelong": "floor_mats", "gf lifelong": "floor_mats",
    "matting": "floor_mats",

    "gfx": "gfx", "gfx mat": "gfx", "gfx mats": "gfx",
    "gfx pro": "gfx_pro", "gfx pro mat": "gfx_pro", "gfx pro mats": "gfx_pro",
    "gfx lifelong": "gfx_pro", "gfx pro lifelong": "gfx_pro",
    "pro mat": "gfx_pro", "pro mats": "gfx_pro", "gfx premium": "gfx_pro",
    "gfx normal": "gfx_normal", "normal gfx": "gfx_normal",
    "gfx normal mat": "gfx_normal", "gfx normal mats": "gfx_normal",
    "normal mat": "gfx_normal", "normal mats": "gfx_normal",
    "traditional mat": "gfx_normal", "traditional mats": "gfx_normal",
    "simple mat": "gfx_normal",

    "seat cover": "seat_covers", "seat covers": "seat_covers",
    "seatcover": "seat_covers", "seat kavar": "seat_covers",

    "vacuum": "vacuum", "vaccum": "vacuum", "vacume": "vacuum",
    "vacuum cleaner": "vacuum", "blaupunkt": "vacuum",

    "wiper": "wipers", "wipers": "wipers", "wiper blade": "wipers",
    "wiper blades": "wipers",

    "organizer": "seat_organizer", "organiser": "seat_organizer",
    "seat organizer": "seat_organizer", "back seat organizer": "seat_organizer",

    "neck pillow": "neck_pillow", "neck rest": "neck_pillow",
    "pillow": "neck_pillow",

    "wind visor": "wind_visor", "wind visors": "wind_visor",
    "sun shade": "wind_visor", "sun shades": "wind_visor",
    "door visor": "wind_visor", "badge": "wind_visor",

    "engine oil": "engine_oil", "oil": "engine_oil",
    "coolant": "coolant", "red coolant": "coolant",
    "perfume": "perfume", "freshener": "perfume",

    "speaker": "speakers", "speakers": "speakers", "subwoofer": "speakers",
    "subwoofers": "speakers", "woofer": "speakers", "jbl": "speakers",
    "hertz": "speakers", "jl audio": "speakers", "morel": "speakers",
    "music system": "speakers", "sound system": "speakers",
    "amplifier": "speakers",

    "carplay": "carplay", "apple carplay": "carplay",
    "apple car play": "carplay", "car play": "carplay",
    "android auto": "carplay", "android screen": "carplay",
    "touch screen": "carplay",

    "alloy": "alloy_wheels", "alloys": "alloy_wheels",
    "alloy wheel": "alloy_wheels", "alloy wheels": "alloy_wheels",
    "alloy rim": "alloy_wheels", "rim": "alloy_wheels",
    "rims": "alloy_wheels",

    "spare part": "spare_parts", "spare parts": "spare_parts",
    "timing belt": "spare_parts", "water pump": "spare_parts",
    "clutch plate": "spare_parts", "valve lifter": "spare_parts",
    "vvt solenoid": "spare_parts", "vvt": "spare_parts",

    "shock absorber": "suspension_parts", "shocker": "suspension_parts",
    "spring cushion": "suspension_parts",
    "suspension arm": "suspension_parts",

    "android system": "android_system", "android player": "android_system",
    "android stereo": "android_system", "android head unit": "android_system",
    "android music system": "android_system", "touchscreen system": "android_system",
    "armrest": "armrest", "arm rest": "armrest", "hand rest": "armrest",
    "car cover": "car_cover", "body cover": "car_cover",
    "mud flap": "mud_flaps", "mud flaps": "mud_flaps",
    "mudflap": "mud_flaps",
    "steering cover": "steering_cover", "steering kavar": "steering_cover",
    "dashcam": "dashcam", "dash cam": "dashcam", "dash camera": "dashcam",
    "tyre": "tyres", "tyres": "tyres", "tire": "tyres",
    "roof rail": "roof_rails", "roof rails": "roof_rails",
}


# Service labels used by the composers and escalation wording.
SERVICE_LABELS: Dict[str, str] = {
    Service.PPF: "PPF",
    Service.CERAMIC: "ceramic coating",
    Service.DETAILING: "compounding and polishing",
    Service.DENTING: "denting and painting",
    Service.PAINTING: "painting",
    Service.CAR_WASH: "car wash and detailing",
    Service.MECHANICAL: "a full service",
    Service.TYRES: "wheel alignment and balancing",
    Service.SUSPENSION: "suspension work",
    Service.ACCESSORIES: "accessories",
    Service.AUDIO: "car audio",
    Service.ALLOY_SALES: "alloy wheels",
    Service.USED_CARS: "used cars",
    Service.MEMBERSHIP: "the Gold Membership",
    Service.SPARE_PARTS: "spare parts",
}


def product_by_key(key: Optional[str]) -> Optional[Dict[str, Any]]:
    return PRODUCTS.get(key) if key else None


# Every alias must point at a real product, or recognition silently breaks.
for _alias, _key in PRODUCT_ALIASES.items():
    assert _key in PRODUCTS, f"alias {_alias!r} points at unknown product {_key!r}"


# ===========================================================================
# ESCALATION MESSAGE
# ===========================================================================
def escalation_message(note: Optional[str] = None) -> str:
    """The approved wording for "we do not know - ask a human".

    `note` carries the extra guidance attached to some missing-information
    FAQ entries, e.g. "WhatsApp us your car's photos for an exact estimate".
    """
    base = ("I don't want to give you incorrect information. Our team can "
            f"confirm this for you. You can call or WhatsApp us on {PHONE}.")
    if note:
        return f"{note} {base}"
    return base
