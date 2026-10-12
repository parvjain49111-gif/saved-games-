"""
=============================================================================
 WEEKLY GAP REPORT - what customers asked that we could not answer
=============================================================================

     python weekly_gaps.py              the last 7 days
     python weekly_gaps.py --days 14    a longer window
     python weekly_gaps.py --out x.md   also write the report to a file

 WHY THIS EXISTS
 ---------------
 Owner, 12 Oct 2026: "the comments are just asking the data - PPF mat for
 alto 2024 - so we have to put the data in it, and I don't know which data
 I have to put. At the end of the week analyse all the comments and give me
 which data the comments are asking for."

 Every Reel comment and every DM of the window is read from Meta and run
 back through the brain. A question is counted as UNANSWERED when the reply
 carries no fact of its own - when it only hands the question to the team.
 Those are grouped by the data that would have answered them, with the
 customers' own words as evidence, and the car models they asked about.

 Nothing is written to the live database and no reply is ever sent: this
 reads the history and reports. The access token is read from the normal
 configuration and never printed.
=============================================================================
"""
import argparse
import collections
import datetime
import io
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

import brain
import config
import knowledge as kb

GRAPH_TIMEOUT = 30


# ---------------------------------------------------------------------------
# READING THE HISTORY FROM META
# ---------------------------------------------------------------------------
def _get(path_or_url: str, **params) -> Tuple[int, Dict[str, Any]]:
    """One Graph call. Returns (status, body); never raises on HTTP errors."""
    url = (path_or_url if path_or_url.startswith("http")
           else f"{config.GRAPH_API_BASE}/{path_or_url}")
    params.setdefault("access_token", config.PAGE_ACCESS_TOKEN)
    try:
        r = requests.get(url, params=params, timeout=GRAPH_TIMEOUT)
    except Exception as error:
        return 0, {"error": {"message": type(error).__name__}}
    try:
        return r.status_code, r.json() or {}
    except Exception:
        return r.status_code, {}


def _paged(path: str, limit: int = 50, pages: int = 10, **params) -> List[Dict[str, Any]]:
    """Follow `paging.next` a few times and return every row."""
    code, body = _get(path, limit=limit, **params)
    if code != 200:
        print(f"[GAPS] {path} -> HTTP {code}: "
              f"{(body.get('error') or {}).get('message', '')[:120]}")
        return []
    rows = list(body.get("data") or [])
    nxt = (body.get("paging") or {}).get("next")
    while nxt and pages > 1:
        code, body = _get(nxt)
        if code != 200:
            break
        rows.extend(body.get("data") or [])
        nxt = (body.get("paging") or {}).get("next")
        pages -= 1
    return rows


def _when(row: Dict[str, Any], *keys: str) -> Optional[datetime.datetime]:
    for key in keys:
        raw = row.get(key)
        if not raw:
            continue
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S+0000"):
            try:
                return datetime.datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def our_username() -> str:
    code, body = _get("me", fields="username")
    return (body.get("username") or config.INSTAGRAM_USERNAME or "") if code == 200 else ""


def collect_questions(days: int) -> List[Dict[str, Any]]:
    """Every customer message of the window, from Reel comments and DMs.

    Each row: {where, text, when, who, media_id, caption}. Our own replies,
    empty messages and photo-only messages are left out.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    us = our_username()
    rows: List[Dict[str, Any]] = []

    # ---- Reel and post comments -----------------------------------------
    media = _paged("me/media", limit=50, pages=4,
                   fields="id,caption,timestamp,permalink")
    for item in media:
        comments = _paged(f"{item['id']}/comments", limit=50, pages=3,
                          fields="id,text,timestamp,username")
        for c in comments:
            when = _when(c, "timestamp")
            if not when or when < cutoff:
                continue
            if (c.get("username") or "") == us:
                continue                       # our own public reply
            text = " ".join((c.get("text") or "").split())
            if not text:
                continue
            rows.append({"where": "comment", "text": text, "when": when,
                         "id": c.get("id") or "", "who": c.get("username") or "?",
                         "media_id": item["id"],
                         "caption": " ".join((item.get("caption") or "").split())[:160]})

    # ---- DMs -------------------------------------------------------------
    threads = _paged("me/conversations", limit=50, pages=6,
                     fields="participants,messages.limit(40){id,created_time,from,message}")
    for thread in threads:
        for m in ((thread.get("messages") or {}).get("data") or []):
            when = _when(m, "created_time")
            if not when or when < cutoff:
                continue
            if ((m.get("from") or {}).get("username") or "") == us:
                continue
            text = " ".join((m.get("message") or "").split())
            if not text:
                continue
            rows.append({"where": "dm", "text": text, "when": when,
                         "id": m.get("id") or "", "who": (m.get("from") or {}).get("username") or "?",
                         "media_id": "", "caption": ""})

    rows.sort(key=lambda r: r["when"])
    return rows


# ---------------------------------------------------------------------------
# WHAT DATA WOULD HAVE ANSWERED IT
# ---------------------------------------------------------------------------
# A question we could not answer is useless to the owner as "PRICE_INQUIRY".
# It is useful as "the price of GFX Pro mats, asked for 14 different cars".
_INTENT_LABEL = {
    kb.Intent.PRICE_INQUIRY: "price",
    kb.Intent.WARRANTY: "warranty",
    kb.Intent.DURATION: "how long it takes",
    kb.Intent.AVAILABILITY_REQUEST: "whether we have it",
    kb.Intent.OFFER_DISCOUNT: "current offers",
    kb.Intent.PICKUP_DROP: "pick-up and drop",
    kb.Intent.MEMBERSHIP_INQUIRY: "membership",
    kb.Intent.USED_CAR_INQUIRY: "used cars in stock",
    kb.Intent.RECOMMENDATION: "what to recommend",
    kb.Intent.SERVICE_COMPARISON: "which is better",
}


def thing_asked_about(a) -> str:
    """The product or service the question was about, in the owner's words."""
    if a.product and a.product in kb.PRODUCTS:
        return kb.PRODUCTS[a.product]["label"]
    if a.service:
        return kb.SERVICE_LABELS.get(a.service, str(a.service).replace("_", " ").lower())
    return "general"


def data_needed(a, text: str) -> Optional[str]:
    """The missing fact, as a line the owner can answer. None when answered.

    "Answered" means the reply carried a fact of its own. A reply that only
    says the team will confirm has told the customer nothing - that is the
    gap, whatever the bot's own status flags say.
    """
    if brain.reply_says_something(a.reply) and not a.gap_topic:
        return None
    if a.gap_topic and "Delivery" in a.gap_topic:
        return "delivery / courier outside Jaipur"
    what = _INTENT_LABEL.get(a.intent)
    thing = thing_asked_about(a)
    if thing == "general" and a.car_model and not what:
        return ("they named only their car - the Reel's caption did not say "
                "what it is selling")
    if what:
        return f"{what} of {thing}"
    if thing != "general":
        return f"details of {thing}"
    return "could not tell what they wanted"


_LINK_ONLY = ("http://", "https://", "www.")


def _is_link_only(text: str) -> bool:
    """A shared Reel or video and nothing else is not a question."""
    words = [w for w in text.split() if w]
    return bool(words) and all(w.lower().startswith(_LINK_ONLY) for w in words)


def _conversation_key(row: Dict[str, Any]) -> str:
    """The same grouping the live bot uses: one thread per person per Reel."""
    if row["where"] == "comment":
        if row["who"] == "?":                  # the export hides some names;
            return f"c:{row['id']}"            # never merge two strangers
        return f"c:{row['media_id']}:{row['who']}"
    return f"d:{row['who']}" if row["who"] != "?" else f"d:{row['id']}"


def analyse(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Replay the week exactly as the bot lived it, and bucket the gaps.

    Messages are replayed IN CONVERSATION, on a scratch database, with the
    Reel's own subject seeded in - so "Price" and "Victoris" carry the topic
    they carried live instead of looking like questions about nothing.
    """
    import tempfile
    import config as _cfg
    import database as _db
    import comments as _comments

    old_path = _cfg.DB_PATH
    scratch = os.path.join(tempfile.gettempdir(), "cartrends_weekly_gaps.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(scratch + suffix)
        except OSError:
            pass
    _db.close_connection()
    _cfg.DB_PATH = scratch

    buckets: Dict[str, Dict[str, Any]] = {}
    answered = unanswered = 0
    models = collections.Counter()
    try:
        _db.init_db()
        topics: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if _is_link_only(row["text"]):
                continue
            key = _conversation_key(row)
            if row["where"] == "comment" and row["caption"]:
                hint = topics.get(row["media_id"])
                if hint is None:
                    seen = brain.perceive(row["caption"])
                    service = (None if seen.service in brain.dialogue.SIDE_SERVICES
                               else seen.service)
                    hint = {"caption": row["caption"], "service": service,
                            "product": seen.product}
                    topics[row["media_id"]] = hint
                if hint.get("service") or hint.get("product"):
                    _comments.seed_topic_from_reel(
                        key, hint, brain.perceive(row["text"]))
            try:
                a = brain.process(key, row["text"], use_ai=False)
            except Exception as error:           # never lose the whole report
                print(f"[GAPS] {row['text'][:40]!r} raised {error!r}")
                continue
            if a.car_model:
                models[a.car_model] += 1
            gap = data_needed(a, row["text"])
            if gap is None:
                answered += 1
                continue
            unanswered += 1
            slot = buckets.setdefault(gap, {"count": 0, "examples": [],
                                            "models": collections.Counter(),
                                            "comment": 0, "dm": 0})
            slot["count"] += 1
            slot[row["where"]] += 1
            if a.car_model:
                slot["models"][a.car_model] += 1
            if len(slot["examples"]) < 4:
                slot["examples"].append(f"{row['who']}: {row['text'][:90]}")
    finally:
        _db.close_connection()
        _cfg.DB_PATH = old_path
    return {"buckets": buckets, "answered": answered, "unanswered": unanswered,
            "models": models, "total": answered + unanswered}


# ---------------------------------------------------------------------------
# THE REPORT
# ---------------------------------------------------------------------------
def render(result: Dict[str, Any], days: int) -> str:
    out: List[str] = []
    total = result["total"]
    answered, unanswered = result["answered"], result["unanswered"]
    share = round(answered / total * 100) if total else 0
    today = datetime.date.today().strftime("%d %b %Y")
    out.append(f"# What customers asked in the last {days} days  ({today})")
    out.append("")
    out.append(f"- {total} questions came in (Reel comments + DMs)")
    out.append(f"- {answered} got a real answer ({share}%)")
    out.append(f"- {unanswered} got only \"our team will confirm\" - "
               "these are the ones your data would fix")
    out.append("")
    if not result["buckets"]:
        out.append("Nothing to add this week: every question had an answer.")
        return "\n".join(out)

    out.append("## The data to give me, most asked first")
    out.append("")
    ordered = sorted(result["buckets"].items(), key=lambda kv: -kv[1]["count"])
    for i, (gap, slot) in enumerate(ordered, 1):
        cars = ", ".join(f"{m} ({n})" for m, n in slot["models"].most_common(6))
        out.append(f"### {i}. {gap} - asked {slot['count']} times "
                   f"({slot['comment']} in comments, {slot['dm']} in DMs)")
        if cars:
            out.append(f"Cars asked about: {cars}")
        out.append("")
        for example in slot["examples"]:
            out.append(f"> {example}")
        out.append("")

    out.append("## Cars named this week")
    out.append("")
    out.append(", ".join(f"{m} ({n})" for m, n in result["models"].most_common(20))
               or "none")
    out.append("")
    out.append("## Still waiting from before")
    out.append("")
    waiting = [f["question"] for f in kb.MISSING_INFO_FAQS][:40]
    out.append(f"{len(kb.MISSING_INFO_FAQS)} approved questions have no answer yet:")
    for q in waiting:
        out.append(f"- {q}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not config.PAGE_ACCESS_TOKEN:
        print("No access token configured - nothing to read. "
              "Set it in the environment, not here.")
        return 1

    rows = collect_questions(args.days)
    if not rows:
        print("No customer questions found in that window.")
        return 0
    report = render(analyse(rows), args.days)
    print(report)
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(report + "\n")
        print(f"\n[GAPS] written to {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
