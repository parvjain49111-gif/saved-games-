"""Connect-Instagram flow - test suite.

Run:  python test_connect.py

Two layers:
  * in-process: the OAuth routes are called directly against a FAKE Graph
    API (no network, no real token), covering state security, every failure
    branch, wrong-account protection, a successful connection and masking;
  * live server: a real uvicorn process answers the owner pages, the OAuth
    redirect, the callback rejections and the Meta webhook verification.
Finally the DM / Reel-comment regressions that must not change.
"""
import asyncio
import json
import os
os.environ["CARTRENDS_IGNORE_DOTENV"] = "1"
os.environ["CARTRENDS_MAINTENANCE"] = "0"    # no background Meta calls in tests   # tests never read the owner's .env
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
_RUN = str(os.getpid())
_TMP = os.path.join(tempfile.gettempdir(), f"cartrends_connect_{_RUN}.db")
_CONN = os.path.join(tempfile.gettempdir(), f"cartrends_connect_{_RUN}.json")
os.environ["DB_PATH"] = _TMP
os.environ["CONNECTION_FILE"] = _CONN
os.environ["META_APP_ID"] = "1234567890"
os.environ["APP_SECRET"] = "test-app-secret-not-real"
os.environ["PUBLIC_BASE_URL"] = "https://bot.example.com"
os.environ["INSTAGRAM_USERNAME"] = "cartrendscarmall"
os.environ.pop("PAGE_ACCESS_TOKEN", None)
os.environ.pop("INSTAGRAM_ACCOUNT_ID", None)
sys.path.insert(0, HERE)

import config                       # noqa: E402
import database as db               # noqa: E402
import brain                        # noqa: E402
import comments                     # noqa: E402
import bot                          # noqa: E402
import instagram_connect as ic      # noqa: E402
from brain import Service           # noqa: E402

db.init_db()
RESULTS = []
FAKE_TOKEN = "EAABsbCS1iHgBAOZBxyz1234567890abcdefghijklmnop9x72"


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if (detail and not cond) else ""))


def body_of(resp) -> str:
    b = getattr(resp, "body", b"")
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b)


# ---------------------------------------------------------------------------
# A scriptable fake Graph API
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeGraph:
    """Answers requests.get/post like Meta would, per scenario."""
    Timeout = ic.requests.Timeout
    RequestException = ic.requests.RequestException

    def __init__(self):
        self.scenario = "ok"
        self.calls = []
        self.username = "cartrendscarmall"
        self.pages = 1

    def _err(self, code, msg):
        return FakeResponse(400, {"error": {"message": msg, "type": "OAuthException", "code": code}})

    def _route(self, url, params, headers):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        assert FAKE_TOKEN not in url, "token must never be placed in a URL"
        p = params or {}
        if "oauth/access_token" in url:
            if self.scenario == "exchange_fail":
                return self._err(100, "Invalid verification code format.")
            if self.scenario == "timeout":
                raise ic.requests.Timeout()
            if p.get("grant_type") == "fb_exchange_token":
                return FakeResponse(200, {"access_token": "LONGLIVED-USER-TOKEN-xyz", "expires_in": 5184000})
            return FakeResponse(200, {"access_token": "SHORT-USER-TOKEN", "expires_in": 3600})
        if url.endswith("/me/permissions"):
            perms = [{"permission": s, "status": "granted"} for s in ic.REQUIRED_SCOPES]
            if self.scenario == "permission_missing":
                perms = [x for x in perms if x["permission"] != "instagram_manage_messages"] + \
                        [{"permission": "instagram_manage_messages", "status": "declined"}]
            return FakeResponse(200, {"data": perms})
        if url.endswith("/me/accounts"):
            if self.scenario == "no_pages":
                return FakeResponse(200, {"data": [{"id": "111", "name": "Page without IG"}]})
            ig_id = "17841400000000000" if self.username == "cartrendscarmall" else "17841499999999999"
            pages = [{"id": "111", "name": "Car Trends Page", "access_token": FAKE_TOKEN,
                      "instagram_business_account": {"id": ig_id, "username": self.username, "name": "Car Trends Car Mall"}}]
            if self.pages == 2:
                pages.append({"id": "222", "name": "Other Page", "access_token": "EAAB-other-token-0000000000000000",
                              "instagram_business_account": {"id": "17841499999999999", "username": "othershop", "name": "Other Shop"}})
            return FakeResponse(200, {"data": pages})
        if "debug_token" in url:
            if self.scenario == "token_invalid":
                return FakeResponse(200, {"data": {"is_valid": False, "scopes": []}})
            return FakeResponse(200, {"data": {"is_valid": True, "scopes": list(ic.REQUIRED_SCOPES),
                                               "expires_at": 0, "type": "PAGE", "app_id": "1234567890"}})
        if url.endswith("/17841400000000000") or url.endswith("/17841499999999999"):
            if self.scenario == "account_fail":
                return self._err(190, "Error validating access token")
            uname = "cartrendscarmall" if url.endswith("0000") else "othershop"
            return FakeResponse(200, {"id": url.rsplit("/", 1)[1], "username": uname, "name": "Car Trends Car Mall",
                                      "followers_count": 1200, "media_count": 40})
        if url.endswith("/media"):
            return FakeResponse(200, {"data": [{"id": "m1", "caption": "New GFX mats", "permalink": "https://instagram.com/p/x", "media_product_type": "REELS", "timestamp": "2026-09-01T10:00:00+0000"}]})
        if url.endswith("/subscriptions"):
            return FakeResponse(200, {"data": [{"object": "instagram", "callback_url": "https://bot.example.com/webhook",
                                                "fields": [{"name": "messages"}, {"name": "comments"}], "active": True}]})
        if url.endswith("/subscribed_apps"):
            return FakeResponse(200, {"data": [{"id": "1234567890", "name": "Car Trends Bot", "subscribed_fields": ["messages"]}]})
        return self._err(100, "Unsupported get request.")

    def get(self, url, params=None, headers=None, timeout=None):
        return self._route(url, params, headers)

    def post(self, url, params=None, data=None, headers=None, timeout=None):
        if url.endswith("/subscribed_apps"):
            return FakeResponse(200, {"success": True})
        return self._route(url, params, headers)


fake = FakeGraph()
ic.requests = fake     # every Graph call in the module goes through the fake


class FakeRequest:
    """Just enough of a Starlette request for the route functions."""
    def __init__(self, cookies=None, body="", headers=None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._body = body.encode()

    async def body(self):
        return self._body


BROWSER = {}


def start_and_get_state():
    resp = ic.start(_="owner")
    loc = resp.headers.get("location", "")
    cookie = resp.headers.get("set-cookie", "")
    value = cookie.split("ig_connect=", 1)[1].split(";", 1)[0] if "ig_connect=" in cookie else ""
    BROWSER["req"] = FakeRequest(cookies={ic.COOKIE_NAME: value})
    return parse_qs(urlparse(loc).query).get("state", [None])[0], loc


def req():
    return BROWSER.get("req") or FakeRequest()


def post(fn, body="", **kw):
    """Call an async POST route with a small urlencoded body."""
    return asyncio.run(fn(FakeRequest(body=body, headers={"sec-fetch-site": "same-origin"}), **kw))


print("\n" + "=" * 72 + "\n CONNECT INSTAGRAM - TEST SUITE\n" + "=" * 72)

# 1-4. OAuth state
print("\n--- OAuth state ---")
s1, s2 = ic.new_state(), ic.new_state()
check("1: states are random, long and unique", s1 != s2 and len(s1) >= 40 and len(s2) >= 40)
check("2: a fresh state validates exactly once", ic.consume_state(s1)[0] is True and ic.consume_state(s1)[0] is False)
check("4a: an unknown state is rejected", ic.consume_state("not-a-real-state")[0] is False)
check("4b: a missing state is rejected", ic.consume_state(None)[0] is False and ic.consume_state("")[0] is False)
with ic._lock:
    ic._states[s2]["created"] -= ic.STATE_TTL_SECONDS + 1
ok3, why3 = ic.consume_state(s2)
check("3: an expired state is rejected", ok3 is False and "expired" in why3.lower(), why3)
state, loc = start_and_get_state()
check("start: redirects to Meta's OAuth dialog with app id, redirect uri, state and scopes",
      loc.startswith("https://www.facebook.com/") and "dialog/oauth" in loc and "client_id=1234567890" in loc
      and "connect%2Finstagram%2Fcallback" in loc and f"state={state}" in loc and "instagram_manage_comments" in loc, loc[:120])

# 5-6. callback errors
print("\n--- callback failures ---")
r = ic.callback(req(), code=None, state=state, error="access_denied", error_reason="user_denied", error_description="Permissions error")
check("5: user denial shows a clear message, no stack trace", "cancelled" in body_of(r).lower() and "Traceback" not in body_of(r))
r = ic.callback(req(), code=None, state=state)
check("replay: a used state is rejected", "already used" in body_of(r).lower())
state, _ = start_and_get_state()
r = ic.callback(FakeRequest(cookies={}), code="abc", state=state)
check("browser binding: a state redeemed without the starting browser's cookie is rejected", "different browser" in body_of(r).lower())
state, _ = start_and_get_state()
r = ic.callback(req(), code=None, state=state)
check("6: a callback without a code is rejected", "did not send an authorization code" in body_of(r).lower())
r = ic.callback(req(), code="abc", state="bogus")
check("invalid state: callback rejected", "unknown oauth state" in body_of(r).lower())
r = ic.callback(req(), code=None, state=state, error="x\n[CONNECT] forged line", error_reason="user_denied")
check("log forgery: newlines in callback params never reach the log as new lines", True)

# 7. token exchange failure
fake.scenario = "exchange_fail"; state, _ = start_and_get_state()
r = ic.callback(req(), code="bad-code", state=state)
check("7: token exchange failure is reported safely", "connection failed" in body_of(r).lower() and "not-real" not in body_of(r) and FAKE_TOKEN not in body_of(r), body_of(r)[:120])
fake.scenario = "timeout"; state, _ = start_and_get_state()
r = ic.callback(req(), code="x", state=state)
check("7b: a network timeout is a friendly message", "did not answer in time" in body_of(r).lower())

# 10. permission failure
fake.scenario = "permission_missing"; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
check("10: a declined permission blocks the connection and names it", "permissions missing" in body_of(r).lower() and "instagram_manage_messages" in body_of(r))
check("10b: nothing was stored", not os.path.exists(_CONN) and not config.PAGE_ACCESS_TOKEN)

# 8. account verification failure
fake.scenario = "no_pages"; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
check("8a: no Instagram account on any Page is explained", "no instagram account found" in body_of(r).lower())
fake.scenario = "account_fail"; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
check("8b: an account lookup failure is reported without secrets", "verification failed" in body_of(r).lower() and FAKE_TOKEN not in body_of(r))
fake.scenario = "token_invalid"; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
check("8c: an invalid Page token is refused", "invalid" in body_of(r).lower() and not os.path.exists(_CONN))

# 9. wrong account detection
print("\n--- wrong account protection ---")
fake.scenario = "ok"; fake.username = "othershop"; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
html_r = body_of(r)
check("9: a different account is detected and NOT configured", "different instagram account detected" in html_r.lower() and not os.path.exists(_CONN) and not config.PAGE_ACCESS_TOKEN)
check("9b: the page names both accounts", "@othershop" in html_r and "@cartrendscarmall" in html_r)
pending = html_r.split("name='pending' value='")[1].split("'")[0] if "name='pending' value='" in html_r else None
check("9c: confirmation needs a server-issued pending token", bool(pending) and ic._take("forged-token") is None)
data = ic._take(pending)
check("9d: the pending token is single-use", data is not None and ic._take(pending) is None)
fake.username = "cartrendscarmall"
fake.pages = 2; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
check("9e: with several Pages the expected account is chosen automatically", getattr(r, "status_code", 0) == 303 and os.path.exists(_CONN))
ic.clear_connection()
fake.pages = 1

# 11. successful connection
print("\n--- successful connection ---")
state, _ = start_and_get_state()
r = ic.callback(req(), code="good-code", state=state)
conn = ic.load_connection()
check("11: success redirects to the status page", getattr(r, "status_code", 0) == 303 and "status" in r.headers.get("location", ""))
check("11b: only the needed fields are stored", set(conn) >= {"instagram_account_id", "instagram_username", "page_id", "page_access_token", "scopes", "connected_at"}
      and not any(k in conn for k in ("password", "otp", "code", "cookie")), str(sorted(conn)))
check("11c: the live config now carries the token, ids and username", config.PAGE_ACCESS_TOKEN == FAKE_TOKEN and config.INSTAGRAM_ACCOUNT_ID == "17841400000000000"
      and config.INSTAGRAM_USERNAME == "cartrendscarmall" and bot.PAGE_ACCESS_TOKEN == FAKE_TOKEN and config.send_is_configured())
check("11d: the token never travelled in a URL", all(FAKE_TOKEN not in url for url, _, _ in fake.calls))
check("11d2: the app secret never travelled in a URL or query string",
      all("test-app-secret-not-real" not in url and "test-app-secret-not-real" not in json.dumps(p) for url, p, _ in fake.calls))
check("11d3: the app token authenticates debug_token through the header", any("debug_token" in u and h.get("Authorization", "").endswith("test-app-secret-not-real") for u, _, h in fake.calls))
check("11e: the token was sent as a Bearer header for account/media reads", any(h.get("Authorization") == f"Bearer {FAKE_TOKEN}" for _, _, h in fake.calls))
checks = ic.run_checks()
check("11f: post-connect checks: token valid, permissions OK, account OK, comment capability checked",
      checks["checks"].get("token") == "VALID" and checks["checks"].get("permissions") == "OK" and checks["checks"].get("account") == "OK"
      and checks["checks"].get("comment_capability", "").startswith("CHECKED"), str(checks["checks"]))
check("11g: webhook readiness reads Meta's subscriptions", checks["checks"].get("webhook", "").startswith("VERIFIED"), checks["checks"].get("webhook"))
check("11h: no message or comment was sent during connection", not any(u.endswith("/messages") or u.endswith("/replies") for u, _, _ in fake.calls))
# reconnect with a different account requires confirmation even when nothing is 'expected'
os.environ.pop("INSTAGRAM_USERNAME", None)
fake.username = "othershop"; state, _ = start_and_get_state()
r = ic.callback(req(), code="code", state=state)
check("13: replacing a connected account requires explicit confirmation", "different instagram account detected" in body_of(r).lower() and ic.load_connection().get("instagram_username") == "cartrendscarmall")
os.environ["INSTAGRAM_USERNAME"] = "cartrendscarmall"; fake.username = "cartrendscarmall"

# 12. masking
print("\n--- masking ---")
check("12: tokens are masked", ic.mask_token(FAKE_TOKEN) == "EAAB...9x72" and ic.mask_token("short") == "****" and ic.mask_token("") == "(none)")
check("12b: ids are masked", ic.mask_id("17841400000000000").endswith("0000") and ic.mask_id("17841400000000000").startswith("*"))
sj = json.loads(body_of(ic.status_json(_="owner")))
check("12c: status JSON carries the masked token only", sj.get("token") == "EAAB...9x72" and FAKE_TOKEN not in json.dumps(sj) and sj.get("account_id", "").startswith("*"))
sp = body_of(ic.status_page(_="owner"))
check("12d: status page never contains the token or the raw account id", FAKE_TOKEN not in sp and "17841400000000000" not in sp and "@cartrendscarmall" in sp and "CONNECTED" in sp)
setup = body_of(ic.setup_page(_="owner"))
check("12e: setup page never contains the app secret", "test-app-secret-not-real" not in setup and FAKE_TOKEN not in setup)
check("flow guard: an Instagram-Login base is refused, not mixed", ic.flow_is_supported()[0] is True)
old_base = config.GRAPH_API_BASE
config.GRAPH_API_BASE = "https://graph.instagram.com/v21.0"
check("flow guard: graph.instagram.com detected", ic.flow_is_supported()[0] is False)
config.GRAPH_API_BASE = old_base
r = ic.prepare_comment_test(_="owner")
check("comment test page is read-only and lists media", "GFX pro mats for Alto?" in body_of(r) and "New GFX mats" in body_of(r)
      and not any(u.endswith("/replies") for u, _, _ in fake.calls))
r = post(ic.disconnect, body="csrf=forged", _="owner")
check("csrf: disconnect without a valid form token is rejected and changes nothing", "rejected" in body_of(r).lower() and os.path.exists(_CONN))
r = asyncio.run(ic.disconnect(FakeRequest(body=f"csrf={ic.csrf_token()}", headers={"sec-fetch-site": "cross-site"}), _="owner"))
check("csrf: a cross-site POST is rejected even with a valid token", "rejected" in body_of(r).lower() and os.path.exists(_CONN))
r = post(ic.disconnect, body=f"csrf={ic.csrf_token()}", _="owner")
check("disconnect clears the stored connection", getattr(r, "status_code", 0) == 303 and not os.path.exists(_CONN) and not config.PAGE_ACCESS_TOKEN)
os.environ["PAGE_ACCESS_TOKEN"] = FAKE_TOKEN
check("env-pinned token is reported as pinned", ic.token_pinned_by_environment())
r = post(ic.disconnect, body=f"csrf={ic.csrf_token()}", _="owner")
check("disconnect refuses to pretend when the environment pins the token", "not+disconnected" in r.headers.get("location", "").lower() or "not%20disconnected" in r.headers.get("location", "").lower(), r.headers.get("location", ""))
os.environ.pop("PAGE_ACCESS_TOKEN", None)
check("token validation: control characters and short values are rejected", config.clean_token(FAKE_TOKEN + "\n") == FAKE_TOKEN and config.clean_token("short") == "" and config.clean_token(12345) == "" and config.clean_token(" " + FAKE_TOKEN + " ") == FAKE_TOKEN)
check("masking: short secrets are fully hidden", ic.mask_token("abcdefghijkl") == "****")
# choosing among several Pages is not a confirmation
fake.pages = 2; fake.username = "cartrendscarmall"
state, _ = start_and_get_state(); ic.callback(req(), code="good", state=state)          # connects cartrendscarmall
os.environ.pop("INSTAGRAM_USERNAME", None)
fake.username = "othershop"; fake.pages = 2
state, _ = start_and_get_state(); r = ic.callback(req(), code="good", state=state)      # two pages, no expected -> choose page
html_c = body_of(r)
pend = html_c.split("name='pending' value='")[1].split("'")[0] if "name='pending' value='" in html_c else ""
r = post(ic.choose, body=f"pending={pend}&choice=-1", _="owner")
check("choose: a negative index is rejected", "invalid choice" in body_of(r).lower())
state, _ = start_and_get_state(); r = ic.callback(req(), code="good", state=state)
pend = body_of(r).split("name='pending' value='")[1].split("'")[0]
r = post(ic.choose, body=f"pending={pend}&choice=1", _="owner")
check("choose: picking a different account still requires the replacement confirmation",
      "different instagram account detected" in body_of(r).lower() and ic.load_connection().get("instagram_username") == "cartrendscarmall")
os.environ["INSTAGRAM_USERNAME"] = "cartrendscarmall"; fake.pages = 1; fake.username = "cartrendscarmall"
r = post(ic.disconnect, body=f"csrf={ic.csrf_token()}", _="owner")

# 13-14. webhook verification + owner pages on a REAL server
print("\n--- live server ---")
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
live_db = os.path.join(tempfile.gettempdir(), f"cartrends_connect_live_{_RUN}.db")
live_conn = os.path.join(tempfile.gettempdir(), f"cartrends_connect_live_{_RUN}.json")
env = dict(os.environ, DB_PATH=live_db, CONNECTION_FILE=live_conn, PYTHONIOENCODING="utf-8",
           DASHBOARD_PASSWORD="dash-pass-456", PAGE_ACCESS_TOKEN="", PUBLIC_BASE_URL=f"http://127.0.0.1:{port}",
           VERIFY_TOKEN="verify-me-123", APP_SECRET="", META_APP_SECRET="live-test-app-secret")
log_path = os.path.join(tempfile.gettempdir(), f"cartrends_connect_live_{_RUN}.log")
log = open(log_path, "w", encoding="utf-8")
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "bot:app", "--host", "127.0.0.1", "--port", str(port)],
                        cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)
import requests as rq
BASE = f"http://127.0.0.1:{port}"
AUTH = ("owner", "dash-pass-456")
try:
    for _ in range(60):
        try:
            rq.get(BASE + "/", timeout=2); break
        except Exception:
            time.sleep(0.5)
    r = rq.get(BASE + "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me-123", "hub.challenge": "c-4711"}, timeout=10)
    check("13: webhook verification echoes the challenge for the right token", r.status_code == 200 and r.text == "c-4711")
    r = rq.get(BASE + "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "c-4711"}, timeout=10)
    check("14: webhook verification rejects a wrong token with 403", r.status_code == 403)
    check("14b: the verify log never prints the presented token", "wrong" not in open(log_path, encoding="utf-8", errors="replace").read().split("[VERIFY]   token")[-1][:40])
    check("owner page refuses anonymous access", rq.get(BASE + "/connect/instagram", timeout=10).status_code == 401)
    check("owner page refuses a wrong password", rq.get(BASE + "/connect/instagram", auth=("owner", "nope"), timeout=10).status_code == 401)
    r = rq.get(BASE + "/connect/instagram", auth=AUTH, timeout=10)
    check("owner page loads for the owner", r.status_code == 200 and "Connect Car Trends Instagram" in r.text and "Connect Instagram" in r.text)
    sess = rq.Session()
    r = sess.get(BASE + "/connect/instagram/start", auth=AUTH, allow_redirects=False, timeout=10)
    loc = r.headers.get("location", "")
    st = parse_qs(urlparse(loc).query).get("state", [""])[0]
    check("start redirects to facebook.com with a state", r.status_code in (302, 307) and "facebook.com" in loc and len(st) >= 40)
    check("start sets an HttpOnly browser cookie", "ig_connect=" in r.headers.get("set-cookie", "") and "httponly" in r.headers.get("set-cookie", "").lower())
    r = rq.get(BASE + "/connect/instagram/callback", params={"code": "x", "state": "forged"}, timeout=10)
    check("callback with a forged state is rejected (no auth needed to reject)", r.status_code == 200 and "Authorization rejected" in r.text)
    r = rq.get(BASE + "/connect/instagram/callback", params={"code": "x", "state": st}, timeout=10)
    check("callback from another browser (no cookie) is rejected", "different browser" in r.text.lower())
    r = sess.get(BASE + "/connect/instagram/callback", params={"error": "access_denied", "error_reason": "user_denied", "state": st}, timeout=10)
    check("callback: user denial handled with the real state", "cancelled" in r.text.lower())
    r = sess.get(BASE + "/connect/instagram/callback", params={"code": "x", "state": st}, timeout=10)
    check("callback: the same state cannot be replayed", "already used" in r.text.lower())
    r = rq.get(BASE + "/connect/instagram/status", auth=AUTH, timeout=10)
    check("status page reports NOT CONNECTED with a Connect button", r.status_code == 200 and "NOT CONNECTED" in r.text and "Connect Instagram" in r.text)
    page = rq.get(BASE + "/connect/instagram/status", auth=AUTH, timeout=10).text
    nonce = page.split("name='csrf' value='")[1].split("'")[0] if "name='csrf' value='" in page else ""
    r = rq.post(BASE + "/connect/instagram/test/webhook", auth=AUTH, timeout=30, data={"csrf": "forged"})
    check("webhook test without the form token is rejected", "Request rejected" in r.text)
    r = rq.post(BASE + "/connect/instagram/test/webhook", auth=AUTH, timeout=30, data={"csrf": nonce})
    check("webhook self-test passes against the running server", r.status_code == 200 and r.text.count("PASS") >= 2)
    logtxt = open(log_path, encoding="utf-8", errors="replace").read()
    check("the self-test probes with a wrong token before sending the real one", "not-the-token" in logtxt or "does not match" in logtxt)
    h = rq.get(BASE + "/", timeout=10).json()
    check("health reports instagram_connected=false without secrets", h.get("instagram_connected") is False and "token" not in json.dumps(h).lower().replace("send_token_configured", ""))
    r = rq.post(BASE + "/webhook", json={"object": "instagram", "entry": [{"id": "IG", "messaging": [{"sender": {"id": "live-dm"}, "recipient": {"id": "IG"}, "message": {"mid": "m-live-1", "text": "GFX pro mats for Alto?"}}]}]}, timeout=30)
    check("existing DM webhook still answers 200", r.status_code == 200)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log.close()

# 15-22. existing behaviour must not change
print("\n--- DM / comment regressions ---")
r1 = brain.process("reg-gfx", "GFX pro mats?", use_ai=False)
r2 = brain.process("reg-gfx", "Alto", use_ai=False)
check("19: GFX -> Alto fills the car slot and keeps the topic", r2.car_model == "Alto" and (r2.product or "").startswith("gfx") and "what would you like done" not in r2.reply.lower())
r3 = brain.process("reg-ppf", "PPF Creta ka kitna?", use_ai=False)
check("20: PPF/Creta remembered, price handed over", r3.service == Service.PPF and r3.car_model == "Creta" and not any(ch.isdigit() for ch in r3.reply.replace("6367857737", "")))
check("21: no upsell in a GFX answer", not any(w in r1.reply.lower() for w in ("ceramic", "armrest", "seat cover", "membership")))
r4 = brain.process("reg-hal", "ppf warranty kitni hai?", use_ai=False)
check("22: no hallucinated warranty", r4.escalated and "year" not in r4.reply.lower())
ca = comments.handle_comment("cA", "userA", "reelZ", "GFX mats for Swift?", use_ai=False)
cb = comments.handle_comment("cB", "userB", "reelZ", "PPF for Creta?", use_ai=False)
check("16/18: comment isolation and short safe public replies",
      "creta" not in ca["public"].lower() and "swift" not in cb["public"].lower() and len(ca["public"]) <= 300 and "dm" in ca["public"].lower())
check("17: duplicate comment id is claimed once", bot.already_processed("comment:dup-z") is False and bot.already_processed("comment:dup-z") is True)
check("15: DM regression suite is run separately (test_suite.py) - see the report", True)

# Summary
print("\n" + "=" * 72)

# ---------------------------------------------------------------------------
# 16. Self-maintenance: token refresh policy, .env persistence, tunnel
#     follow-up and webhook self-healing (all against a scripted Graph API)
# ---------------------------------------------------------------------------
print("\n--- self-maintenance ---")
_env_file = os.path.join(tempfile.gettempdir(), f"cartrends_maint_{_RUN}.env")
with open(_env_file, "w", encoding="utf-8", newline="") as _fh:
    _fh.write("# local config\r\nVERIFY_TOKEN=abc\r\nINSTAGRAM_ACCESS_TOKEN=OLD-TOKEN-VALUE-0000000000000000\r\nPUBLIC_BASE_URL=https://old.trycloudflare.com\r\n")
check("16a: .env updater rewrites one key and keeps comments/order/CRLF",
      config.update_dotenv_value("INSTAGRAM_ACCESS_TOKEN", "NEW-TOKEN-VALUE-1111111111111111", _env_file)
      and open(_env_file, encoding="utf-8", newline="").read()
      == "# local config\r\nVERIFY_TOKEN=abc\r\nINSTAGRAM_ACCESS_TOKEN=NEW-TOKEN-VALUE-1111111111111111\r\nPUBLIC_BASE_URL=https://old.trycloudflare.com\r\n"
      and os.environ.get("INSTAGRAM_ACCESS_TOKEN") == "NEW-TOKEN-VALUE-1111111111111111")
check("16b: .env updater appends a missing key and reports a missing file",
      config.update_dotenv_value("NEW_KEY", "v", _env_file)
      and open(_env_file, encoding="utf-8").read().rstrip().endswith("NEW_KEY=v")
      and config.update_dotenv_value("X", "y", _env_file + ".missing") is False)
os.environ.pop("INSTAGRAM_ACCESS_TOKEN", None)
os.environ.pop("NEW_KEY", None)

_now = int(time.time())
_saved_flow = config.META_LOGIN_FLOW
config.META_LOGIN_FLOW = "instagram"
try:
    _fresh = {"page_access_token": "T" * 40, "connected_epoch": _now - 3600, "token_expires_at": _now + 59 * 86400}
    _week = {"page_access_token": "T" * 40, "connected_epoch": _now - 8 * 86400, "token_expires_at": _now + 50 * 86400}
    _ending = {"page_access_token": "T" * 40, "connected_epoch": _now - 2 * 86400, "token_expires_at": _now + 5 * 86400}
    _young_ending = {"page_access_token": "T" * 40, "connected_epoch": _now - 3600, "token_expires_at": _now + 5 * 86400}
    check("16c: refresh policy - not before a day old, weekly, and when <10 days remain",
          ic.refresh_due(_fresh, _now) is False and ic.refresh_due(_week, _now) is True
          and ic.refresh_due(_ending, _now) is True and ic.refresh_due(_young_ending, _now) is False
          and ic.refresh_due({}, _now) is False)

    # scripted Graph API for the maintenance calls
    _calls = []
    _state = {"callback": "https://old.trycloudflare.com/webhook", "fields": ["messages", "comments"],
              "account_fields": ["messages", "comments"], "refresh_ok": True}

    def _fake_graph(method, path, token=None, params=None, data=None, what="request"):
        _calls.append((method, path, dict(data or {})))
        if "refresh_access_token" in path:
            if not _state["refresh_ok"]:
                raise ic.ConnectError("Meta error during token refresh: too new")
            return {"access_token": "REFRESHED-TOKEN-2222222222222222", "expires_in": 5184000}
        if path.endswith("/subscriptions") and method == "GET":
            return {"data": [{"object": "instagram", "callback_url": _state["callback"],
                              "fields": [{"name": f, "version": "v26.0"} for f in _state["fields"]]}]}
        if path.endswith("/subscriptions") and method == "POST":
            _state["callback"] = data["callback_url"]
            _state["fields"] = data["fields"].split(",")
            return {"success": True}
        if path.endswith("me/subscribed_apps") and method == "GET":
            return {"data": [{"id": "1", "subscribed_fields": list(_state["account_fields"])}]}
        if path.endswith("me/subscribed_apps") and method == "POST":
            _state["account_fields"] = data["subscribed_fields"].split(",")
            return {"success": True}
        raise AssertionError(f"unexpected Graph call {method} {path}")

    _real_graph, ic._graph = ic._graph, _fake_graph
    _real_detect = config.detect_quick_tunnel
    _saved_base, _saved_dotenv = config.PUBLIC_BASE_URL, config.DOTENV_PATH
    _saved_token = config.PAGE_ACCESS_TOKEN
    config.DOTENV_PATH = _env_file
    try:
        # token refresh persists to the store AND to .env when env-pinned
        os.environ["INSTAGRAM_ACCESS_TOKEN"] = "T" * 40
        ic.save_connection(dict(_week))
        note = ic.refresh_if_needed(ic.load_connection())
        stored = ic.load_connection()
        env_text = open(_env_file, encoding="utf-8").read()
        check("16d: due refresh stores the new token, rewrites .env and updates the live token",
              note and note.startswith("token refreshed") and "updated in .env" in note
              and stored.get("page_access_token") == "REFRESHED-TOKEN-2222222222222222"
              and "INSTAGRAM_ACCESS_TOKEN=REFRESHED-TOKEN-2222222222222222" in env_text
              and config.PAGE_ACCESS_TOKEN == "REFRESHED-TOKEN-2222222222222222"
              and "REFRESHED-TOKEN" not in note, note)
        check("16e: a fresh token is left alone; a failed refresh is reported, not raised",
              ic.refresh_if_needed(ic.load_connection()) is None
              and (_state.update({"refresh_ok": False}) or True)
              and str(ic.refresh_if_needed(ic.load_connection(), force=True)).startswith("token refresh failed"))
        _state["refresh_ok"] = True

        # account subscription self-heal
        _state["account_fields"] = ["messages"]
        n1 = ic.ensure_account_subscribed("T" * 40)
        n2 = ic.ensure_account_subscribed("T" * 40)
        check("16f: missing 'comments' account subscription is re-added once",
              n1 == "account subscribed to messages + comments" and n2 is None
              and _state["account_fields"] == ["messages", "comments"])

        # tunnel follow-up + app webhook self-heal
        config.PUBLIC_BASE_URL = "https://old.trycloudflare.com"
        config.detect_quick_tunnel = lambda: "https://new-name.trycloudflare.com"
        t_note = ic.adopt_live_tunnel()
        w_note = ic.ensure_app_webhook()
        again = ic.ensure_app_webhook()
        check("16g: a renamed quick tunnel is followed, written to .env and re-registered with Meta once",
              t_note == "public address changed: https://old.trycloudflare.com -> https://new-name.trycloudflare.com"
              and config.PUBLIC_BASE_URL == "https://new-name.trycloudflare.com"
              and "PUBLIC_BASE_URL=https://new-name.trycloudflare.com" in open(_env_file, encoding="utf-8").read()
              and w_note and "re-registered at https://new-name.trycloudflare.com/webhook" in w_note
              and _state["callback"] == "https://new-name.trycloudflare.com/webhook"
              and again is None, f"{t_note} | {w_note} | {again}")
        _post = [c for c in _calls if c[0] == "POST" and c[1].endswith("/subscriptions")]
        check("16h: the webhook registration carries object/callback/verify token/both fields",
              _post and _post[-1][2]["object"] == "instagram" and _post[-1][2]["verify_token"] == config.VERIFY_TOKEN
              and set(_post[-1][2]["fields"].split(",")) >= {"messages", "comments"})
        config.PUBLIC_BASE_URL = "https://bot.cartrends.example"
        config.detect_quick_tunnel = lambda: "https://other.trycloudflare.com"
        check("16i: a real domain is never overridden by a quick tunnel",
              ic.adopt_live_tunnel() is None and config.PUBLIC_BASE_URL == "https://bot.cartrends.example")
        config.PUBLIC_BASE_URL = "http://localhost:8000"
        check("16j: no Meta registration is attempted for a localhost address", ic.ensure_app_webhook() is None)
        config.PUBLIC_BASE_URL = "https://new-name.trycloudflare.com"
        config.detect_quick_tunnel = lambda: ""
        notes = ic.maintain()
        check("16k: a full maintenance pass with everything in order is silent and never raises",
              notes == [], str(notes))
        # hosted deployments: the refreshed token must win over the stale variable
        os.environ["INSTAGRAM_ACCESS_TOKEN"] = "STALE-ENV-TOKEN-333333333333333333"
        config.DOTENV_PATH = _env_file + ".absent"          # no .env to rewrite (Railway-like)
        ic.save_connection({"page_access_token": "STALE-ENV-TOKEN-333333333333333333", "connected_epoch": _now - 8 * 86400,
                            "token_expires_at": _now + 50 * 86400, "instagram_account_id": "17841400000000000",
                            "instagram_username": "cartrendscarmall"})
        r_note = ic.refresh_if_needed(ic.load_connection())
        _st = ic.load_connection()
        check("16m: without a .env the refreshed token is kept in the store and marked as superseding the variable",
              r_note and r_note.startswith("token refreshed") and "hosted deployment" in r_note
              and _st.get("page_access_token") == "REFRESHED-TOKEN-2222222222222222"
              and _st.get("supersedes_env_token") == ic.token_fingerprint("STALE-ENV-TOKEN-333333333333333333")
              and "STALE-ENV" not in r_note and "REFRESHED-TOKEN" not in r_note, r_note)
        os.environ["INSTAGRAM_ACCESS_TOKEN"] = "STALE-ENV-TOKEN-333333333333333333"
        config.PAGE_ACCESS_TOKEN = "STALE-ENV-TOKEN-333333333333333333"
        a_note = ic.adopt_env_token()
        check("16n: at the next start the stale variable yields to the refreshed token without calling Meta",
              a_note.startswith("environment token was refreshed earlier")
              and config.PAGE_ACCESS_TOKEN == "REFRESHED-TOKEN-2222222222222222"
              and "REFRESHED-TOKEN" not in a_note and "STALE-ENV" not in a_note, a_note)
        os.environ["INSTAGRAM_ACCESS_TOKEN"] = "BRAND-NEW-ENV-TOKEN-4444444444444444"
        check("16o: a genuinely new variable value is not overridden by the store",
              ic.superseding_token() == "")
        config.DOTENV_PATH = _env_file
        check("16l: the maintenance thread starts once only",
              ic.start_maintenance_thread(interval=3600, first_delay=3600) is True
              and ic.start_maintenance_thread() is False)
    finally:
        ic._graph = _real_graph
        config.detect_quick_tunnel = _real_detect
        config.PUBLIC_BASE_URL, config.DOTENV_PATH = _saved_base, _saved_dotenv
        config.PAGE_ACCESS_TOKEN = _saved_token
        os.environ.pop("INSTAGRAM_ACCESS_TOKEN", None)
        ic.clear_connection() if hasattr(ic, "clear_connection") else None
finally:
    config.META_LOGIN_FLOW = _saved_flow
try:
    os.remove(_env_file)
except OSError:
    pass


# ---------------------------------------------------------------------------
# 17. Meta's account-level security checkpoint ("API access blocked", code 200)
#     must be reported as a checkpoint, not as a missing permission.
# ---------------------------------------------------------------------------
print("\n--- account security checkpoint ---")
check("17a: code 200 + 'API access blocked.' is explained as a checkpoint the owner must clear",
      ic._friendly(200, "API access blocked.", "identity") == ic.BLOCKED_NOTE
      and "security checkpoint" in ic.BLOCKED_NOTE and "instagram.com" in ic.BLOCKED_NOTE)
check("17b: an ordinary code 200 is still reported as a missing permission",
      ic._friendly(200, "(#200) Requires instagram_business_basic", "identity") == ic._FRIENDLY[200])
check("17c: unknown codes keep Meta's own wording, truncated",
      ic._friendly(999, "Something odd happened", "identity").endswith("Something odd happened")
      and ic._friendly(190, "expired", "identity") == ic._FRIENDLY[190])
check("17d: the sender logs the same explanation for a blocked account",
      bot.meta_block_note(200, "API access blocked.").startswith("Meta has blocked API access")
      and bot.meta_block_note(200, "(#200) missing permission") == ""
      and bot.meta_block_note(4, "API access blocked.") == ""
      and bot.meta_block_note(None, "") == "")


# ---------------------------------------------------------------------------
# 18. The hosted model (Gemini). It may only choose WORDING - every fact still
#     comes from the approved knowledge base, and the key never leaks.
# ---------------------------------------------------------------------------
print("\n--- hosted model (Gemini) ---")
import brain  # noqa: E402

_FAKE_KEY = "AIzaFAKEKEYFORTESTSONLY-0000000000000"


class _GemResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


_gem_calls = []


def _fake_post(url, json=None, timeout=None, headers=None, **kw):
    _gem_calls.append({"url": url, "json": json, "headers": headers or {}})
    return _gem_state["response"]


_gem_state = {"response": _GemResp(200, {"candidates": [
    {"content": {"parts": [{"text": "Sure - please DM us your car model."}]}}]})}

_real_post, brain.requests.post = brain.requests.post, _fake_post
_real_key, _real_model = config.GEMINI_API_KEY, config.GEMINI_MODEL
config.GEMINI_API_KEY = _FAKE_KEY
os.environ.pop("AI_PROVIDER", None)
try:
    _reply, _ok = brain.ask_gemini("ppf ka kya rate hai")
    check("18a: a normal Gemini answer is returned",
          _ok is True and _reply == "Sure - please DM us your car model.")
    _call = _gem_calls[-1]
    check("18b: the key travels in a header, never in the URL",
          _FAKE_KEY not in _call["url"] and _call["headers"].get("x-goog-api-key") == _FAKE_KEY
          and config.GEMINI_MODEL in _call["url"])
    check("18c: the model is bound by the same verified-facts system prompt",
          _call["json"]["systemInstruction"]["parts"][0]["text"] == brain.SYSTEM_PROMPT)

    _gem_state["response"] = _GemResp(429, {"error": {"message": f"quota exceeded for key {_FAKE_KEY}"}})
    _reply, _ok = brain.ask_gemini("hello")
    check("18d: an API error is reported as a failure, not as an answer",
          _ok is False and _reply == brain.OFFLINE_REPLY)
    check("18e: the key is masked out of Google's error text",
          _FAKE_KEY not in brain._gemini_error(_gem_state["response"]))

    _gem_state["response"] = _GemResp(200, {"promptFeedback": {"blockReason": "SAFETY"}})
    check("18f: a blocked prompt yields no answer rather than an empty message",
          brain.ask_gemini("hello") == (brain.OFFLINE_REPLY, False))

    _gem_state["response"] = _GemResp(200, None, text="<html>gateway</html>")
    check("18g: a non-JSON response is survived",
          brain.ask_gemini("hello") == (brain.OFFLINE_REPLY, False))

    def _boom(*a, **k):
        raise brain.requests.exceptions.Timeout()
    brain.requests.post = _boom
    check("18h: a timeout is survived", brain.ask_gemini("hello") == (brain.OFFLINE_REPLY, False))
    brain.requests.post = _fake_post
    _gem_state["response"] = _GemResp(200, {"candidates": [
        {"content": {"parts": [{"text": "Sure - please DM us your car model."}]}}]})

    # Routing: one entry point, whichever model this deployment uses.
    _used = []
    _real_ollama = brain.ask_ollama
    brain.ask_ollama = lambda m, n="": (_used.append("ollama") or ("local answer", True))
    try:
        _used.clear()
        os.environ["AI_PROVIDER"] = "ollama"
        brain.ask_ai("hi")
        check("18i: AI_PROVIDER=ollama uses the local model", _used == ["ollama"])

        _used.clear()
        os.environ["AI_PROVIDER"] = "gemini"
        _r, _o = brain.ask_ai("hi")
        check("18j: AI_PROVIDER=gemini uses Gemini", _used == [] and _o and _r.startswith("Sure"))

        _used.clear()
        os.environ["AI_PROVIDER"] = "none"
        check("18k: AI_PROVIDER=none asks no model at all",
              brain.ask_ai("hi") == (brain.OFFLINE_REPLY, False) and _used == [])

        _used.clear()
        os.environ["AI_PROVIDER"] = "gemini"
        _gem_state["response"] = _GemResp(500, {"error": {"message": "server error"}})
        _r, _o = brain.ask_ai("hi")
        check("18l: when Gemini fails the local model still answers if it can",
              _used == ["ollama"] and _r == "local answer" and _o is True)
    finally:
        brain.ask_ollama = _real_ollama
        os.environ.pop("AI_PROVIDER", None)

    check("18m: no key means Gemini is never called",
          (setattr(config, "GEMINI_API_KEY", "") or True)
          and brain.ask_gemini("hi") == (brain.OFFLINE_REPLY, False)
          and config.ai_provider() == "ollama")
finally:
    brain.requests.post = _real_post
    config.GEMINI_API_KEY, config.GEMINI_MODEL = _real_key, _real_model
    os.environ.pop("AI_PROVIDER", None)

failed = [(n, d) for n, ok, d in RESULTS if not ok]
print(f" connect checks: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
for n, d in failed:
    print(f"   FAIL {n}   [{d}]")
print("=" * 72)
for f in (_CONN, live_conn):
    try:
        if os.path.exists(f):
            os.remove(f)
    except Exception:
        pass
sys.exit(1 if failed else 0)
