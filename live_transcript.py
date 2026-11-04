"""
=============================================================================
 LIVE TRANSCRIPT - what customers wrote and what was actually sent back
=============================================================================

     python live_transcript.py --days 7 --out transcript.txt

 The weekly gap report shows the questions. This shows the conversations:
 every Reel comment thread and DM of the window, both sides, in order, read
 from Meta. It is how a wrong reply is found - the report can only say
 whether a question was answerable, not whether the answer that went out
 was right.

 A reply is marked BOT when it came from our account, and (other tool) when
 it is one of the sentences the second auto-reply app on the account sends
 - those are not ours and cannot be fixed here. Nothing is sent; nothing is
 written to the live database; the token is never printed.
=============================================================================
"""
import argparse
import datetime
import io
import os
import sys
from typing import Any, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import weekly_gaps as wg

# Sentences the OTHER auto-reply tool on the account sends. Grep the repo
# before adding one here: if a sentence is in our code, it is ours.
OTHER_TOOL = ("jai jinendra", "just checking if you saw my message",
              "thanks for messaging car trends car mall!",
              "car trends car mall, opposite iskcon temple, kharbas cir rd")


def _who(username: str, text: str, us: str) -> str:
    if not username:
        return "REPLY"                     # Meta hides most usernames on replies
    if username != us:
        return "CUST"
    low = (text or "").lower()
    return "BOT (other tool)" if any(s in low for s in OTHER_TOOL) else "BOT"


def collect(days: int) -> List[str]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    us = wg.our_username()
    out: List[str] = []

    # ---- comment threads: each top-level comment with its replies ------
    out.append("=" * 78)
    out.append("REEL COMMENTS")
    out.append("=" * 78)
    media = wg._paged("me/media", limit=50, pages=4, fields="id,caption,timestamp")
    for item in media:
        comments = wg._paged(f"{item['id']}/comments", limit=50, pages=3,
                             fields="id,text,timestamp,username,"
                                    "replies{id,text,timestamp,username}")
        fresh = [c for c in comments
                 if (wg._when(c, "timestamp") or cutoff) >= cutoff
                 and (c.get("username") or "") != us]
        if not fresh:
            continue
        cap = " ".join((item.get("caption") or "").split())[:90]
        out.append("")
        out.append(f"--- Reel {item['id'][-8:]} | caption: {cap!r}")
        for c in sorted(fresh, key=lambda c: wg._when(c, "timestamp") or cutoff):
            when = wg._when(c, "timestamp")
            out.append(f"  {when:%d %b %H:%M} CUST {c.get('username') or '?'}: "
                       f"{' '.join((c.get('text') or '').split())}")
            replies = ((c.get("replies") or {}).get("data") or [])
            for r in sorted(replies, key=lambda r: wg._when(r, "timestamp") or cutoff):
                rw = wg._when(r, "timestamp")
                tag = _who(r.get("username") or "", r.get("text") or "", us)
                out.append(f"      {rw:%d %b %H:%M} {tag}: "
                           f"{' '.join((r.get('text') or '').split())}")

    # ---- DMs ---------------------------------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("DMS")
    out.append("=" * 78)
    threads = wg._paged("me/conversations", limit=50, pages=6,
                        fields="participants,messages.limit(40){id,created_time,from,message}")
    for thread in threads:
        msgs = [m for m in ((thread.get("messages") or {}).get("data") or [])
                if wg._when(m, "created_time")]
        msgs.sort(key=lambda m: wg._when(m, "created_time"))
        recent = [m for m in msgs if wg._when(m, "created_time") >= cutoff]
        if not any((m.get("from") or {}).get("username") != us for m in recent):
            continue                       # nothing from the customer this window
        who = [p.get("username") for p in (thread.get("participants") or {}).get("data") or []
               if p.get("username") != us]
        out.append("")
        out.append(f"--- DM with {who[0] if who else '?'}")
        for m in recent:
            when = wg._when(m, "created_time")
            text = " ".join((m.get("message") or "(photo / reel share)").split())
            tag = _who((m.get("from") or {}).get("username") or "", text, us)
            out.append(f"  {when:%d %b %H:%M} {tag}: {text}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    lines = collect(args.days)
    text = "\n".join(lines)
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(text + "\n")
        print(f"{len(lines)} lines -> {os.path.abspath(args.out)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
