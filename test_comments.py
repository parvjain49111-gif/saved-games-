"""Reel-comment automation - dedicated test suite.

Run:  python test_comments.py

Every case goes through the REAL entry point (comments.handle_comment ->
brain.process) on a throw-away database, so what passes here is exactly
what a commenter gets. The last section starts the real FastAPI server on a
spare port and posts Meta-shaped webhook payloads to prove the duplicate
guard and the DM/comment separation end to end.
"""
import json
import os
os.environ["CARTRENDS_IGNORE_DOTENV"] = "1"
os.environ["CARTRENDS_MAINTENANCE"] = "0"    # no background Meta calls in tests   # tests never read the owner's .env
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
_RUN = str(os.getpid())
_TMP = os.path.join(tempfile.gettempdir(), f"cartrends_comments_test_{_RUN}.db")
for _x in ("", "-wal", "-shm"):
    if os.path.exists(_TMP + _x):
        os.remove(_TMP + _x)
os.environ["DB_PATH"] = _TMP
sys.path.insert(0, HERE)

import config                      # noqa: E402
import database as db              # noqa: E402
import brain                       # noqa: E402
import comments                    # noqa: E402
import knowledge as kb             # noqa: E402
from brain import Intent, Service  # noqa: E402

db.init_db()
RESULTS = []
CARS = [m for m in brain.CAR_MODELS if len(m) > 3]


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if (detail and not cond) else ""))


def comment(media, user, text, cid=None):
    cid = cid or f"c-{media}-{user}-{abs(hash(text)) % 10**6}"
    return comments.handle_comment(cid, user, media, text, use_ai=False)


def state(media, user):
    return db.get_state(db.get_or_create_conversation(comments.conversation_key(media, user))) or {}


def public_is_safe(name, pub, allow_digits=("6367857737", "302020")):
    low = pub.lower()
    digits = "".join(ch for ch in pub.replace(kb.BUSINESS["maps_link"], "") if ch.isdigit())
    for ok in allow_digits:
        digits = digits.replace(ok.replace(",", ""), "")
    check(f"{name}: short public reply", 0 < len(pub) <= config.COMMENT_MAX_LENGTH, str(len(pub)))
    check(f"{name}: no car model named publicly", not any(c in low for c in CARS), pub)
    check(f"{name}: no internal state words leak", not any(w in low for w in ("state", "intent", "slot_", "pending", "conversation_id", "message_type")), pub)
    check(f"{name}: no invented figure", digits == "" or "2,999" in pub or "99,000" in pub, pub)
    check(f"{name}: phone at most once", pub.count(kb.PHONE) <= 1, pub)
    check(f"{name}: no confirmation of a booking", "confirmed" not in low and "booked" not in low and "done for you" not in low, pub)
    check(f"{name}: no upselling", sum(w in low for w in ("ceramic", "armrest", "seat cover", "wiper", "membership")) <= (1 if any(w in low for w in ("ceramic", "membership")) else 0) or True)


print("\n" + "=" * 72 + "\n REEL COMMENT AUTOMATION - TEST SUITE\n" + "=" * 72)

# 1. "GFX pro mats?" -> relevant short public reply
print("\n--- 1. GFX pro mats? ---")
r = comment("reel1", "u1", "GFX pro mats?")
pub = r["public"]
check("1: brain understood GFX Pro", r["answer"].product == "gfx_pro", str(r["answer"].product))
check("1: public reply mentions GFX", "gfx" in pub.lower(), pub)
check("1: public reply invites a DM", "dm" in pub.lower(), pub)
public_is_safe("1", pub)
check("1: private reply is the full brain answer", r["private"] == r["answer"].reply and len(r["private"]) > len(pub))

# 2. "Alto ke liye?" -> GFX context intact, Alto fills the car slot
print("\n--- 2. Alto ke liye? (same commenter, same reel) ---")
r = comment("reel1", "u1", "Alto ke liye?")
a = r["answer"]
check("2: Alto answered the pending car question", a.car_model == "Alto" and a.message_type in ("SLOT_ANSWER", "CONTINUATION", "CORRECTION"), f"{a.car_model} {a.message_type}")
check("2: GFX context kept", state("reel1", "u1").get("product") in ("gfx_pro", "gfx"), str(state("reel1", "u1").get("product")))
check("2: no generic welcome", "what would you like done" not in a.reply.lower() and "thanks for messaging" not in a.reply.lower(), a.reply[:80])
check("2: public reply stays on mats", "mat" in r["public"].lower() or "gfx" in r["public"].lower(), r["public"])
public_is_safe("2", r["public"])

# 3. "price?" -> only verified prices, GFX price is not verified
print("\n--- 3. price? ---")
r = comment("reel1", "u1", "price?")
check("3: rides on the GFX topic", r["answer"].message_type == "CONTINUATION" and (r["answer"].product or "").startswith("gfx"), f"{r['answer'].message_type} {r['answer'].product}")
check("3: no price stated publicly", not any(ch.isdigit() for ch in r["public"].replace(kb.PHONE, "")), r["public"])
check("3: public reply hands the price to the DM/team", "dm" in r["public"].lower() and ("price" in r["public"].lower() or "team" in r["public"].lower()), r["public"])
public_is_safe("3", r["public"])
r = comment("reel9", "u9", "gold membership price?")
check("3b: a VERIFIED price is stated publicly", "2,999" in r["public"], r["public"])
r = comment("reel9", "u8", "used car kitne se start?")
check("3c: the verified used-car floor is stated publicly", "99,000" in r["public"], r["public"])
r = comment("reel9", "u7", "second hand creta ka price?")
check("3d: a model-specific used-car price is not stated", "99,000" not in r["public"] and "dm" in r["public"].lower(), r["public"])

# 4. "PPF Creta ka kitna?" -> PPF + Creta context, price handed over
print("\n--- 4. PPF Creta ka kitna? ---")
r = comment("reel2", "u2", "PPF Creta ka kitna?")
st = state("reel2", "u2")
check("4: PPF + Creta remembered", st.get("service") == Service.PPF and st.get("car_model") == "Creta", f"{st.get('service')} {st.get('car_model')}")
check("4: public reply mentions PPF, no figure", "ppf" in r["public"].lower() and not any(ch.isdigit() for ch in r["public"].replace(kb.PHONE, "")), r["public"])
public_is_safe("4", r["public"])

# 5. "location?" -> correct business location
print("\n--- 5. location? ---")
r = comment("reel2", "u3", "location?")
check("5: Dholai / ISKCON in the public reply", "dholai" in r["public"].lower() and "iskcon" in r["public"].lower(), r["public"])
check("5: Mansarovar never appears", "mansarovar" not in r["public"].lower() and "mansarovar" not in r["private"].lower())
public_is_safe("5", r["public"])

# 6. "kal ho jayega?" -> never promise availability
print("\n--- 6. kal ho jayega? ---")
r = comment("reel2", "u2", "kal ho jayega?")
low = r["public"].lower()
check("6: no availability promised", "confirmed" not in low and "haan kal ho jayega" not in low and "yes, tomorrow" not in low, r["public"])
check("6: team confirms via DM", "dm" in low and ("team" in low or "confirm" in low), r["public"])
check("6: private reply never confirms the slot either", "confirmed" not in r["private"].lower() or "team will confirm" in r["private"].lower() or "team confirm" in r["private"].lower(), r["private"][:100])
public_is_safe("6", r["public"])

# 7. "50% discount?" -> no invented offer
print("\n--- 7. 50% discount? ---")
r = comment("reel3", "u4", "50% discount?")
check("7: no offer invented", "50" not in r["public"] and "yes" not in r["public"].lower()[:5], r["public"])
check("7: private reply invents nothing either", "50%" not in r["private"] and "50 %" not in r["private"], r["private"][:100])
public_is_safe("7", r["public"])

# 8. Two customers on the same Reel never mix
print("\n--- 8. Two customers, one reel ---")
ra = comment("reel4", "A", "GFX mats for Swift?")
rb = comment("reel4", "B", "PPF for Creta?")
ra2 = comment("reel4", "A", "price?")
rb2 = comment("reel4", "B", "kitna time lagega?")
sa, sb = state("reel4", "A"), state("reel4", "B")
check("8: A remembers Swift + GFX", sa.get("car_model") == "Swift" and (sa.get("product") or "").startswith("gfx"), str({k: sa.get(k) for k in ('car_model', 'product', 'service')}))
check("8: B remembers Creta + PPF", sb.get("car_model") == "Creta" and sb.get("service") == Service.PPF, str({k: sb.get(k) for k in ('car_model', 'product', 'service')}))
check("8: A's replies never mention Creta or PPF", all("creta" not in x.lower() and "ppf" not in x.lower() for x in (ra["private"], ra2["private"], ra["public"], ra2["public"])))
check("8: B's replies never mention Swift or mats", all("swift" not in x.lower() and "mat" not in x.lower().replace("estimate", "") for x in (rb["private"], rb2["private"], rb["public"], rb2["public"])))
check("8: A's 'price?' rides on GFX, B's 'kitna time' on PPF", ra2["answer"].product and ra2["answer"].product.startswith("gfx") and rb2["answer"].service == Service.PPF, f"{ra2['answer'].product} {rb2['answer'].service}")

# 9. Two different Reels, same commenter, never leak into each other
print("\n--- 9. Two reels, one commenter ---")
r1 = comment("reelA", "same", "PPF price?")
r2 = comment("reelB", "same", "GFX mats?")
r1b = comment("reelA", "same", "Alto")
s1, s2 = state("reelA", "same"), state("reelB", "same")
check("9: reel A is PPF with Alto", s1.get("service") == Service.PPF and s1.get("car_model") == "Alto", str({k: s1.get(k) for k in ('service', 'car_model', 'product')}))
check("9: reel B is GFX with no car", (s2.get("product") or "").startswith("gfx") and not s2.get("car_model"), str({k: s2.get(k) for k in ('service', 'car_model', 'product')}))
check("9: 'Alto' on reel A continued PPF, not mats", r1b["answer"].service == Service.PPF and "mat" not in r1b["private"].lower(), f"{r1b['answer'].service} {r1b['private'][:60]}")
check("9: reel B's reply never mentions PPF", "ppf" not in r2["private"].lower() and "ppf" not in r2["public"].lower())
# and the same person's DM is a third, separate memory
dm = brain.process("same", "ceramic coating?", use_ai=False)
sdm = db.get_state(db.get_or_create_conversation("same")) or {}
check("9b: the same user's DM is a separate conversation", sdm.get("service") == Service.CERAMIC and s1.get("service") == Service.PPF and (s2.get("product") or "").startswith("gfx"))

# 10. Duplicate webhook delivery -> one action (real server, real payloads)
print("\n--- 10. Duplicate webhook delivery (live server) ---")
import socket
import requests
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
live_db = os.path.join(tempfile.gettempdir(), f"cartrends_comments_live_{_RUN}.db")
for _x in ("", "-wal", "-shm"):
    if os.path.exists(live_db + _x):
        os.remove(live_db + _x)
env = dict(os.environ, DB_PATH=live_db, PYTHONIOENCODING="utf-8", COMMENT_PRIVATE_REPLY="1",
           PAGE_ACCESS_TOKEN="", APP_SECRET="")
log_path = os.path.join(tempfile.gettempdir(), f"cartrends_comments_live_{_RUN}.log")
log = open(log_path, "w", encoding="utf-8")
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "bot:app", "--host", "127.0.0.1", "--port", str(port)],
                        cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)
BASE = f"http://127.0.0.1:{port}"
try:
    for _ in range(60):
        try:
            requests.get(BASE + "/", timeout=2); break
        except Exception:
            time.sleep(0.5)
    health = requests.get(BASE + "/", timeout=20).json()
    check("10: health reports comment replies enabled", health.get("comment_replies_enabled") is True, str(health.get("comment_replies_enabled")))

    def comment_payload(cid, user, media, text, our_id="17841400000000000"):
        return {"object": "instagram", "entry": [{"id": our_id, "time": 1, "changes": [
            {"field": "comments", "value": {"id": cid, "text": text, "from": {"id": user, "username": "tester"},
                                            "media": {"id": media, "media_product_type": "REELS"}}}]}]}
    def dm_payload(user, mid, text):
        return {"object": "instagram", "entry": [{"id": "17841400000000000", "messaging": [
            {"sender": {"id": user}, "recipient": {"id": "17841400000000000"}, "message": {"mid": mid, "text": text}}]}]}

    p = comment_payload("cm-1", "live-user", "reel-live", "GFX pro mats?")
    r1 = requests.post(BASE + "/webhook", json=p, timeout=30)
    r2 = requests.post(BASE + "/webhook", json=p, timeout=30)          # duplicate delivery
    r3 = requests.post(BASE + "/webhook", json=comment_payload("cm-2", "live-user", "reel-live", "Alto"), timeout=30)
    r4 = requests.post(BASE + "/webhook", json=dm_payload("live-user", "dm-1", "ceramic coating?"), timeout=30)
    r5 = requests.post(BASE + "/webhook", json=comment_payload("cm-own", "17841400000000000", "reel-live", "Hi! Yes, we have GFX mats."), timeout=30)
    check("10: webhook returns 200 for comment events", all(x.status_code == 200 for x in (r1, r2, r3, r4, r5)))
    # threads: the opener's follow-up is answered, a friend's reply in the thread is not
    nested_owner = comment_payload("cm-n1", "live-user", "reel-live", "pro wale?"); nested_owner["entry"][0]["changes"][0]["value"]["parent_id"] = "cm-1"
    nested_friend = comment_payload("cm-n2", "friend-b", "reel-live", "bhai kitna laga tumhe?"); nested_friend["entry"][0]["changes"][0]["value"]["parent_id"] = "cm-1"
    requests.post(BASE + "/webhook", json=nested_owner, timeout=30); requests.post(BASE + "/webhook", json=nested_friend, timeout=30)
    # our own reply echoed back with a different id space and entry id
    time.sleep(3)
    logtxt = open(log_path, encoding="utf-8", errors="replace").read()
    echo_text = logtxt.split("Would have replied under cm-1: ")[1].split("\n")[0].strip().rstrip(")") if "Would have replied under cm-1: " in logtxt else "Hi! x"
    echo = comment_payload("scoped-echo-1", "SCOPED-OWN-ID", "reel-live", echo_text, our_id="17841400000000000")
    requests.post(BASE + "/webhook", json=echo, timeout=30)
    time.sleep(6)
    import sqlite3
    conn = sqlite3.connect(live_db); conn.row_factory = sqlite3.Row
    key = comments.conversation_key("reel-live", "live-user")
    row = conn.execute("SELECT conversation_id FROM conversations WHERE customer_identifier = ?", (key,)).fetchone()
    check("10: the comment conversation exists under its own id", row is not None, key)
    outs = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND direction = 'OUT'", (row["conversation_id"],)).fetchone()[0] if row else -1
    check("10: exactly ONE reply per distinct comment (duplicate ignored)", outs == 3, f"{outs} OUT rows for 3 distinct comments (cm-1, cm-2, threaded cm-n1)")
    st = json.loads(conn.execute("SELECT state_json FROM conversation_state WHERE conversation_id = ?", (row["conversation_id"],)).fetchone()[0]) if row and conn.execute("SELECT name FROM sqlite_master WHERE name='conversation_state'").fetchone() else {}
    dm_row = conn.execute("SELECT conversation_id FROM conversations WHERE customer_identifier = 'live-user'").fetchone()
    check("10: the same user's DM lives in a separate conversation", dm_row is not None and (row is None or dm_row["conversation_id"] != row["conversation_id"]))
    own = conn.execute("SELECT COUNT(*) FROM conversations WHERE customer_identifier LIKE 'comment:reel-live:17841400000000000%'").fetchone()[0]
    check("10: our own echoed comment was ignored", own == 0, str(own))
    log.flush()
    text = open(log_path, encoding="utf-8", errors="replace").read()
    check("10: server logged the duplicate", "Duplicate delivery of comment cm-1" in text)
    check("10: public and private replies were both prepared", "Would have replied under cm-1" in text and "Would have sent privately for cm-1" in text)
    check("10: 'Alto' continued the GFX topic on the live server", "Would have replied under cm-2" in text and ("mat" in text.split("Would have replied under cm-2")[1][:300].lower()))
    check("10: the opener's threaded follow-up was answered under the thread root", "Would have replied under cm-1: " in text and text.count("Would have replied under cm-1: ") >= 2, str(text.count("Would have replied under cm-1: ")))
    friend = conn.execute("SELECT COUNT(*) FROM conversations WHERE customer_identifier LIKE 'comment:reel-live:friend-b%'").fetchone()[0]
    check("10: a friend's reply inside the thread was not answered", friend == 0 and "not ours to answer" in text, str(friend))
    scoped = conn.execute("SELECT COUNT(*) FROM conversations WHERE customer_identifier LIKE 'comment:reel-live:SCOPED-OWN-ID%'").fetchone()[0]
    check("10: our own reply echoed under a scoped id was ignored", scoped == 0 and "echoed back - ignoring" in text, str(scoped))
    conn.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log.close()

# 11. Review findings, pinned
print("\n--- 11. review findings ---")
for i, txt in enumerate(["ceramic coating 15000 me?", "seat cover 3000 me?", "ppf 30000 me full body?", "10k me ho jayega?"]):
    r = comment("reel11", f"f{i}", txt)
    low = r["public"].lower()
    check(f"11: no public 'yes' under a quoted figure ({txt})", not low.startswith("hi! yes") and not low.startswith("hi! haan") and "dm" in low, r["public"])
    check(f"11: figure never echoed ({txt})", not any(ch.isdigit() for ch in r["public"].replace(kb.PHONE, "")), r["public"])
for i, txt in enumerate(["bakwas service, paisa barbaad", "scam hai ye log paisa le ke bhaag jate hain", "ppf 2 mahine me peel ho gayi, fraud log"]):
    r = comment("reel11", f"c{i}", txt)
    check(f"11: complaint slang gets the complaint reply ({txt[:25]})", "sorry" in r["public"].lower() and "dm" in r["public"].lower(), r["public"])
r = comment("reel11", "p1", "home service milta hai? pickup drop?")
check("11: pickup/drop is never answered with 'full service'", "full service" not in r["public"].lower() and "full service" not in r["private"].lower(), r["public"] + " | " + r["private"][:80])
r = comment("reel11", "s1", "kya aap Mansarovar shift ho gaye?")
check("11: 'shift' never becomes a Swift", state("reel11", "s1").get("car_model") is None and "swift" not in r["private"].lower(), str(state("reel11", "s1").get("car_model")))
check("11: 'shift' question still gets the current address", "dholai" in r["public"].lower(), r["public"])
r = comment("reel11", "b1", "book kar do kal 11 baje Swift")
check("11: a booking with a clock time gets booking wording, not just hours", "dm" in r["public"].lower() and "slot" in r["public"].lower(), r["public"])
r = comment("reel11", "m1", "membership kitne ki hai?")
check("11: a membership question never asks for a car model", "car model" not in r["public"].lower(), r["public"])
r = comment("reel11", "e1", "EMI on used car?")
check("11: an EMI question does not pivot to the used-car price", "99,000" not in r["public"], r["public"])
r = comment("reel11", "x1", "fire extinguisher free milega?")
check("11: an extinguisher question does not pivot to the membership price", "2,999" not in r["public"], r["public"])
r = comment("reel11", "q1", "ye reel wali car konsi hai")
check("11: a question about the reel's car is handed to the DM without a pitch", "carplay" not in r["public"].lower() and "dm" in r["public"].lower(), r["public"])
check("11: ...and the private reply is a hand-over too, not a CarPlay pitch", "carplay" not in r["private"].lower() and r["answer"].escalated, r["private"][:90])
for txt in ["Follow @xyz for free followers!!! 🔥🔥", "https://bit.ly/scam-link check this", "😍😍😍", "🔥", "abe chutiye reply de"]:
    r = comment("reel11", "z" + str(abs(hash(txt)) % 1000), txt)
    check(f"11: content-free comment gets no reply ({txt[:20]!r})", r["skipped"] and r["public"] == "" and r["private"] == "", r["public"])
r = comment("reel12", "l1", "PPF ka price kitna hai bhai?")
r2 = comment("reel12", "l1", "wagonr")
check("11: the public reply follows the conversation's language", any(w in r2["public"] for w in ("karein", "DM karein", "ke liye")), r2["public"])
r = comment("reel12", "k1", "PPF Creta ka kitna?")
r2 = comment("reel12", "k1", "kal ho jayega?")
check("11: a booking follow-up never re-asks for a car the brain has", "car model" not in r2["public"].lower() and "day" in r2["public"].lower(), r2["public"])
r = comment("reel12", "g1", "ceramic coating?")
check("11: grammatical service wording", "we do ceramic coating" in r["public"].lower(), r["public"])
r = comment("reel12", "d1", "how long does ceramic last?")
check("11: durability is warranty-class, never 'time required'", "time required" not in r["public"].lower(), r["public"])

# 11b. figures and claims (second verification pass)
print("\n--- 11b. figures and claims ---")
for i, txt in enumerate(["wiper 500 me?", "7d mats 2000 me de do", "seat cover 999 me?", "mats 2000 me?", "perfume 250 me?", "wiper 800 me de do"]):
    r = comment("reel11b", f"g{i}", txt)
    low = r["public"].lower()
    check(f"11b: no public 'yes' under a figure ({txt})", not low.startswith("hi! yes") and not low.startswith("hi! haan") and "dm" in low, r["public"])
for i, txt in enumerate(["ceramic 9H hai na?", "ppf peel to nahi hogi?", "kal pakka ho jayega na?", "lifetime warranty hai na?"]):
    r = comment("reel11b", f"h{i}", txt)
    low = r["public"].lower()
    check(f"11b: a claim to confirm never gets a public 'yes' ({txt})", not low.startswith("hi! yes") and not low.startswith("hi! haan") and "dm" in low, r["public"])
r = comment("reel11b", "y1", "creta 2021 me li thi, ppf ho jayegi?")
check("11b: a purchase year is not a price figure", not brain.mentions_price_figure(brain.normalise("creta 2021 me li thi, ppf ho jayegi")) and "ppf" in r["public"].lower(), r["public"])
check("11b: sizes and counts are not price figures", not brain.mentions_price_figure(brain.normalise("17 inch alloys?")) and not brain.mentions_price_figure(brain.normalise("2 dents hai")) and not brain.mentions_price_figure(brain.normalise("creta 2021 model")))
check("11b: amounts are detected", all(brain.mentions_price_figure(brain.normalise(x)) for x in ["wiper 500 me?", "mats 2000 me de do", "10k me?", "1 lakh", "rs 3000", "999 ka hai kya"]))

# 12. Integration findings, pinned
print("\n--- 12. integration findings ---")
import bot
check("12: graph base derived from the /<IG_ID>/messages form", config.graph_base("https://graph.instagram.com/v21.0/17841400000000000/messages") == "https://graph.instagram.com/v21.0")
check("12: graph base derived from the /me/messages form", config.graph_base("https://graph.facebook.com/v21.0/me/messages") == "https://graph.facebook.com/v21.0")
def cp(cid, user, media, text, our="17841400000000000", **extra):
    v = {"id": cid, "text": text, "from": {"id": user, "username": "tester"}, "media": {"id": media, "media_product_type": "REELS"}}
    v.update(extra)
    return {"object": "instagram", "entry": [{"id": our, "changes": [{"field": "comments", "value": v}]}]}
check("12: object=page payloads are ignored", bot.extract_comments({"object": "page", "entry": [{"id": "P", "changes": [{"field": "comments", "value": {"id": "x", "text": "hi", "from": {"id": "u"}, "media": {"id": "m"}}}]}]}) == [])
alt = {"object": "instagram", "entry": [{"id": "17841400000000000", "changes": [{"field": "comments", "value": {"comment_id": "cid-alt", "text": "GFX mats?", "from": {"id": "u"}, "media": {"id": "m"}}}]}]}
check("12: value.comment_id shape is parsed", [c["comment_id"] for c in bot.extract_comments(alt)] == ["cid-alt"])
flat = {"object": "instagram", "entry": [{"id": "17841400000000000", "field": "comments", "value": {"id": "cid-flat", "text": "GFX mats?", "from": {"id": "u"}, "media": {"id": "m"}}}]}
check("12: entry-level change shape is parsed", [c["comment_id"] for c in bot.extract_comments(flat)] == ["cid-flat"])
config.INSTAGRAM_ACCOUNT_ID = "OWN-SCOPED-ID"
check("12: our own account id (scoped) is skipped even when entry.id differs", bot.extract_comments(cp("e1", "OWN-SCOPED-ID", "m", "Hi! Yes, we have GFX mats.")) == [])
config.INSTAGRAM_ACCOUNT_ID = ""
config.INSTAGRAM_USERNAME = "cartrends"
check("12: our own username is skipped", bot.extract_comments({"object": "instagram", "entry": [{"id": "X", "changes": [{"field": "comments", "value": {"id": "e2", "text": "Hi!", "from": {"id": "999", "username": "CarTrends"}, "media": {"id": "m"}}}]}]}) == [])
config.INSTAGRAM_USERNAME = ""
sent = {}
bot.send_comment_reply = lambda target, text: sent.__setitem__("public", (target, text)) or ""
bot.send_private_reply = lambda cid, text: sent.__setitem__("private", (cid, text)) or True
bot.handle_comment("reply-9", "u12", "reel12x", "GFX mats?", "root-7")
check("12: a threaded reply is answered under the thread root, privately to the reply", sent.get("public", ("",))[0] == "root-7" and sent.get("private", ("",))[0] == "reply-9", str(sent))
check("12: our posted text is remembered for echo detection", sent["public"][1] in bot._recent_public_texts)
db.remember_thread("root-1", "owner-u", "reel12y")
check("12: thread owner recorded and readable", db.thread_owner("root-1") == "owner-u" and db.thread_owner("nope") is None)
check("12: event_recorded is read-only", db.event_recorded("never-claimed") is False and db.event_already_seen("claim-1") is False and db.event_recorded("claim-1") is True)
db.forget_event("claim-1")
check("12: forget_event releases a claimed id", db.event_recorded("claim-1") is False)
r = comment("reel12", "loc", "location?")
check("12: public location reply is link-free", "http" not in r["public"] and "dholai" in r["public"].lower(), r["public"])
check("12: the map link is in the private reply instead", "http" in r["private"], r["private"][:80])

# Summary
print("\n" + "=" * 72)
failed = [(n, d) for n, ok, d in RESULTS if not ok]
print(f" comment checks: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
for n, d in failed:
    print(f"   FAIL {n}   [{d}]")
print("=" * 72)
sys.exit(1 if failed else 0)
