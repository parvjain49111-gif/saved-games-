"""
=============================================================================
 Car Trends Car Mall - Instagram DM Customer Service Bot
=============================================================================

 WHAT THIS FILE IS
 -----------------
 The Meta-facing edge of the system. It owns the HTTP surface and nothing
 else: webhook verification, signature checking, payload parsing, duplicate
 protection, message splitting, and sending replies to Instagram.

 All of the THINKING lives in brain.py, which is also what the local console
 (chat.py) calls - so there is exactly one chatbot, not one for production
 and another for testing.

     Instagram (Meta)
            |
            v
     bot.py          receive, verify, parse, de-duplicate
            |
            v
     brain.py        intent + context -> manager data -> approved FAQs ->
            |        verified facts -> Ollama (wording only)
            v
     database.py     conversations, messages, leads, knowledge gaps
            |
            v
     dashboard.py    authenticated owner analytics at /dashboard

 THE MODULES
 -----------
     config.py     every environment-driven setting; the only reader of os.environ
     knowledge.py  business facts, the 99 approved FAQs, MANAGER_DATA
     brain.py      normalisation, intent detection, the answer pipeline
     database.py   SQLite persistence and all analytics aggregation
     dashboard.py  the owner dashboard (HTTP Basic auth, no default password)
     chat.py       local testing console
     test_suite.py 181 automated behaviour tests

 WHY THESE LIBRARIES
 -------------------
 - fastapi   : HTTP plumbing (routing, query params, JSON) so we write logic
 - uvicorn   : the web server process that actually runs the FastAPI app
 - requests  : outgoing HTTP - to Ollama, and to the Meta Graph API
 - hmac/hashlib : verify a webhook really came from Meta, not a stranger
 - sqlite3   : persistent storage, no server to install

 HOW TO RUN IT
 -------------
     pip install fastapi uvicorn requests
     ollama pull llama3.2:3b
     $env:DASHBOARD_PASSWORD = "something-long"     (enables /dashboard)
     $env:PAGE_ACCESS_TOKEN  = "..."                (enables sending)
     uvicorn bot:app --host 0.0.0.0 --port 8000

 "bot:app" means: in the file bot.py, use the variable named `app`.

 Then expose port 8000 to the internet (e.g. `ngrok http 8000`) and give
 Meta the resulting HTTPS URL + "/webhook" as your Callback URL.
=============================================================================
"""

# ---------------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------------
# Standard library (ships with Python, nothing to install):
import os          # read configuration from environment variables
import re          # keyword matching used by the intent router (Phase 1 rules)
import sys         # used to make console printing appear instantly (see below)
import json        # decode the raw JSON body Meta sends us
import hmac        # verify Meta's request signature (security)
import hashlib     # the SHA-256 hashing algorithm used by that signature
import textwrap    # neatly wrap long AI replies into Instagram-sized chunks
import time        # retry back-off for Graph API rate limits
from collections import deque      # a fixed-size list, used for de-duplication
from typing import Any, Dict, List, Optional, Tuple

# Third-party (installed via pip):
import requests
from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# Project modules. The split keeps each concern in one place:
#   config    - every environment-driven setting and secret
#   knowledge - business facts, the 99 approved FAQs, manager data
#   brain     - intent detection, the answer pipeline, Ollama
#   database  - persistent storage and analytics queries
#   dashboard - the authenticated owner dashboard
import config
import knowledge as kb
import brain
import comments
import database as db


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# os.getenv("NAME", "fallback") reads an environment variable. If the variable
# is not set on your machine, the second argument is used instead. This lets
# you keep real secrets out of the source code (safer if you ever share it),
# while still working out-of-the-box with the placeholders below.
#
# On Windows PowerShell you would set one like this:
#     $env:PAGE_ACCESS_TOKEN = "EAAG...your-real-token..."
# ---------------------------------------------------------------------------

# Every one of these now comes from config.py, which is the ONLY module that
# reads the environment. They are re-exported here under their original names
# so the rest of this file - and anything you have already written against it
# - keeps working unchanged.
#
# On Windows PowerShell you set a secret like this, before starting the bot:
#     $env:PAGE_ACCESS_TOKEN = "EAAG...your-real-token..."

VERIFY_TOKEN: str = config.VERIFY_TOKEN
PAGE_ACCESS_TOKEN: str = config.PAGE_ACCESS_TOKEN
APP_SECRET: str = config.APP_SECRET
GRAPH_API_URL: str = config.GRAPH_API_URL
GRAPH_TIMEOUT: Tuple[int, int] = config.GRAPH_TIMEOUT
MAX_DM_LENGTH: int = config.MAX_DM_LENGTH
OLLAMA_URL: str = config.OLLAMA_URL
OLLAMA_MODEL: str = config.OLLAMA_MODEL
OLLAMA_TIMEOUT: Tuple[int, int] = config.OLLAMA_TIMEOUT

# ---------------------------------------------------------------------------
# THE FASTAPI APPLICATION OBJECT
# ---------------------------------------------------------------------------
# `app` is the object uvicorn looks for. Every @app.get / @app.post decorator
# below attaches one URL route to this object.
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Car Trends Car Mall - Instagram DM Bot",
    description="Middleware between the Meta Graph API and a local Ollama LLM.",
    version="1.2.0",  # Reel comment automation: same brain, public-safe replies
)

# ---------------------------------------------------------------------------
# MAKE PRINTING INSTANT (important for requirement: watch the chat live)
# ---------------------------------------------------------------------------
# By default Python only flushes print() output in ~8KB blocks whenever stdout
# is NOT a terminal - for example when you run the bot with `> bot.log` or from
# inside a service manager, PyCharm, or Docker. That would make conversations
# appear minutes late, in bursts. Turning on line buffering forces every print
# out immediately, so the console always shows the live conversation.
# ---------------------------------------------------------------------------
try:
    # utf-8 + errors="replace": a reply containing an emoji must never
    # crash the print() that runs BEFORE the message is sent to Instagram.
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8",
                           errors="replace")
except Exception:
    pass  # very old Python or an unusual stdout - harmless to skip


# ---------------------------------------------------------------------------
# DUPLICATE-MESSAGE PROTECTION
# ---------------------------------------------------------------------------
# Meta re-sends a webhook if it does not receive a 200 OK fast enough. Without
# protection, a slow reply could make the bot answer the same customer twice.
# Every Instagram message carries a unique "mid" (message id), so we remember
# the last 500 ids we have already handled and ignore repeats.
#
# `deque(maxlen=500)` automatically discards the oldest id when it is full, so
# memory usage stays constant no matter how long the bot runs.
# ---------------------------------------------------------------------------
_seen_message_ids: deque = deque(maxlen=500)
_seen_lookup: set = set()


def already_processed(message_id: Optional[str]) -> bool:
    """Return True if we have handled this message id before.

    TWO LAYERS, because a restart used to wipe the memory-only guard:
      1. the in-process set below - instant, catches Meta's rapid retries
      2. the processed_events table - survives restarts and is shared by
         every worker thread, so two threads cannot both claim the same id

    Messages with no id (rare/malformed) are always allowed through.
    """
    if not message_id:
        return False

    if message_id in _seen_lookup:
        return True

    # The durable check is authoritative. Its INSERT either succeeds (new)
    # or trips the primary key (already handled), which makes the test and
    # the claim a single atomic step.
    try:
        if db.event_already_seen(message_id):
            _seen_lookup.add(message_id)
            _seen_message_ids.append(message_id)
            return True
    except Exception as error:
        # A database problem must never stop us replying to a customer.
        print(f"[DEDUP] database check failed, using memory only: {error!r}")

    # If the deque is already full, adding a new id silently evicts the oldest
    # one - we must drop that same id from our lookup set to keep them in sync.
    if len(_seen_message_ids) == _seen_message_ids.maxlen:
        evicted = _seen_message_ids[0]
        _seen_lookup.discard(evicted)

    _seen_message_ids.append(message_id)
    _seen_lookup.add(message_id)
    return False


# ---------------------------------------------------------------------------
# SECURITY: VERIFY THE REQUEST REALLY CAME FROM META
# ---------------------------------------------------------------------------
def signature_is_valid(raw_body: bytes, header_value: Optional[str]) -> bool:
    """Check Meta's 'X-Hub-Signature-256' header against the raw request body.

    Meta hashes the exact bytes of the request body using your App Secret and
    sends the result in a header shaped like 'sha256=abc123...'. We repeat the
    same calculation locally; if the two hashes match, the request is genuine.

    NOTE: this must run on the *raw* bytes, before any JSON parsing, because
    re-serialising the JSON would change spacing and break the hash.

    Returns True automatically when APP_SECRET is empty, i.e. the check is
    opt-in so the bot still works before you configure it.
    """
    secrets_to_try = [s for s in (config.APP_SECRET, config.INSTAGRAM_APP_SECRET) if s]
    if not secrets_to_try:
        return True
    if not header_value or not header_value.startswith("sha256="):
        return False
    received = header_value.split("=", 1)[1]
    for secret in secrets_to_try:
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received):
            return True
    return False


# ---------------------------------------------------------------------------
# ENDPOINT 1 -  GET /webhook   (one-time verification handshake with Meta)
# ---------------------------------------------------------------------------
# When you paste your Callback URL into the Meta dashboard and press "Verify
# and Save", Meta immediately calls this URL with three query parameters:
#
#     GET /webhook?hub.mode=subscribe&hub.verify_token=my_secret_token
#                 &hub.challenge=1158201444
#
# We must confirm hub.mode is "subscribe" and that hub.verify_token matches
# our own VERIFY_TOKEN. If both are correct we echo hub.challenge back as
# PLAIN TEXT. Meta compares what we echo against what it sent; only an exact
# match completes the subscription.
#
# The parameter names contain a dot, which is not valid in a Python variable
# name, so we use `alias=` to map "hub.mode" onto the variable `hub_mode`.
# ---------------------------------------------------------------------------
@app.get("/webhook")
def verify_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
):
    print("\n[VERIFY] Meta sent a verification request.")
    print(f"[VERIFY]   mode  = {hub_mode}")
    print(f"[VERIFY]   token = {'(matches)' if hub_verify_token == VERIFY_TOKEN else '(does not match)'}")

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        print("[VERIFY]   RESULT: token matched - echoing the challenge back.\n")
        # PlainTextResponse (not JSON!) because Meta expects the bare value.
        return PlainTextResponse(content=hub_challenge or "", status_code=200)

    print("[VERIFY]   RESULT: token mismatch - rejecting with 403.\n")
    return PlainTextResponse(content="Verification token mismatch", status_code=403)


# ---------------------------------------------------------------------------
# PARSING HELPER - dig the useful bits out of Meta's nested JSON
# ---------------------------------------------------------------------------
# A real Instagram message webhook looks roughly like this:
#
# {
#   "object": "instagram",
#   "entry": [
#     {
#       "id": "17841400000000000",
#       "time": 1700000000,
#       "messaging": [
#         {
#           "sender":    {"id": "6789012345678901"},   <-- the CUSTOMER
#           "recipient": {"id": "17841400000000000"},  <-- YOUR business
#           "timestamp": 1700000000,
#           "message": {
#             "mid":  "aWc6...",
#             "text": "Do you have Moco Android screens?"
#           }
#         }
#       ]
#     }
#   ]
# }
#
# Every one of those keys can be missing, and "messaging" also carries events
# we do NOT want to answer:
#   * message.is_echo   -> a copy of a message WE sent. Replying would create
#                          an infinite loop of the bot talking to itself.
#   * "read" / "delivery" -> read receipts, not real messages.
#   * attachments with no "text" -> a photo or voice note; nothing to read.
#
# Rather than writing payload["entry"][0]["messaging"][0]["message"]["text"],
# which explodes with a KeyError or IndexError the moment anything differs,
# we walk the structure defensively with .get() and isinstance() checks and
# return a clean list of (sender_id, message_id, text) tuples.
# ---------------------------------------------------------------------------
def extract_messages(payload: Any) -> List[Tuple[str, str, str]]:
    """Safely pull every real text message out of a Meta webhook payload."""
    found: List[Tuple[str, str, str]] = []

    try:
        # Guard 1: the payload must be a dictionary at the top level.
        if not isinstance(payload, dict):
            print("[PARSE] Payload was not a JSON object - ignoring.")
            return found

        # Guard 2: "entry" must exist and be a list. A single webhook can
        # legitimately batch several entries together.
        entries = payload.get("entry")
        if not isinstance(entries, list):
            print("[PARSE] No 'entry' list in payload - ignoring.")
            return found

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            # Instagram uses "messaging"; some Meta products use "standby"
            # for handover-protocol events. We read "messaging" only.
            events = entry.get("messaging")
            if not isinstance(events, list):
                continue

            for event in events:
                if not isinstance(event, dict):
                    continue

                # --- who sent it? -------------------------------------------
                sender = event.get("sender")
                sender_id = sender.get("id") if isinstance(sender, dict) else None
                if not sender_id:
                    continue  # cannot reply without an address

                # --- is it actually a message? ------------------------------
                message = event.get("message")
                if not isinstance(message, dict):
                    # Read receipts / delivery reports land here. Skip quietly.
                    continue

                # --- is it our own message echoed back to us? ---------------
                if message.get("is_echo"):
                    print("[PARSE] Skipping echo of our own outgoing message.")
                    continue

                # --- does it contain text we can send to the AI? ------------
                text = message.get("text")
                if not isinstance(text, str) or not text.strip():
                    print("[PARSE] Message had no text (image/sticker?) - skipping.")
                    continue

                message_id = message.get("mid") or ""
                found.append((str(sender_id), str(message_id), text.strip()))

    except Exception as error:
        # A catch-all so a surprising payload shape can never crash the server.
        print(f"[PARSE] Unexpected error while parsing payload: {error!r}")

    return found


# ---------------------------------------------------------------------------
# COMMENT EVENTS  (Instagram webhook field "comments")
# ---------------------------------------------------------------------------
# A comment on a Reel or post arrives in the SAME webhook, but under
# entry["changes"] instead of entry["messaging"]:
#
#   {"object": "instagram", "entry": [{"id": "<our IG user id>", "changes": [
#       {"field": "comments", "value": {
#           "id": "<comment id>", "text": "GFX pro mats?",
#           "from": {"id": "<commenter id>", "username": "..."},
#           "media": {"id": "<media id>", "media_product_type": "REELS"},
#           "parent_id": "<comment id of the thread>"   # only for replies
#       }}]}]}
#
# Our own replies come back through this webhook too (from.id == entry.id);
# answering them would create an infinite loop, so they are skipped.
# ---------------------------------------------------------------------------
def extract_comments(payload: Any) -> List[Dict[str, str]]:
    """Safely pull every answerable comment out of a Meta webhook payload."""
    found: List[Dict[str, str]] = []
    try:
        if not isinstance(payload, dict):
            return found
        if payload.get("object") not in (None, "instagram"):
            return found                          # a Page / other object - not ours
        entries = payload.get("entry")
        if not isinstance(entries, list):
            return found
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            our_id = str(entry.get("id") or "")
            changes = entry.get("changes")
            if not isinstance(changes, list):
                # Meta's comment-moderation guide shows the change at entry level.
                changes = [entry] if entry.get("field") else []
            for change in changes:
                if not isinstance(change, dict) or change.get("field") != "comments":
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                comment_id = str(value.get("id") or value.get("comment_id") or "")
                sender = value.get("from")
                commenter_id = str(sender.get("id") or "") if isinstance(sender, dict) else ""
                username = str(sender.get("username") or "").lstrip("@").lower() \
                    if isinstance(sender, dict) else ""
                media = value.get("media")
                media_id = str(media.get("id") or "") if isinstance(media, dict) else ""
                media_type = str(media.get("media_product_type") or "") if isinstance(media, dict) else ""
                text = value.get("text")
                if not comment_id or not commenter_id or not media_id:
                    print("[COMMENT] unrecognised comment payload - ignoring.")
                    continue
                # Our own replies come back through this webhook: never answer them.
                if (commenter_id == our_id
                        or (config.INSTAGRAM_ACCOUNT_ID and commenter_id == config.INSTAGRAM_ACCOUNT_ID)
                        or (config.INSTAGRAM_USERNAME and username == config.INSTAGRAM_USERNAME)):
                    print("[COMMENT] Skipping our own comment echoed back.")
                    continue
                if not isinstance(text, str) or not text.strip():
                    continue
                found.append({"comment_id": comment_id, "commenter_id": commenter_id,
                              "media_id": media_id, "text": text.strip(),
                              "parent_id": str(value.get("parent_id") or ""),
                              "media_type": media_type, "username": username})
    except Exception as error:
        print(f"[COMMENT] Unexpected error while parsing payload: {error!r}")
    return found


# META - send the reply back to the customer
# ---------------------------------------------------------------------------
def split_for_instagram(text: str, limit: int = MAX_DM_LENGTH) -> List[str]:
    """Break a long reply into Instagram-sized chunks, splitting on whitespace
    so words are never cut in half. Short replies come back as a 1-item list.
    """
    if len(text) <= limit:
        return [text]
    return textwrap.wrap(text, width=limit, break_long_words=True, replace_whitespace=False)


def send_instagram_reply(recipient_id: str, text: str) -> bool:
    """POST the AI's answer to the Graph API so it appears in the customer's DMs.

    The access token travels in an Authorization header rather than in the URL,
    because anything placed in a URL tends to end up in server logs.

    Returns True only if every chunk was delivered successfully.
    """
    if not config.send_is_configured():
        print("[SEND] PAGE_ACCESS_TOKEN is not configured - reply NOT sent.")
        print(f"[SEND] (Would have sent to {recipient_id}: {text})")
        return False

    headers = {"Authorization": f"Bearer {config.PAGE_ACCESS_TOKEN}"}
    all_ok = True

    for chunk in split_for_instagram(text):
        body = {
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
        }
        try:
            response = requests.post(
                GRAPH_API_URL, json=body, headers=headers, timeout=GRAPH_TIMEOUT
            )
            if response.status_code == 200:
                print(f"[SEND] Delivered to {recipient_id} ({len(chunk)} chars).")
            else:
                # Meta explains refusals in the body - print it, it is the
                # fastest way to debug an expired token or a closed 24h window.
                all_ok = False
                print(f"[SEND] Meta returned HTTP {response.status_code}: {response.text}")
        except Exception as error:
            all_ok = False
            print(f"[SEND] Network error while contacting Meta: {type(error).__name__}")

    return all_ok


# Graph API error codes that mean "try again later", not "this is wrong".
_RATE_LIMIT_CODES = {4, 17, 32, 613, 80002, 80007}
_RETRY_DELAYS = (2, 5, 10)


def meta_block_note(code: Optional[int], message: str) -> str:
    """Meta answers an account-level security checkpoint with code 200 and the
    text "API access blocked." for EVERY endpoint, including reading the
    profile. Nothing in this bot can clear it: the account owner has to sign in
    to Instagram or Meta and pass the identity check. Say so plainly, because
    the symptom on Instagram is simply that the bot went quiet."""
    if code == 200 and "api access blocked" in (message or "").lower():
        return ("Meta has blocked API access for this account (an account security "
                "checkpoint). Replies cannot be sent until the owner signs in at "
                "instagram.com or developers.facebook.com and completes the identity "
                "check Meta asks for. No change to this bot can lift it.")
    return ""


def _graph_post(url: str, body: Dict[str, Any], what: str,
                form: bool = False) -> Optional[Dict[str, Any]]:
    """POST to the Graph API with retries on rate limits. Returns the JSON
    body on success, None on a final failure (already logged)."""
    headers = {"Authorization": f"Bearer {config.PAGE_ACCESS_TOKEN}"}
    for attempt, delay in enumerate((0,) + _RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            response = (requests.post(url, data=body, headers=headers, timeout=GRAPH_TIMEOUT) if form
                        else requests.post(url, json=body, headers=headers, timeout=GRAPH_TIMEOUT))
        except Exception as error:
            print(f"[COMMENT] Network error while sending {what}: {type(error).__name__}")
            continue
        if response.status_code == 200:
            try:
                return response.json() if response.text else {}
            except ValueError:
                return {}
        code, message = None, ""
        try:
            error = response.json().get("error") or {}
            code, message = error.get("code"), str(error.get("message") or "")
        except ValueError:
            pass
        print(f"[COMMENT] Meta returned HTTP {response.status_code} for {what}: {response.text[:300]}")
        note = meta_block_note(code, message)
        if note:
            print(f"[META] {note}")
        if code not in _RATE_LIMIT_CODES:
            return None                       # a real refusal - retrying will not help
    return None


def send_comment_reply(comment_id: str, text: str) -> Optional[str]:
    """Reply PUBLICLY under a comment: POST /{comment_id}/replies.

    Returns the id of the reply Meta created (so its echo can be ignored),
    "" when sending is not configured, or None on failure.
    """
    if not config.send_is_configured():
        print("[COMMENT] PAGE_ACCESS_TOKEN is not configured - public reply NOT sent.")
        print(f"[COMMENT] (Would have replied under {comment_id}: {text})")
        return ""
    data = _graph_post(f"{config.GRAPH_API_BASE}/{comment_id}/replies",
                       {"message": text}, f"public reply under {comment_id}", form=True)
    if data is None:
        return None
    new_id = str(data.get("id") or "")
    print(f"[COMMENT] Public reply posted under {comment_id} (id {new_id or '?'}).")
    return new_id


def send_private_reply(comment_id: str, text: str) -> bool:
    """Send the full answer PRIVATELY to the commenter (Meta "private replies").

    Meta allows one private reply per comment, within 7 days, on the
    business's own media. The recipient is the COMMENT, not a user id.
    """
    # Meta allows exactly ONE private reply per comment, so the answer is
    # sent as a single message; an over-long answer is cut at a word with a
    # pointer to the phone number rather than split into a second message.
    if len(text) > MAX_DM_LENGTH:
        text = text[:MAX_DM_LENGTH - 60].rsplit(" ", 1)[0] + f" … (more on WhatsApp {kb.PHONE})"
    if not config.send_is_configured():
        print("[COMMENT] PAGE_ACCESS_TOKEN is not configured - private reply NOT sent.")
        print(f"[COMMENT] (Would have sent privately for {comment_id}: {text})")
        return False
    data = _graph_post(GRAPH_API_URL,
                       {"recipient": {"comment_id": comment_id}, "message": {"text": text}},
                       f"private reply for {comment_id}")
    if data is None:
        return False
    print(f"[COMMENT] Private reply sent for {comment_id} ({len(text)} chars).")
    return True


# ---------------------------------------------------------------------------
# THE BACKGROUND WORKER
# ---------------------------------------------------------------------------
# This function does the slow work: calling Ollama, then calling Meta. It is
# deliberately a normal `def` (not `async def`). FastAPI runs plain functions
# in a worker thread, so `requests` blocking for 30 seconds cannot freeze the
# rest of the server. It is scheduled by BackgroundTasks and therefore runs
# only AFTER our 200 OK has already been sent to Meta (see requirement 7).
# ---------------------------------------------------------------------------
def handle_message(sender_id: str, user_message: str) -> None:
    """Run one full conversation turn for a single customer message.

    All of the thinking now lives in brain.process(), which is the SAME
    function the local console (chat.py) calls. There is no separate
    testing brain, so whatever you try in the console is exactly what a
    customer receives here.

    brain.process() also does the persistence: it opens or continues the
    conversation, stores both messages, classifies the exchange for
    analytics, records a knowledge gap when we could not answer, and
    creates or enriches the sales lead. This function only has to print
    the exchange and put the reply on Instagram.
    """
    print("\n" + "=" * 70)
    print(f"[CUSTOMER {sender_id}] {user_message}")
    print("-" * 70)

    result = brain.process(sender_id, user_message)

    # The tag makes it obvious at a glance which layer answered:
    #   FAQ        approved FAQ knowledge base
    #   MANAGER    manager-supplied answer
    #   RULE       verified core fact (location / hours / contact)
    #   ESCALATION we did not know, so the customer was handed to the team
    #   AI         llama3.2:3b wrote the wording
    print(f"[CAR TRENDS | {result.source:<10}] {result.reply}")
    print(f"           service={result.service} intent={result.intent} "
          f"buying={result.buying_intent} conf={result.confidence}"
          + (f" faq#{result.faq_id}" if result.faq_id else "")
          + (" ESCALATED" if result.escalated else ""))
    print("=" * 70 + "\n")

    send_instagram_reply(sender_id, result.reply)


# Texts we posted publicly in this process: a comment carrying exactly one of
# them is our own reply echoed back, whatever id space Meta used for it.
_recent_public_texts: deque = deque(maxlen=300)


def handle_comment(comment_id: str, commenter_id: str, media_id: str, text: str,
                   parent_id: str = "") -> None:
    """Run one comment through the SAME brain and deliver the two replies.

    comments.handle_comment() calls brain.process() under a conversation id
    that is private to this commenter on this Reel, so "GFX mats?" followed
    by "Alto" is understood exactly like a DM - and never mixes with another
    Reel, another commenter, or this user's DMs.
    """
    print("\n" + "=" * 70)
    print(f"[COMMENT {commenter_id} on media {media_id}] {text}")
    print("-" * 70)
    result = comments.handle_comment(comment_id, commenter_id, media_id, text)
    a = result["answer"]
    if result.get("skipped") or a is None:
        print("[COMMENT] no reply (content-free / spam).")
        print("=" * 70 + "\n")
        return
    print(f"[PUBLIC  | {a.source:<10}] {result['public']}")
    print(f"[PRIVATE | {a.source:<10}] {result['private']}")
    print(f"           type={a.message_type} service={a.service} product={a.product} "
          f"intent={a.intent}" + (" ESCALATED" if a.escalated else ""))
    print("=" * 70 + "\n")
    # Instagram threads are one level deep: a public reply must hang under
    # the thread's top-level comment, so a follow-up posted as a reply
    # ("Alto" under our answer) is answered in the same thread.
    target = parent_id or comment_id
    public_ok = private_ok = True
    if result["public"]:
        new_id = send_comment_reply(target, result["public"])
        public_ok = new_id is not None
        _recent_public_texts.append(result["public"])
        if new_id:
            try:
                db.event_already_seen("ours:" + new_id)   # its echo is ignored
            except Exception as error:
                print(f"[COMMENT] could not record our reply id: {error!r}")
    if config.COMMENT_PRIVATE_REPLY and result["private"]:
        private_ok = send_private_reply(comment_id, result["private"])
    if config.send_is_configured() and not (public_ok or private_ok):
        # Nothing reached the customer: release the id so Meta's redelivery
        # (or a manual replay) can try again instead of being swallowed.
        try:
            db.forget_event("comment:" + comment_id)
        except Exception as error:
            print(f"[COMMENT] could not release {comment_id}: {error!r}")
        print(f"[COMMENT] FAILED to deliver any reply for {comment_id} - id released for retry.")


# ---------------------------------------------------------------------------
# ENDPOINT 2 -  POST /webhook   (every incoming customer message)
# ---------------------------------------------------------------------------
# Meta expects a 200 OK within a few seconds. If we made it wait for Ollama,
# Meta would assume we timed out and re-deliver the same message repeatedly.
#
# So this endpoint does the bare minimum - verify, parse, queue - and returns
# immediately. `background_tasks.add_task(...)` tells FastAPI: "send the
# response first, then run this function." That is what keeps us inside
# Meta's timeout no matter how slow the local model is.
# ---------------------------------------------------------------------------
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    # Read the raw bytes first. The signature check below needs the exact
    # bytes Meta hashed, so we cannot let FastAPI parse the JSON for us.
    raw_body = await request.body()

    # --- security check ----------------------------------------------------
    if not signature_is_valid(raw_body, request.headers.get("x-hub-signature-256")):
        print("[WEBHOOK] Signature check FAILED - request rejected.")
        return JSONResponse(content={"status": "invalid signature"}, status_code=403)

    # --- decode the JSON ---------------------------------------------------
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as error:
        # Still answer 200: a malformed body will never become valid, so
        # there is no point making Meta retry it forever.
        print(f"[WEBHOOK] Body was not valid JSON ({error!r}) - ignoring.")
        return JSONResponse(content={"status": "ignored"}, status_code=200)

    # --- pull out the real messages ---------------------------------------
    messages = extract_messages(payload)

    for sender_id, message_id, text in messages:
        if already_processed(message_id):
            print(f"[WEBHOOK] Duplicate delivery of {message_id} - ignoring.")
            continue
        # Queue the slow work. Nothing here blocks the response below.
        background_tasks.add_task(handle_message, sender_id, text)

    # --- and the comments (Reels / posts), on the same webhook ------------
    comment_events = extract_comments(payload) if config.COMMENT_REPLIES_ENABLED else []
    for c in comment_events:
        cid = c["comment_id"]
        try:
            if db.event_recorded("ours:" + cid) or c["text"] in _recent_public_texts:
                print(f"[WEBHOOK] Our own reply {cid} echoed back - ignoring.")
                continue
        except Exception as error:
            print(f"[WEBHOOK] echo check failed: {error!r}")
        if c["parent_id"]:
            # A reply inside a thread: answer it only when it comes from the
            # commenter who opened that thread ("GFX mats?" ... "Alto").
            try:
                owner = db.thread_owner(c["parent_id"])
            except Exception as error:
                print(f"[WEBHOOK] thread lookup failed: {error!r}")
                owner = None
            if owner != c["commenter_id"]:
                print(f"[WEBHOOK] Reply {cid} in a thread we did not open with this "
                      "commenter - not ours to answer.")
                continue
        if already_processed("comment:" + cid):
            print(f"[WEBHOOK] Duplicate delivery of comment {cid} - ignoring.")
            continue
        try:
            db.remember_thread(cid, c["commenter_id"], c["media_id"])
        except Exception as error:
            print(f"[WEBHOOK] could not record thread: {error!r}")
        print(f"[WEBHOOK] comment {cid} on {c['media_type'] or 'media'} {c['media_id']} queued.")
        background_tasks.add_task(handle_comment, cid, c["commenter_id"],
                                  c["media_id"], c["text"], c["parent_id"])
    if not messages and not comment_events:
        print("[WEBHOOK] Received an event with no answerable text message.")

    # --- answer Meta immediately ------------------------------------------
    return JSONResponse(content={"status": "EVENT_RECEIVED"}, status_code=200)


# ---------------------------------------------------------------------------
# ENDPOINT 3 -  GET /   (a simple health check for your own peace of mind)
# ---------------------------------------------------------------------------
# Open http://localhost:8000/ in a browser. If you see this JSON, the server
# is alive and you can also tell at a glance whether Ollama is reachable.
# ---------------------------------------------------------------------------
PRIVACY_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Privacy Policy - Car Trends Car Mall Instagram Assistant</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#222}</style></head>
<body><h1>Privacy Policy</h1><p><strong>Car Trends Car Mall Instagram Assistant</strong> (the "Assistant") is an automated
support tool operated by Car Trends Car Mall, Jaipur, India. It answers questions sent to the Car Trends Car Mall Instagram
account by direct message and replies to comments on its posts and reels.</p>
<h2>What we receive</h2><p>When you message or comment, Meta sends the Assistant your Instagram-scoped user ID, your username
where Meta provides it, and the text of your message or comment. The Assistant does not receive your password, email address,
phone number or contacts, and it does not read your other conversations.</p>
<h2>How we use it</h2><p>The text is used only to prepare a reply about Car Trends Car Mall (showroom address and hours, membership,
used-car enquiries and test-drive requests). Enquiries that need a human, such as a test-drive booking, are passed to the
Car Trends Car Mall team so they can contact you.</p>
<h2>Storage and sharing</h2><p>Conversations are stored on Car Trends Car Mall's own systems so the Assistant can remember the
context of your enquiry. We do not sell your data and do not share it with third parties other than Meta's Instagram
platform, which delivers the messages, and the Car Trends Car Mall team.</p>
<h2>Deleting your data</h2><p>Removing the Assistant from your Instagram account, or asking Meta to delete your data, triggers
the deletion callback registered with Meta, and your conversation history is erased. You can also email
<a href="mailto:foundersteam@cartrends.net">foundersteam@cartrends.net</a> from any account and we will delete it.</p>
<h2>Contact</h2><p>Car Trends Car Mall, Opposite ISKCON Temple, Kharbas Cir Rd, Dholai, Jaipur 302020. Phone 6367857737.
Email <a href="mailto:foundersteam@cartrends.net">foundersteam@cartrends.net</a>.</p></body></html>"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    """Public privacy policy; Meta requires a Privacy Policy URL before an app
    can go Live (App settings > Basic)."""
    return HTMLResponse(PRIVACY_HTML)


@app.get("/")
def health_check():
    try:
        # Ollama's root URL returns "Ollama is running" when the service is up.
        base_url = OLLAMA_URL.split("/api/")[0]
        ollama_ok = requests.get(base_url, timeout=3).status_code == 200
    except Exception:
        ollama_ok = False

    try:
        db_ok = db.get_connection().execute("SELECT 1").fetchone() is not None
    except Exception:
        db_ok = False

    # Deliberately says nothing about tokens or passwords beyond whether one
    # is present - this endpoint is unauthenticated.
    return {
        "service": f"{kb.BUSINESS['name']} Instagram DM Bot",
        "status": "running",
        "model": OLLAMA_MODEL,
        "ollama_reachable": ollama_ok,
        "database_ready": db_ok,
        "approved_faqs": len(kb.APPROVED_FAQS),
        "confirmed_faqs": len(kb.CONFIRMED_FAQS),
        "missing_info_faqs": len(kb.MISSING_INFO_FAQS),
        "signature_verification": bool(config.APP_SECRET or config.INSTAGRAM_APP_SECRET),
        "login_flow": config.META_LOGIN_FLOW,
        "send_token_configured": config.send_is_configured(),
        "dashboard_configured": config.dashboard_is_configured(),
        "comment_replies_enabled": config.COMMENT_REPLIES_ENABLED,
        "comment_private_reply": config.COMMENT_PRIVATE_REPLY,
        "instagram_connected": bool(config.PAGE_ACCESS_TOKEN and config.INSTAGRAM_ACCOUNT_ID),
    }


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
# Creating the schema on startup means a fresh clone just works: the very
# first run builds cartrends.db, and every later run finds it already there.
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    db.init_db()
    db.prune_processed_events(days=7)
    print(f"[STARTUP] database ready at {config.DB_PATH}")
    print(f"[STARTUP] {len(kb.CONFIRMED_FAQS)} confirmed FAQs, "
          f"{len(kb.MISSING_INFO_FAQS)} awaiting business information")
    if not config.dashboard_is_configured():
        print("[STARTUP] DASHBOARD_PASSWORD is not set - "
              "the owner dashboard will refuse every request.")
    # Self-maintenance: token refresh, tunnel follow-up, webhook re-registration.
    if os.getenv("CARTRENDS_MAINTENANCE", "1") == "1":
        try:
            import instagram_connect as _ic
            if _ic.start_maintenance_thread():
                print("[STARTUP] self-maintenance thread started (token refresh, webhook check)")
        except Exception as _maint_error:      # pragma: no cover - defensive only
            print(f"[STARTUP] self-maintenance unavailable: {_maint_error!r}")


# ---------------------------------------------------------------------------
# OWNER DASHBOARD
# ---------------------------------------------------------------------------
# Mounted last so a failure to import it can never take the webhook down -
# answering customers matters more than showing charts.
# ---------------------------------------------------------------------------
try:
    import dashboard
    app.include_router(dashboard.router)
    print("[STARTUP] owner dashboard mounted at /dashboard")
except Exception as _dash_error:      # pragma: no cover - defensive only
    print(f"[STARTUP] dashboard unavailable: {_dash_error!r}")

# The owner-only "Connect Instagram" setup flow (OAuth with Meta). Same
# guard: a problem here must never take the webhook down.
try:
    import instagram_connect
    app.include_router(instagram_connect.router)
    print("[STARTUP] Instagram connect flow mounted at /connect/instagram")
    _note = instagram_connect.adopt_env_token()
    if _note:
        print(f"[STARTUP] {_note}")
except Exception as _connect_error:   # pragma: no cover - defensive only
    print(f"[STARTUP] connect flow unavailable: {_connect_error!r}")


# ---------------------------------------------------------------------------
# OPTIONAL CONVENIENCE LAUNCHER
# ---------------------------------------------------------------------------
# The recommended way to start the bot is:  uvicorn bot:app --port 8000
# But running `python bot.py` directly works too, thanks to this block.
# `__name__ == "__main__"` is True only when this file is run directly, not
# when it is imported by uvicorn - which prevents a double start-up.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    B = kb.BUSINESS
    print("\n" + "=" * 72)
    print(f" {B['name']} - Instagram DM Bot")
    print(f" {B['tagline']}")
    print("=" * 72)
    print(f" Address        : {B['address']}")
    print(f" Phone/WhatsApp : {B['phone']}")
    print(f" Hours          : {B['hours_sentence']}")
    print("-" * 72)
    print(f" Knowledge base : {len(kb.CONFIRMED_FAQS)} confirmed FAQs")
    print(f"                  {len(kb.MISSING_INFO_FAQS)} awaiting business "
          f"information")
    manager_answers = sum(len(s) for s in kb.MANAGER_DATA.values())
    print(f" Manager data   : {manager_answers} answers supplied")
    print("-" * 72)
    print(f" Model          : {OLLAMA_MODEL}")
    print(f" Ollama URL     : {OLLAMA_URL}")
    print(f" Database       : {config.DB_PATH}")
    print(f" Send token     : "
          f"{'configured' if config.send_is_configured() else 'NOT SET'}")
    print(f" Dashboard      : "
          f"{'/dashboard' if config.dashboard_is_configured() else 'DISABLED (set DASHBOARD_PASSWORD)'}")
    _port = int(os.getenv("PORT", "8000"))      # hosts (Railway etc.) assign the port
    print(f" Listening on   : http://0.0.0.0:{_port}")
    print(" Webhook path   : /webhook")
    print("=" * 72 + "\n")

    # host="0.0.0.0" makes the server reachable from outside this PC, which
    # ngrok (or any tunnel) needs in order to forward Meta's requests to us.
    # access_log=False: uvicorn's access log would otherwise record the
    # webhook verify token and OAuth authorization codes from query strings.
    # (When starting with the uvicorn command, add --no-access-log.)
    uvicorn.run("bot:app", host="0.0.0.0", port=_port, reload=False, access_log=False)
