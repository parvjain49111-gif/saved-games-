"""
=============================================================================
 Car Trends Car Mall - LOCAL TEST CONSOLE
=============================================================================

 WHAT THIS IS
 ------------
 A practice room for the chatbot. It calls brain.answer() - the exact same
 function the Instagram webhook calls - so whatever you see here is
 precisely what a real customer would receive. There is deliberately no
 separate "testing brain".

 Nothing here touches Instagram. No Meta app, no ngrok, no access token.

 HOW TO RUN IT
 -------------
     python chat.py

 COMMANDS
 --------
     /reload    re-read the code after you edit knowledge.py or brain.py
     /facts     verified business facts and the FAQ counts
     /prompt    the full system prompt sent to Ollama
     /intents   how the last message was classified, in detail
     /test      run the automated test suite
     /stats     live analytics straight from the database
     /gaps      questions the knowledge base could not answer
     /new       start a fresh conversation (new customer, clean memory)
     /save      toggle writing into the PRODUCTION database instead of
                the console's own console.db (default: off)
     /noai      toggle the language model on and off
     /quit      leave

 MEMORY
 ------
 The console remembers the conversation exactly as Instagram does - it
 calls brain.process() with a conversation id, so "my Alto has scratches"
 followed by "how much?" is understood the same way a customer's DM would
 be. It keeps that memory in its OWN database file (console.db) so the
 owner's real analytics are never polluted by test chats. /new starts a
 clean conversation; /save switches to the production database.

 READING THE OUTPUT
 ------------------
 Every reply is tagged with the layer that produced it:

     [MANAGER]    an answer the manager supplied - highest authority
     [FAQ]        one of the 99 approved FAQ answers, word for word
     [RULE]       a verified core fact (location, hours, contact)
     [ESCALATION] we do NOT know, so the customer goes to the human team
     [AI]         llama3.2:3b wrote the wording. The only layer that can be
                  wrong about a fact - read these carefully.
=============================================================================
"""

import importlib
import os
import sys
import time

# Windows consoles default to cp1252, which cannot print an emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import brain
import config
import database as db
import knowledge as kb

# The console ALWAYS runs with conversation memory - exactly like Instagram.
# It uses its own database file so test chats never touch the owner's real
# analytics; /save switches to the production database when you want to.
CONSOLE_DB = os.environ.get("DB_PATH") or "console.db"
PRODUCTION_DB = "cartrends.db"
SAVE_TO_DB = False               # False = console.db, True = production db
USE_AI = True
LAST_ANSWER = None

_conversation_no = 1
_LAUNCH = __import__("time").strftime("%H%M%S")   # a restart never re-attaches to an old chat
CONSOLE_CUSTOMER = f"console-{_LAUNCH}-{_conversation_no}"


def _use_database(path: str) -> None:
    """Point the storage layer at `path` and (re)create the schema."""
    db.close_connection()
    config.DB_PATH = path
    db.init_db()


def show_banner() -> None:
    print("\n" + "=" * 72)
    print(f" {kb.BUSINESS['name']} - local test console")
    print("=" * 72)
    print(f" Model    : {config.OLLAMA_MODEL}")
    print(f" Address  : {kb.BUSINESS['address']}")
    print(f" Phone    : {kb.BUSINESS['phone']}")
    print(f" Hours    : {kb.BUSINESS['hours_sentence']}")
    print(f" Knowledge: {len(kb.CONFIRMED_FAQS)} confirmed FAQs, "
          f"{len(kb.MISSING_INFO_FAQS)} awaiting business information")
    print(f" Memory   : ON - conversation '{CONSOLE_CUSTOMER}' in "
          f"{config.DB_PATH}   AI: {'ON' if USE_AI else 'OFF'}")
    print("-" * 72)
    print(" Type a customer message and press Enter.")
    print(" /reload  /facts  /prompt  /intents  /test  /stats  /gaps"
          "  /new  /save  /noai  /quit")
    print("=" * 72 + "\n")


def show_facts() -> None:
    print("\n--- VERIFIED BUSINESS FACTS ---")
    for key in ("name", "tagline", "address", "area", "maps_link", "phone",
                "days", "hours", "brands"):
        print(f"  {key:12}: {kb.BUSINESS[key]}")
    print(f"  {'website':12}: NONE - all enquiries go to WhatsApp")
    print(f"  {'former area':12}: {kb.BUSINESS['former_area']} "
          "(recognition only - never shown as current)")

    print("\n--- DETERMINISTIC REPLIES ---")
    for label, text in [("LOCATION", brain.LOCATION_REPLY),
                        ("HOURS", brain.HOURS_REPLY),
                        ("CONTACT", brain.CONTACT_REPLY),
                        ("OFFLINE", brain.OFFLINE_REPLY),
                        ("ESCALATION", kb.escalation_message())]:
        print(f"\n[{label}] {text}")

    print(f"\n--- FAQ KNOWLEDGE BASE ({len(kb.APPROVED_FAQS)} entries) ---")
    print(f"  confirmed          : {len(kb.CONFIRMED_FAQS)}")
    print(f"  information needed : {len(kb.MISSING_INFO_FAQS)}")
    missing = {}
    for f in kb.MISSING_INFO_FAQS:
        missing.setdefault(f["service"], []).append(f["id"])
    print("\n  Business information still required, by service:")
    for svc, ids in sorted(missing.items()):
        print(f"    {svc:18} FAQ {', '.join(str(i) for i in ids)}")

    filled = sum(1 for section in kb.MANAGER_DATA.values() for _ in section)
    print(f"\n--- MANAGER DATA: {filled} answers supplied ---")
    if not filled:
        print("  (empty - the questionnaire has not been returned yet)")
    print()


def show_intents() -> None:
    """Explain in full how the last message was classified."""
    if LAST_ANSWER is None:
        print("\nNo message analysed yet - send one first.\n")
        return
    a = LAST_ANSWER
    print("\n--- CLASSIFICATION OF THE LAST MESSAGE ---")
    for label, value in [
        ("message type", getattr(a, "message_type", "")),
        ("service", a.service), ("intent", a.intent),
        ("buying intent", a.buying_intent), ("source", a.source),
        ("confidence", a.confidence), ("matched FAQ", a.faq_id),
        ("escalated", a.escalated), ("KB covered", a.covered),
        ("resolution", a.resolution), ("car brand", a.car_brand),
        ("car model", a.car_model), ("car year", a.car_year),
        ("knowledge gap", a.gap_topic),
        ("lead status", brain.lead_status_for(a)),
        ("is a lead", brain.is_lead(a)),
    ]:
        print(f"  {label:15}: {value}")
    if a.faq_id:
        faq = kb.faq_by_id(a.faq_id)
        print(f"\n  FAQ #{faq['id']} ({faq['category']}, "
              f"{'confirmed' if faq['confirmed'] else 'INFORMATION REQUIRED'})")
        print(f"    Q: {faq['question']}")
        print(f"    A: {faq['answer'] or '(no approved answer)'}")
    print()


def show_stats() -> None:
    """Live analytics. Every number is a query against real stored rows."""
    db.init_db()
    if db.database_is_empty():
        print("\nNo conversations stored yet. Turn on /save and chat, or run "
              "the bot, and these numbers will fill in.\n")
        return

    for label, days in [("Today", 1), ("Last 7 days", 7),
                        ("Last 30 days", 30), ("All time", None)]:
        ov = db.overview_metrics(days)
        print(f"\n--- {label} ---")
        for k, v in ov.items():
            print(f"  {k:22}: {v}")

    services = db.service_breakdown(None)
    if services:
        print("\n--- Services asked about (all time) ---")
        for row in services:
            print(f"  {row['service']:20} {row['conversations']}")

    prices = db.price_question_breakdown(None)
    if prices:
        print("\n--- Price questions by service (all time) ---")
        for row in prices:
            print(f"  {row['service']:20} {row['price_questions']}")

    perf = db.ai_performance(None)
    print("\n--- AI performance (all time) ---")
    for k, v in perf.items():
        print(f"  {k:22}: {v}")
    print()


def show_gaps() -> None:
    db.init_db()
    rows = db.list_knowledge_gaps(limit=50)
    if not rows:
        print("\nNo knowledge gaps recorded yet.\n")
        return
    print(f"\n--- KNOWLEDGE GAPS ({len(rows)}) ---")
    print(f"  {'topic':40} {'asked':>6}  {'last seen':16}")
    for r in rows:
        print(f"  {r['topic'][:40]:40} {r['occurrences']:>6}  "
              f"{r['last_seen'][:16]}")
    print("\nThese are the questions to answer in the manager questionnaire.\n")


def run_tests() -> None:
    """Run the automated suite in-process so /reload picks up edits."""
    try:
        import test_suite
        importlib.reload(test_suite)
        test_suite.main(use_ai=False)
    except Exception as error:
        print(f"\nCould not run the test suite: {error!r}\n")


def answer_message(message: str) -> None:
    global LAST_ANSWER
    started = time.time()

    # Always the production path, with memory: brain.process() is exactly
    # what the Instagram webhook calls.
    result = brain.process(CONSOLE_CUSTOMER, message, use_ai=USE_AI)

    LAST_ANSWER = result
    elapsed = time.time() - started

    chunks = brain_split(result.reply)
    note = f", {len(chunks)} Instagram messages" if len(chunks) > 1 else ""

    print(f"\n[{result.source}] {result.reply}")
    print(f"      ({elapsed:.1f}s, {len(result.reply)} chars{note}"
          f" | {getattr(result, 'message_type', '')}"
          f" | {result.service or 'no service'} / {result.intent}"
          f" | buying={result.buying_intent}"
          + (f" | FAQ#{result.faq_id}" if result.faq_id else "")
          + (" | ESCALATED" if result.escalated else "") + ")\n")


def brain_split(text: str):
    """Reuse the bot's own Instagram splitter so length warnings match live."""
    try:
        import bot
        return bot.split_for_instagram(text)
    except Exception:
        return [text]


def main() -> None:
    global SAVE_TO_DB, USE_AI, CONSOLE_CUSTOMER, _conversation_no
    _use_database(CONSOLE_DB)
    show_banner()

    while True:
        try:
            message = input("customer> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return

        if not message:
            continue
        command = message.lower()

        if command in ("/quit", "/exit", "/q"):
            print("Bye!")
            return
        if command == "/reload":
            current_db = config.DB_PATH          # a reload must not switch databases
            for module in (kb, brain, db, config):
                importlib.reload(module)
            importlib.reload(brain)      # re-index the FAQs after kb reload
            _use_database(current_db)    # config.py reload reset DB_PATH
            print("\nModules reloaded.\n")
            show_banner()
            continue
        if command == "/facts":
            show_facts(); continue
        if command == "/prompt":
            print("\n--- SYSTEM PROMPT ---")
            print(brain.SYSTEM_PROMPT); continue
        if command == "/intents":
            show_intents(); continue
        if command == "/stats":
            show_stats(); continue
        if command == "/gaps":
            show_gaps(); continue
        if command == "/test":
            run_tests(); continue
        if command == "/new":
            _conversation_no += 1
            CONSOLE_CUSTOMER = f"console-{_LAUNCH}-{_conversation_no}"
            print(f"\nNew conversation started: {CONSOLE_CUSTOMER} "
                  "(memory is clean).\n")
            continue
        if command == "/save":
            SAVE_TO_DB = not SAVE_TO_DB
            _use_database(PRODUCTION_DB if SAVE_TO_DB else CONSOLE_DB)
            print(f"\nNow using {'the PRODUCTION database' if SAVE_TO_DB else 'console.db'}"
                  f" ({config.DB_PATH}).\n")
            continue
        if command == "/noai":
            USE_AI = not USE_AI
            print(f"\nLanguage model: {'ON' if USE_AI else 'OFF'}\n")
            continue

        answer_message(message)


if __name__ == "__main__":
    main()
