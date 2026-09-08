"""
=============================================================================
 Car Trends Car Mall - CONNECT INSTAGRAM (safe OAuth setup flow)
=============================================================================

 What this is
 ------------
 A small, owner-only setup layer that connects the REAL Instagram account to
 the existing bot without anyone ever typing a password, OTP or 2FA code into
 this project. The owner logs in on Meta's own pages; Meta sends back an
 authorization code; this module exchanges it server-side for the Page token
 the existing bot already uses (config.PAGE_ACCESS_TOKEN).

 Flow (Facebook Login for Business - the flow this project is built on:
 GRAPH_API_URL = https://graph.facebook.com/<ver>/me/messages with a Page token)

     owner  -> GET /connect/instagram            (owner-only page, "Connect" button)
            -> GET /connect/instagram/start      (random state, redirect to Meta)
     Meta   -> owner logs in, approves permissions (password/OTP stay with Meta)
     Meta   -> GET /connect/instagram/callback?code=...&state=...
     bot    -> validates state (single use, 10 min), exchanges code -> user token
               -> long-lived user token -> Pages with an Instagram account
               -> verifies permissions and the account identity
               -> if the account is not the expected one: asks for confirmation
               -> saves ONLY: page token, page id, IG id, username, scopes
               -> re-verifies (read-only) and shows the status page

 Nothing here sends a message or posts a comment. The real-customer test is a
 separate, explicitly confirmed action, and the first DM test needs a person
 to message the account first (Meta's messaging window).

 Endpoints (all owner-only via the dashboard's HTTP Basic auth, except the
 callback which is protected by the one-time OAuth state):
     GET  /connect/instagram                  setup page
     GET  /connect/instagram/start            begin OAuth (redirect to Meta)
     GET  /connect/instagram/callback         Meta redirects here
     POST /connect/instagram/choose           pick one of several Pages/accounts
     POST /connect/instagram/confirm          confirm a different-than-expected account
     GET  /connect/instagram/status           status page   (JSON: /status.json)
     POST /connect/instagram/test/connection  re-run the read-only checks
     POST /connect/instagram/test/webhook     self-test GET /webhook + Meta subscription info
     POST /connect/instagram/webhook/subscribe   subscribe the app to the Page (explicit)
     GET  /connect/instagram/test/comment     prepare the Reel comment test (read-only)
     GET  /connect/instagram/test/dm          prepare the DM test
     POST /connect/instagram/test/dm/send     send ONE confirmed test reply
     POST /connect/instagram/disconnect       forget the stored connection

 CLI:
     python instagram_connect.py status       what is configured / missing
     python instagram_connect.py url          the URL to open to connect
=============================================================================
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import config
import database as db
from dashboard import require_owner

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

router = APIRouter(prefix="/connect/instagram", tags=["connect"])

# Permissions the bot genuinely needs, per Meta login flow.
REQUIRED_SCOPES_FB: Tuple[str, ...] = (
    "instagram_basic",
    "instagram_manage_comments",
    "instagram_manage_messages",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_metadata",
)
REQUIRED_SCOPES_IG: Tuple[str, ...] = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
)
REQUIRED_SCOPES: Tuple[str, ...] = REQUIRED_SCOPES_FB     # kept for older callers


def flow() -> str:
    """'instagram' (API setup with Instagram login) or 'facebook'."""
    return "instagram" if config.META_LOGIN_FLOW == "instagram" else "facebook"


def required_scopes() -> Tuple[str, ...]:
    return REQUIRED_SCOPES_IG if flow() == "instagram" else REQUIRED_SCOPES_FB


_IG_API = "https://api.instagram.com"
_IG_GRAPH = "https://graph.instagram.com"


def _ig(path: str) -> str:
    return f"{_IG_GRAPH}/{config.GRAPH_API_VERSION}/{path.lstrip('/')}"
STATE_TTL_SECONDS = 600            # an authorization must finish within 10 minutes
_states: Dict[str, Dict[str, Any]] = {}
_pending: Dict[str, Dict[str, Any]] = {}   # results waiting for the owner's choice/confirmation
_lock = threading.Lock()

_FB = "https://www.facebook.com"


# ---------------------------------------------------------------------------
# SECRET HYGIENE
# ---------------------------------------------------------------------------
def mask_token(token: Optional[str]) -> str:
    """'EAABxyz...9x72' - enough to recognise a token, never enough to use it."""
    if not token:
        return "(none)"
    if len(token) < 20:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


def mask_id(value: Optional[str]) -> str:
    if not value:
        return "(none)"
    return "*" * max(0, len(value) - 4) + value[-4:]


def _log(msg: str) -> None:
    # one line, printable characters only: query values cannot forge log lines
    msg = "".join(ch if ch.isprintable() else " " for ch in str(msg))
    print(f"[CONNECT] {msg[:400]}")


# ---------------------------------------------------------------------------
# GRAPH HELPERS (every Meta error becomes a short, safe message)
# ---------------------------------------------------------------------------
class ConnectError(Exception):
    """A user-facing failure; the message never contains a secret."""


_FRIENDLY = {
    190: "The access token is invalid or expired. Please connect again.",
    10: "Meta refused the request: a required permission is missing (App Review / Advanced Access may be needed).",
    200: "Meta refused the request: a required permission is missing.",
    100: "Meta rejected the request parameters (wrong account, API version or field).",
    4: "Meta rate limit reached - wait a few minutes and try again.",
    17: "Meta rate limit reached - wait a few minutes and try again.",
    32: "Meta rate limit reached - wait a few minutes and try again.",
    613: "Meta rate limit reached - wait a few minutes and try again.",
}


def _graph(method: str, path: str, token: Optional[str] = None,
           params: Optional[Dict[str, Any]] = None, data: Optional[Dict[str, Any]] = None,
           what: str = "request") -> Dict[str, Any]:
    """GET/POST the Graph API. Tokens travel in an Authorization header, never
    in the URL, so they cannot land in access logs."""
    url = path if path.startswith("http") else f"{config.GRAPH_API_BASE}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=config.GRAPH_TIMEOUT)
        else:
            r = requests.post(url, params=params, data=data, headers=headers, timeout=config.GRAPH_TIMEOUT)
    except requests.Timeout:
        raise ConnectError(f"Meta did not answer in time ({what}). Check the internet connection and try again.")
    except requests.RequestException as error:
        raise ConnectError(f"Could not reach Meta ({what}): {type(error).__name__}.")
    try:
        body = r.json() if r.text else {}
    except ValueError:
        body = {}
    if r.status_code != 200 or "error" in body:
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        code = err.get("code")
        message = err.get("message") or f"HTTP {r.status_code}"
        _log(f"{what} failed: HTTP {r.status_code} code={code} type={err.get('type')} "
             f"subcode={err.get('error_subcode')} message={message[:160]}")
        raise ConnectError(_FRIENDLY.get(code, f"Meta error during {what}: {message[:160]}"))
    return body if isinstance(body, dict) else {"data": body}


def _app_token() -> str:
    return f"{config.META_APP_ID}|{config.META_APP_SECRET}"


def _s(value) -> str:
    """Meta may send null for a name: never let that reach html.escape."""
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# CONNECTION STORE  (only what the bot needs; never a password, OTP or cookie)
# ---------------------------------------------------------------------------
def load_connection() -> Dict[str, Any]:
    path = config.CONNECTION_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as error:
        _log(f"could not read the connection file: {type(error).__name__}")
        return {}


_save_lock = threading.Lock()


def save_connection(conn: Dict[str, Any]) -> None:
    """Write the file privately and atomically (0600 on POSIX; on Windows the
    file inherits the user-profile folder's ACL - keep the project under the
    user's profile, never in a shared folder)."""
    path = config.CONNECTION_FILE
    folder = os.path.dirname(os.path.abspath(path)) or "."
    with _save_lock:
        fd, tmp = tempfile.mkstemp(prefix=".instagram_connection.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(conn, fh, indent=2)
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    apply_connection(conn)


def apply_connection(conn: Dict[str, Any]) -> None:
    """Make the stored connection the live one for this process. Environment
    variables, when set, still win - they are the deployment's decision."""
    token = config.clean_token(conn.get("page_access_token"))
    if not os.getenv("PAGE_ACCESS_TOKEN") and token:
        config.PAGE_ACCESS_TOKEN = token
    if not os.getenv("INSTAGRAM_ACCOUNT_ID") and conn.get("instagram_account_id"):
        config.INSTAGRAM_ACCOUNT_ID = str(conn["instagram_account_id"])
    if not os.getenv("INSTAGRAM_USERNAME") and conn.get("instagram_username"):
        config.INSTAGRAM_USERNAME = str(conn["instagram_username"]).lstrip("@").lower()
    config.PAGE_ID = os.getenv("PAGE_ID") or str(conn.get("page_id") or config.PAGE_ID or "")
    try:                                   # bot.py caches the token at import
        import bot
        bot.PAGE_ACCESS_TOKEN = config.PAGE_ACCESS_TOKEN
    except Exception:
        pass


def clear_connection() -> None:
    try:
        if os.path.exists(config.CONNECTION_FILE):
            os.remove(config.CONNECTION_FILE)
    except Exception as error:
        _log(f"could not delete the connection file: {type(error).__name__}")
    if not os.getenv("PAGE_ACCESS_TOKEN"):
        config.PAGE_ACCESS_TOKEN = ""
        try:
            import bot
            bot.PAGE_ACCESS_TOKEN = ""
        except Exception:
            pass
    if not os.getenv("INSTAGRAM_ACCOUNT_ID"):
        config.INSTAGRAM_ACCOUNT_ID = ""
    if not os.getenv("INSTAGRAM_USERNAME"):
        config.INSTAGRAM_USERNAME = ""
    config.PAGE_ID = os.getenv("PAGE_ID", "")


def current_token() -> str:
    return config.PAGE_ACCESS_TOKEN


def token_pinned_by_environment() -> bool:
    """True when the deployment sets the token itself: the stored connection
    then cannot change what the bot sends with."""
    return bool(config.clean_token(os.getenv("PAGE_ACCESS_TOKEN", "") or os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
                                   or os.getenv("ACCESS_TOKEN", "")))


def adopt_env_token() -> str:
    """A token supplied through the environment (generated in the Meta
    dashboard) is verified against Meta and the account identity is filled in
    so the echo guard and the status page know whose token it is. Returns a
    one-line note for the startup log; never raises."""
    token = current_token()
    if not token or not token_pinned_by_environment():
        return ""
    if config.INSTAGRAM_ACCOUNT_ID and config.INSTAGRAM_USERNAME:
        return f"token from environment for @{config.INSTAGRAM_USERNAME}"
    try:
        me = account_identity(config.INSTAGRAM_ACCOUNT_ID or "me", token)
    except ConnectError as error:
        return f"token from environment could not be verified: {error}"
    ig_id = str(me.get("id") or "")
    uname = _s(me.get("username")).lstrip("@").lower()
    if ig_id and not os.getenv("INSTAGRAM_ACCOUNT_ID"):
        config.INSTAGRAM_ACCOUNT_ID = ig_id
    if uname and not os.getenv("INSTAGRAM_USERNAME"):
        config.INSTAGRAM_USERNAME = uname
    exp = expected_username()
    if exp and uname and uname != exp:
        return f"WARNING: the environment token belongs to @{uname}, expected @{exp}"
    return f"token from environment verified for @{uname or '?'} (id {mask_id(ig_id)})"


# ---------------------------------------------------------------------------
# OAUTH STATE  (random, expiring, single-use, bound to nothing the browser sends)
# ---------------------------------------------------------------------------
COOKIE_NAME = "ig_connect"


def new_state(browser_secret: str = "") -> str:
    """A fresh state, optionally tied to a browser secret (an HttpOnly cookie
    set at /start) so a leaked state cannot be redeemed from elsewhere."""
    state = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for k in [k for k, v in _states.items() if now - v["created"] > STATE_TTL_SECONDS]:
            _states.pop(k, None)
        _states[state] = {"created": now, "used": False,
                          "browser": hashlib.sha256(browser_secret.encode()).hexdigest() if browser_secret else ""}
    return state


def consume_state(state: Optional[str], browser_secret: str = "") -> Tuple[bool, str]:
    """True once and only once for a fresh, unexpired state we issued to
    this browser."""
    if not state:
        return False, "Missing OAuth state."
    with _lock:
        entry = _states.get(state)
        if entry is None:
            return False, "Unknown OAuth state (the setup page was not used, or the server restarted). Start again from the setup page."
        if entry["used"]:
            return False, "This authorization was already used. Start again from the setup page."
        if time.time() - entry["created"] > STATE_TTL_SECONDS:
            _states.pop(state, None)
            return False, "The authorization took longer than 10 minutes and expired. Start again."
        if entry["browser"] and not hmac.compare_digest(
                entry["browser"], hashlib.sha256(browser_secret.encode()).hexdigest()):
            return False, "This authorization was started from a different browser. Start again from the setup page."
        entry["used"] = True
    return True, ""


def _stash(payload: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > STATE_TTL_SECONDS]:
            _pending.pop(k, None)
        _pending[token] = {"created": now, "data": payload}
    return token


def _take(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _lock:
        entry = _pending.pop(token, None)
    if entry is None or time.time() - entry["created"] > STATE_TTL_SECONDS:
        return None
    return entry["data"]


# ---------------------------------------------------------------------------
# THE FLOW
# ---------------------------------------------------------------------------
def redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL.rstrip('/')}/connect/instagram/callback"


def flow_is_supported() -> Tuple[bool, str]:
    host = config.GRAPH_API_BASE.split("//", 1)[-1].split("/", 1)[0]
    if flow() == "instagram":
        if not (config.INSTAGRAM_APP_ID and config.INSTAGRAM_APP_SECRET):
            return False, ("Instagram-login flow: set INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET (Meta App "
                           "Dashboard > Instagram > API setup with Instagram login) in the local .env file.")
        if host != "graph.instagram.com":
            return False, (f"Instagram-login flow needs GRAPH_API_URL on graph.instagram.com (it is {host}). "
                           "Remove the GRAPH_API_URL override or set META_LOGIN_FLOW=facebook.")
        return True, ""
    if host == "graph.instagram.com":
        return False, ("GRAPH_API_URL points at graph.instagram.com but META_LOGIN_FLOW is facebook. "
                       "Set INSTAGRAM_APP_ID/INSTAGRAM_APP_SECRET (Instagram login) or use the Page-token setup.")
    if not (config.META_APP_ID and config.META_APP_SECRET):
        return False, "Facebook-login flow: set META_APP_ID and APP_SECRET."
    return True, ""


def authorization_url(state: str) -> str:
    if flow() == "instagram":
        params = {
            "client_id": config.INSTAGRAM_APP_ID,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": ",".join(REQUIRED_SCOPES_IG),
            "state": state,
            "enable_fb_login": "0",
            "force_authentication": "1",
        }
        return f"https://www.instagram.com/oauth/authorize?{urlencode(params)}"
    params = {
        "client_id": config.META_APP_ID,
        "redirect_uri": redirect_uri(),
        "state": state,
        "response_type": "code",
    }
    if config.META_LOGIN_CONFIG_ID:
        # Facebook Login for Business: the configuration carries the
        # permissions; Meta asks not to send "scope" alongside it.
        params["config_id"] = config.META_LOGIN_CONFIG_ID
        params["override_default_response_type"] = "true"
    else:
        scopes = list(REQUIRED_SCOPES_FB) + [s for s in config.META_EXTRA_SCOPES.split(",") if s.strip()]
        params["scope"] = ",".join(dict.fromkeys(s.strip() for s in scopes))
    return f"{_FB}/{config.GRAPH_API_VERSION}/dialog/oauth?{urlencode(params)}"


def exchange_code(code: str) -> Dict[str, Any]:
    """Authorization code -> long-lived token. Returns {"token", "permissions",
    "user_id", "expires_in"}; permissions/user_id are only known for the
    Instagram-login flow (they arrive with the token)."""
    code = code.split("#", 1)[0].strip()          # Instagram appends '#_'
    if flow() == "instagram":
        short = _graph("POST", f"{_IG_API}/oauth/access_token", data={
            "client_id": config.INSTAGRAM_APP_ID, "client_secret": config.INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri(), "code": code},
            what="code exchange")
        token = short.get("access_token")
        if not token:
            raise ConnectError("Instagram did not return an access token for the authorization code.")
        perms = short.get("permissions") or []
        if isinstance(perms, str):
            perms = [x.strip() for x in perms.split(",") if x.strip()]
        # 1-hour token -> 60-day token. Meta's endpoint takes the secret as a
        # query parameter; there is no header form for this call.
        longlived = _graph("GET", f"{_IG_GRAPH}/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": config.INSTAGRAM_APP_SECRET,
            "access_token": token}, what="long-lived token exchange")
        return {"token": longlived.get("access_token") or token, "permissions": perms,
                "user_id": str(short.get("user_id") or ""), "expires_in": int(longlived.get("expires_in") or 0)}
    # Secrets go in the POST body, never in a query string that a proxy or
    # access log could keep.
    short = _graph("POST", "oauth/access_token", data={
        "client_id": config.META_APP_ID, "client_secret": config.META_APP_SECRET,
        "redirect_uri": redirect_uri(), "code": code}, what="code exchange")
    token = short.get("access_token")
    if not token:
        raise ConnectError("Meta did not return an access token for the authorization code.")
    longlived = _graph("POST", "oauth/access_token", data={
        "grant_type": "fb_exchange_token", "client_id": config.META_APP_ID,
        "client_secret": config.META_APP_SECRET, "fb_exchange_token": token},
        what="long-lived token exchange")
    return {"token": longlived.get("access_token") or token, "permissions": None,
            "user_id": "", "expires_in": int(longlived.get("expires_in") or 0)}


def granted_permissions(user_token: str, known: Optional[List[str]] = None) -> Dict[str, str]:
    if known is not None:                       # Instagram login: sent with the token
        return {p: "granted" for p in known}
    data = _graph("GET", "me/permissions", token=user_token, what="permission check").get("data") or []
    return {p.get("permission"): p.get("status") for p in data if isinstance(p, dict)}


def instagram_pages(user_token: str, exchange: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Candidates to connect. Facebook login: every Page with an Instagram
    professional account. Instagram login: the one account that logged in."""
    if flow() == "instagram":
        me = _graph("GET", _ig("me"), token=user_token, params={
            "fields": "id,user_id,username,name,account_type"}, what="account lookup")
        ex = exchange or {}
        return [{"page_id": "", "page_name": "Instagram login (no Page needed)",
                 "page_access_token": user_token,
                 "instagram_account_id": str(me.get("user_id") or ex.get("user_id") or me.get("id") or ""),
                 "app_scoped_id": str(me.get("id") or ""),
                 "instagram_username": _s(me.get("username")).lstrip("@").lower(),
                 "instagram_name": _s(me.get("name")), "account_type": _s(me.get("account_type")),
                 "scopes": list(ex.get("permissions") or []),
                 "expires_at": int(time.time()) + int(ex.get("expires_in") or 0) if ex.get("expires_in") else 0}]
    body = _graph("GET", "me/accounts", token=user_token, params={
        "fields": "id,name,access_token,instagram_business_account{id,username,name}",
        "limit": 100}, what="page lookup")
    pages = []
    for page in body.get("data") or []:
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            pages.append({"page_id": str(page["id"]), "page_name": _s(page.get("name")),
                          "page_access_token": _s(page.get("access_token")),
                          "instagram_account_id": str(ig["id"]),
                          "instagram_username": _s(ig.get("username")).lstrip("@").lower(),
                          "instagram_name": _s(ig.get("name"))})
    return pages


def verify_token(page_token: str, page: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if flow() == "instagram":
        # graph.instagram.com has no debug_token for this flow: a successful
        # /me proves validity; scopes and expiry travelled with the token.
        conn = page if page is not None else load_connection()
        try:
            me = _graph("GET", _ig("me"), token=page_token, params={"fields": "id,username"},
                        what="token verification")
            valid = bool(me.get("id"))
        except ConnectError:
            valid = False
        return {"is_valid": valid, "scopes": list(conn.get("scopes") or []),
                "expires_at": int(conn.get("expires_at") or conn.get("token_expires_at") or 0),
                "type": "IG_USER", "app_id": ""}
    # input_token is the one value Meta requires as a query parameter; the
    # app token authenticates through the header.
    body = _graph("GET", "debug_token", token=_app_token(), params={"input_token": page_token},
                  what="token verification")
    d = body.get("data") or {}
    return {"is_valid": bool(d.get("is_valid")), "scopes": d.get("scopes") or [],
            "expires_at": d.get("expires_at", 0), "type": d.get("type", ""),
            "app_id": str(d.get("app_id", ""))}


def account_identity(ig_id: str, page_token: str) -> Dict[str, Any]:
    if flow() == "instagram":
        me = _graph("GET", _ig("me"), token=page_token, params={
            "fields": "id,user_id,username,name,account_type,followers_count,media_count"},
            what="account lookup")
        me["id"] = str(me.get("user_id") or me.get("id") or ig_id)
        return me
    return _graph("GET", ig_id, token=page_token, params={
        "fields": "id,username,name,followers_count,media_count"}, what="account lookup")


def refresh_if_needed(conn: Dict[str, Any]) -> Optional[str]:
    """Instagram-login tokens last 60 days and can be refreshed once they are
    a day old. Refresh when fewer than 10 days remain; returns a note."""
    if flow() != "instagram" or not conn.get("page_access_token"):
        return None
    exp = int(conn.get("token_expires_at") or 0)
    if not exp:
        return None
    age_ok = time.time() - (conn.get("connected_epoch") or 0) > 86400
    if exp - time.time() > 10 * 86400 or not age_ok:
        return None
    try:
        res = _graph("GET", f"{_IG_GRAPH}/refresh_access_token", token=conn["page_access_token"],
                     params={"grant_type": "ig_refresh_token"}, what="token refresh")
    except ConnectError as error:
        return f"token refresh failed: {error}"
    if res.get("access_token"):
        conn["page_access_token"] = res["access_token"]
        conn["token_expires_at"] = int(time.time()) + int(res.get("expires_in") or 0)
        conn["connected_epoch"] = int(time.time())
        save_connection(conn)
        return "token refreshed"
    return None


def missing_scopes(granted: List[str]) -> List[str]:
    return [s for s in required_scopes() if s not in granted]


def expected_username() -> str:
    return (os.getenv("INSTAGRAM_USERNAME") or "").lstrip("@").lower()


# ---------------------------------------------------------------------------
# READ-ONLY VERIFICATION (never sends, never posts)
# ---------------------------------------------------------------------------
def run_checks(conn: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    conn = conn if conn is not None else load_connection()
    token = current_token()
    result: Dict[str, Any] = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "connected": bool(token), "checks": {}, "errors": []}
    if not token:
        result["checks"]["token"] = "MISSING"
        return result
    note = refresh_if_needed(conn)
    if note:
        result["errors"].append(note)
        token = current_token()
    try:
        info = verify_token(token, conn if flow() == "instagram" else None)
        result["checks"]["token"] = "VALID" if info["is_valid"] else "INVALID"
        result["scopes"] = info["scopes"]
        result["token_expires_at"] = info["expires_at"]
        if config.META_APP_ID and info.get("app_id") and info["app_id"] != config.META_APP_ID:
            result["errors"].append("The token belongs to a different Meta app than META_APP_ID.")
        miss = missing_scopes(info["scopes"])
        result["checks"]["permissions"] = "OK" if not miss else "MISSING: " + ", ".join(miss)
        c_scope = "instagram_business_manage_comments" if flow() == "instagram" else "instagram_manage_comments"
        m_scope = "instagram_business_manage_messages" if flow() == "instagram" else "instagram_manage_messages"
        result["checks"]["comments_permission"] = "OK" if c_scope in info["scopes"] else "MISSING"
        result["checks"]["messaging_permission"] = "OK" if m_scope in info["scopes"] else "MISSING"
    except ConnectError as error:
        result["checks"]["token"] = "ERROR"
        result["errors"].append(str(error))
        return result
    ig_id = config.INSTAGRAM_ACCOUNT_ID or str(conn.get("instagram_account_id") or "")
    if ig_id:
        try:
            me = account_identity(ig_id, token)
            result["username"] = str(me.get("username") or "").lstrip("@").lower()
            result["account_id"] = str(me.get("id") or ig_id)
            result["checks"]["account"] = "OK"
            exp = expected_username()
            if exp and result["username"] != exp:
                result["checks"]["account"] = f"DIFFERENT (@{result['username']}, expected @{exp})"
        except ConnectError as error:
            result["checks"]["account"] = "ERROR"
            result["errors"].append(str(error))
    else:
        result["checks"]["account"] = "MISSING (no Instagram account id)"
    # Comment capability: a read of the account's media proves the comments
    # permission actually works for this account.
    if ig_id and result["checks"].get("comments_permission") == "OK":
        try:
            _graph("GET", _ig("me/media") if flow() == "instagram" else f"{ig_id}/media",
                   token=token, params={"fields": "id", "limit": 1}, what="media read")
            result["checks"]["comment_capability"] = "CHECKED (media readable)"
        except ConnectError as error:
            result["checks"]["comment_capability"] = "ERROR"
            result["errors"].append(str(error))
    else:
        result["checks"]["comment_capability"] = "NOT CHECKED"
    result["checks"]["messaging_capability"] = (
        "CHECKED (permission granted; a real DM test needs a person to message the account first)"
        if result["checks"].get("messaging_permission") == "OK" else "NOT CHECKED")
    # Webhook readiness on Meta's side (app-level subscription + page subscription).
    result["checks"]["webhook"] = webhook_readiness(conn, token)
    return result


def webhook_readiness(conn: Dict[str, Any], token: str) -> str:
    notes = []
    if config.META_APP_ID and config.META_APP_SECRET:
        try:
            subs = _graph("GET", f"https://graph.facebook.com/{config.GRAPH_API_VERSION}/{config.META_APP_ID}/subscriptions",
                          token=_app_token(), what="app subscriptions").get("data") or []
            ig = [x for x in subs if x.get("object") == "instagram"]
            if ig:
                fields = [f.get("name") if isinstance(f, dict) else f for f in (ig[0].get("fields") or [])]
                cb = ig[0].get("callback_url", "")
                ours = cb.rstrip("/").endswith("/webhook") and (config.PUBLIC_BASE_URL.rstrip("/") in cb)
                notes.append(f"app subscribed to instagram fields {fields} at {cb}"
                             + ("" if ours else " (NOT this server's PUBLIC_BASE_URL)"))
                for need in ("messages", "comments"):
                    if need not in fields:
                        notes.append(f"field '{need}' not subscribed in the App Dashboard")
            else:
                notes.append("no 'instagram' webhook subscription found in the Meta app (App Dashboard > Webhooks)")
        except ConnectError as error:
            notes.append(f"app subscription lookup failed: {error}")
    else:
        notes.append("META_APP_ID/APP_SECRET missing - cannot read the app's webhook subscriptions")
    if flow() == "instagram":
        if token:
            try:
                apps = _graph("GET", _ig("me/subscribed_apps"), token=token,
                              what="account subscription lookup").get("data") or []
                fields = sorted({f for a in apps for f in (a.get("subscribed_fields") or [])})
                if apps and "messages" in fields and "comments" in fields:
                    notes.append("account subscribed to messages + comments")
                elif apps:
                    notes.append(f"account subscribed to {fields} only - use 'Subscribe webhook'")
                else:
                    notes.append("account NOT subscribed (use 'Subscribe webhook' below)")
            except ConnectError as error:
                notes.append(f"account subscription lookup failed: {error}")
    else:
        page_id = config.PAGE_ID or str(conn.get("page_id") or "")
        if page_id and token:
            try:
                apps = _graph("GET", f"{page_id}/subscribed_apps", token=token,
                              what="page subscription lookup").get("data") or []
                mine = [a for a in apps if str(a.get("id")) == config.META_APP_ID] if config.META_APP_ID else apps
                notes.append("app is subscribed to the Page" if mine else
                             "app NOT subscribed to the Page (use 'Subscribe webhook' below)")
            except ConnectError as error:
                notes.append(f"page subscription lookup failed: {error}")
    ok = notes and not any(("NOT" in n or "failed" in n or "missing" in n or "no '" in n or "only" in n) for n in notes)
    return ("VERIFIED - " if ok else "CHECK - ") + "; ".join(notes)


def self_test_webhook() -> Dict[str, Any]:
    """Call our own GET /webhook exactly as Meta would."""
    base = config.PUBLIC_BASE_URL.rstrip("/")
    challenge = secrets.token_hex(8)
    out = {"url": f"{base}/webhook", "public": not any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0"))}
    try:
        # Probe with a WRONG token first: only a server that answers with this
        # bot's own rejection text is ours, and only then is the real verify
        # token sent (a mistyped PUBLIC_BASE_URL never receives it).
        bad = requests.get(f"{base}/webhook", params={"hub.mode": "subscribe",
                                                      "hub.verify_token": "not-the-token",
                                                      "hub.challenge": challenge}, timeout=10)
        out["rejects_wrong_token"] = bad.status_code == 403
        if bad.status_code != 403 or "Verification token mismatch" not in bad.text:
            out["ok"] = False
            out["error"] = f"{base}/webhook does not behave like this bot (HTTP {bad.status_code}); verify token not sent"
            out["signature_check"] = bool(config.APP_SECRET)
            return out
        r = requests.get(f"{base}/webhook", params={"hub.mode": "subscribe",
                                                    "hub.verify_token": config.VERIFY_TOKEN,
                                                    "hub.challenge": challenge}, timeout=10)
        out["status_code"] = r.status_code
        out["echoed"] = r.text.strip() == challenge
        out["ok"] = bool(out["echoed"] and out["rejects_wrong_token"])
    except requests.RequestException as error:
        out["ok"] = False
        out["error"] = f"could not reach {base}/webhook ({type(error).__name__})"
    out["signature_check"] = bool(config.APP_SECRET)
    return out


def subscribe_page(conn: Dict[str, Any]) -> Dict[str, Any]:
    """Subscribe this app to the account's webhooks (explicit owner action)."""
    if not current_token():
        raise ConnectError("Connect the account first - no token is available.")
    if flow() == "instagram":
        return _graph("POST", _ig("me/subscribed_apps"), token=current_token(),
                      data={"subscribed_fields": "messages,comments"},
                      what="account webhook subscription")
    page_id = config.PAGE_ID or str(conn.get("page_id") or "")
    if not page_id:
        raise ConnectError("Connect the account first - no Page id is available.")
    return _graph("POST", f"{page_id}/subscribed_apps", token=current_token(),
                  data={"subscribed_fields": "messages"}, what="page webhook subscription")


# ---------------------------------------------------------------------------
# HTML (small, inline, no secrets)
# ---------------------------------------------------------------------------
def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,Segoe UI,Arial;max-width:760px;margin:2rem auto;padding:0 1rem;color:#222}}
h1{{font-size:1.4rem}} .ok{{color:#0a7a2f}} .bad{{color:#b00020}} .warn{{color:#9a6700}}
.btn{{display:inline-block;padding:.6rem 1rem;border:1px solid #444;border-radius:6px;text-decoration:none;color:#222;background:#f6f6f6;margin:.3rem .3rem .3rem 0}}
.btn.primary{{background:#1877f2;color:#fff;border-color:#1877f2}} .btn.danger{{border-color:#b00020;color:#b00020}}
table{{border-collapse:collapse}} td{{padding:.25rem .8rem .25rem 0;vertical-align:top}} code{{background:#eee;padding:.1rem .3rem}}
.box{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}} form{{display:inline}}</style></head>
<body>{body}<p style="margin-top:2rem;font-size:.85rem;color:#666">Car Trends Car Mall - Instagram connection.
Passwords, OTP and 2FA codes are entered only on Meta's own pages and are never stored here.</p></body></html>""")


def _row(label: str, value: str) -> str:
    cls = "ok" if value.startswith(("OK", "VALID", "VERIFIED", "CHECKED", "CONNECTED", "CONFIGURED", "PASS")) \
        else "bad" if value.startswith(("MISSING", "INVALID", "ERROR", "NOT CONNECTED", "DIFFERENT", "FAIL")) else "warn"
    return f"<tr><td><b>{html.escape(label)}</b></td><td class='{cls}'>{html.escape(value)}</td></tr>"


def _status_table(checks: Dict[str, Any]) -> str:
    conn = load_connection()
    rows = []
    connected = bool(current_token())
    rows.append(_row("Status", "CONNECTED" if connected else "NOT CONNECTED"))
    if connected:
        rows.append(_row("Instagram", "@" + (checks.get("username") or config.INSTAGRAM_USERNAME or conn.get("instagram_username") or "?")))
        rows.append(_row("Account ID", mask_id(checks.get("account_id") or config.INSTAGRAM_ACCOUNT_ID)))
        rows.append(_row("Login flow", "Instagram login (graph.instagram.com)" if flow() == "instagram" else "Facebook login (Page token)"))
        if flow() != "instagram":
            rows.append(_row("Facebook Page", (conn.get("page_name") or "?") + " (" + mask_id(config.PAGE_ID) + ")"))
        rows.append(_row("Token", f"{checks['checks'].get('token', '?')} ({mask_token(current_token())})"))
        exp = checks.get("token_expires_at")
        if exp is not None:
            rows.append(_row("Token expiry", "never (long-lived Page token)" if not exp else
                             time.strftime("%Y-%m-%d", time.localtime(exp)) + " (refreshed automatically while the bot runs)"))
        for key, label in (("permissions", "Permissions"), ("account", "Account identity"),
                           ("comment_capability", "Comment capability"), ("messaging_capability", "DM capability"),
                           ("webhook", "Webhook (Meta side)")):
            rows.append(_row(label, str(checks["checks"].get(key, "NOT CHECKED"))))
        rows.append(_row("Last verification", checks.get("checked_at", "-")))
    for e in checks.get("errors", []):
        rows.append(_row("Note", "ERROR: " + e))
    return "<table>" + "".join(rows) + "</table>"


def _config_table() -> str:
    ok, why = flow_is_supported()
    local = any(h in config.PUBLIC_BASE_URL for h in ("localhost", "127.0.0.1"))
    rows = [_row("Login flow", "Instagram login" if flow() == "instagram" else "Facebook login")]
    if flow() == "instagram":
        rows += [_row("Instagram App ID", "CONFIGURED" if config.INSTAGRAM_APP_ID else "MISSING (set INSTAGRAM_APP_ID in .env)"),
                 _row("Instagram App Secret", "CONFIGURED" if config.INSTAGRAM_APP_SECRET else "MISSING (set INSTAGRAM_APP_SECRET in .env)"),
                 _row("Meta App ID / secret (webhook signature, app checks)",
                      "CONFIGURED" if (config.META_APP_ID and config.META_APP_SECRET) else "WARN optional but recommended (META_APP_ID, APP_SECRET)")]
    else:
        rows += [_row("Meta App ID", "CONFIGURED" if config.META_APP_ID else "MISSING (set META_APP_ID)"),
                 _row("App Secret", "CONFIGURED" if config.META_APP_SECRET else "MISSING (set APP_SECRET)")]
    rows += [
        _row("Verify token", "CONFIGURED" if config.VERIFY_TOKEN else "MISSING"),
        _row("Public base URL", ("WARN local only " if local else "CONFIGURED ") + config.PUBLIC_BASE_URL),
        _row("OAuth redirect URI (register it in the Meta app)", redirect_uri()),
        _row("Webhook callback URL (register it in the Meta app)", config.PUBLIC_BASE_URL.rstrip("/") + "/webhook"),
        _row("Graph API version", config.GRAPH_API_VERSION),
        _row("OAuth flow check", "OK" if ok else "FAIL " + why),
        _row("Expected account", "@" + expected_username() if expected_username() else "WARN not set (INSTAGRAM_USERNAME) - any account will need confirmation"),
    ]
    return "<table>" + "".join(rows) + "</table>"


def csrf_token() -> str:
    """A single-use form token for the owner's state-changing buttons: a
    cross-site page cannot obtain one, even with cached Basic credentials."""
    return _stash({"csrf": True})


def _csrf_ok(request: Request, form: Dict[str, str]) -> bool:
    site = (request.headers.get("sec-fetch-site") or "").lower() if request is not None else ""
    if site and site not in ("same-origin", "none"):
        return False
    data = _take(form.get("csrf"))
    return bool(data and data.get("csrf"))


def _forbidden() -> HTMLResponse:
    return _page("Rejected", "<h1>Request rejected</h1><p class='bad'>Missing or expired form token. "
                 "Open the status page again and use its buttons.</p>"
                 "<p><a class='btn' href='/connect/instagram/status'>Status</a></p>")


async def _form(request: Request) -> Dict[str, str]:
    """The few POST forms here are tiny urlencoded bodies; parsing them
    directly avoids a python-multipart dependency."""
    raw = await request.body()
    try:
        fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    except Exception:
        return {}
    return {k: v[0] for k, v in fields.items() if v}


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def setup_page(_: str = Depends(require_owner)):
    ok, _why = flow_is_supported()
    ready = ok
    connected = bool(current_token())
    body = f"""<h1>Connect Car Trends Instagram</h1>
<p>This connects the bot to the real Instagram account. You log in and approve permissions on
<b>Meta's own pages</b>; nothing you type there ever reaches this server. The bot receives only the
Page access token it needs, plus the account id and username.</p>
<div class="box"><b>Configuration</b>{_config_table()}</div>
<div class="box"><b>Current connection</b>{_status_table(run_checks() if connected else {'checks': {}, 'errors': []})}</div>
<p>{'<a class="btn primary" href="/connect/instagram/start">Connect Instagram</a>' if ready else
    '<span class="bad">' + html.escape(_why or "Complete the configuration above, restart, then connect.") + '</span>'}
<a class="btn" href="/connect/instagram/status">Status &amp; tests</a></p>
<p style="font-size:.9rem">Before connecting: the Instagram account must be a Professional account with the
<b>Instagram Tester</b> (or admin) role on this Meta app while the app is in development mode, and
<i>Instagram &gt; Settings &gt; Messages and story replies &gt; Message controls &gt; Connected tools</i> must be on.</p>"""
    return _page("Connect Instagram", body)


@router.get("/start")
def start(_: str = Depends(require_owner)):
    ok, why = flow_is_supported()
    if not ok:
        return _page("Connect Instagram", f"<h1>Cannot start</h1><p class='bad'>{html.escape(why)}</p>")
    browser = secrets.token_urlsafe(24)
    state = new_state(browser)
    _log(f"authorization started (state {state[:6]}...), redirect_uri={redirect_uri()}")
    resp = RedirectResponse(authorization_url(state), status_code=302)
    resp.set_cookie(COOKIE_NAME, browser, max_age=STATE_TTL_SECONDS, httponly=True,
                    samesite="lax", secure=redirect_uri().startswith("https://"), path="/connect/instagram")
    return resp


@router.get("/callback", response_class=HTMLResponse)
def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None,
             error: Optional[str] = None, error_reason: Optional[str] = None,
             error_description: Optional[str] = None):
    browser = request.cookies.get(COOKIE_NAME, "") if request is not None else ""
    ok, why = consume_state(state, browser)
    if not ok:
        _log(f"callback rejected: {why}")
        return _page("Connect Instagram", f"<h1>Authorization rejected</h1><p class='bad'>{html.escape(why)}</p>"
                     "<p><a class='btn' href='/connect/instagram'>Back to setup</a></p>")
    if error:
        msg = "You cancelled the authorization." if error_reason == "user_denied" else \
            f"Meta reported: {error} ({error_description or error_reason or 'no details'})"
        _log(f"authorization not granted: {_s(error)[:40]!r} / {_s(error_reason)[:40]!r}")
        return _page("Connect Instagram", f"<h1>Not connected</h1><p class='warn'>{html.escape(msg)}</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Try again</a></p>")
    if not code:
        return _page("Connect Instagram", "<h1>Not connected</h1><p class='bad'>Meta did not send an authorization code.</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Try again</a></p>")
    try:
        exchange = exchange_code(code)
        user_token = exchange["token"]
        granted = granted_permissions(user_token, exchange.get("permissions"))
        pages = instagram_pages(user_token, exchange)
    except ConnectError as error:
        return _page("Connect Instagram", f"<h1>Connection failed</h1><p class='bad'>{html.escape(str(error))}</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Try again</a></p>")
    declined = [s for s in required_scopes() if granted.get(s) != "granted"]
    if declined:
        _log(f"permissions missing: {declined}")
        return _page("Connect Instagram", "<h1>Permissions missing</h1><p class='bad'>These permissions were not granted: "
                     f"<code>{html.escape(', '.join(declined))}</code>. Approve all requested permissions "
                     "(they may also need App Review / Advanced Access in the Meta app).</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Try again</a></p>")
    if not pages:
        return _page("Connect Instagram", "<h1>No Instagram account found</h1><p class='bad'>"
                     + ("Instagram did not return a professional account for this login."
                        if flow() == "instagram" else
                        "None of your Facebook Pages has an Instagram professional account linked, or the "
                        "account was not selected during authorization. Link the Instagram account to the "
                        "Page in Meta Business settings and try again.") + "</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Try again</a></p>")
    pending = _stash({"pages": pages})
    exp = expected_username()
    match = [p for p in pages if p["instagram_username"] == exp] if exp else []
    if len(pages) == 1 and (not exp or match):
        return _finalise(pages[0])
    if match:
        return _finalise(match[0])
    # Several accounts, or none is the expected one: the owner chooses. The
    # choice is NOT a confirmation - replacing a connected account still asks.
    existing = load_connection()
    current = existing.get("instagram_username") or exp
    options = "".join(
        f"<label class='box' style='display:block'><input type='radio' name='choice' value='{i}' {'checked' if i == 0 else ''}> "
        f"<b>@{html.escape(p['instagram_username'])}</b> ({html.escape(p['instagram_name'])}) - Page: {html.escape(p['page_name'])}"
        f"{' <span class=bad>&nbsp;different from ' + ('the expected' if exp else 'the connected') + ' @' + html.escape(current) + '</span>' if current and p['instagram_username'] != current else ''}</label>"
        for i, p in enumerate(pages))
    warn = (f"<p class='bad'><b>Different Instagram account detected.</b> The {'expected' if exp else 'currently connected'} account is @{html.escape(current)}. "
            "Connecting another account will make the bot answer that account's customers.</p>" if current else "")
    return _page("Choose account", f"<h1>Choose the Instagram account</h1>{warn}"
                 f"<form method='post' action='/connect/instagram/choose'>{options}"
                 f"<input type='hidden' name='pending' value='{pending}'>"
                 "<p><a class='btn' href='/connect/instagram'>Cancel</a> "
                 "<button class='btn danger' type='submit'>Connect the selected account</button></p></form>")


@router.post("/choose", response_class=HTMLResponse)
async def choose(request: Request, _: str = Depends(require_owner)):
    form = await _form(request)
    pending, choice = form.get("pending"), form.get("choice", "")
    data = _take(pending)
    if data is None:
        return _page("Connect Instagram", "<h1>Expired</h1><p class='bad'>This choice expired. Start again.</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Start again</a></p>")
    if not str(choice).isdigit():
        return _page("Connect Instagram", "<h1>Invalid choice</h1><p class='bad'>Start again.</p>")
    try:
        page = data["pages"][int(choice)]
    except (ValueError, IndexError, KeyError, TypeError):
        return _page("Connect Instagram", "<h1>Invalid choice</h1><p class='bad'>Start again.</p>")
    return await run_in_threadpool(_finalise, page, False)


def _finalise(page: Dict[str, Any], confirmed: bool = False) -> HTMLResponse:
    existing = load_connection()
    exp = expected_username()
    pinned_id = (os.getenv("INSTAGRAM_ACCOUNT_ID") or "").strip()
    different_from_expected = (bool(exp) and page["instagram_username"] != exp) or \
        (bool(pinned_id) and pinned_id != page["instagram_account_id"])
    replacing = bool(existing.get("instagram_account_id")) and \
        existing.get("instagram_account_id") != page["instagram_account_id"]
    if (different_from_expected or replacing) and not confirmed:
        pending = _stash({"page": page})
        current = existing.get("instagram_username") or exp or (f"account id {mask_id(pinned_id)}" if pinned_id else "?")
        return _page("Confirm account", "<h1>Different Instagram account detected</h1>"
                     f"<p class='bad'>Authorized: <b>@{html.escape(page['instagram_username'])}</b> ({html.escape(page['instagram_name'])}). "
                     f"Currently expected/configured: <b>@{html.escape(current)}</b>.</p>"
                     "<p>The configured production account is <b>not</b> changed until you confirm.</p>"
                     f"<form method='post' action='/connect/instagram/confirm'><input type='hidden' name='pending' value='{pending}'>"
                     "<a class='btn' href='/connect/instagram'>Cancel (keep current)</a> "
                     "<button class='btn danger' type='submit'>Connect this different account</button></form>")
    try:
        info = verify_token(page["page_access_token"], page if flow() == "instagram" else None)
        me = account_identity(page["instagram_account_id"], page["page_access_token"])
    except ConnectError as error:
        return _page("Connect Instagram", f"<h1>Verification failed</h1><p class='bad'>{html.escape(str(error))}</p>"
                     "<p><a class='btn primary' href='/connect/instagram/start'>Try again</a></p>")
    if not info["is_valid"]:
        return _page("Connect Instagram", "<h1>Verification failed</h1><p class='bad'>Meta reports the Page token as invalid.</p>")
    miss = missing_scopes(info["scopes"])
    if miss:
        return _page("Permissions missing", "<h1>Permissions missing on the Page token</h1>"
                     f"<p class='bad'>{html.escape(', '.join(miss))}</p>")
    conn = {
        "flow": flow(),
        "app_scoped_id": page.get("app_scoped_id", ""),
        "connected_epoch": int(time.time()),
        "instagram_account_id": page["instagram_account_id"],
        "instagram_username": str(me.get("username") or page["instagram_username"]).lstrip("@").lower(),
        "instagram_name": me.get("name", page.get("instagram_name", "")),
        "page_id": page["page_id"], "page_name": page["page_name"],
        "page_access_token": page["page_access_token"],
        "scopes": info["scopes"], "token_expires_at": info["expires_at"],
        "app_id": config.META_APP_ID, "graph_api_version": config.GRAPH_API_VERSION,
        "connected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_connection(conn)
    _log(f"connected @{conn['instagram_username']} (id {mask_id(conn['instagram_account_id'])}, "
         f"token {mask_token(conn['page_access_token'])})")
    if token_pinned_by_environment():
        return RedirectResponse("/connect/instagram/status?" + urlencode({
            "msg": "Saved, but NOT active: PAGE_ACCESS_TOKEN is set in the environment and keeps "
                   "the bot on that token. Remove the variable and restart to use this connection."}),
            status_code=303)
    return RedirectResponse("/connect/instagram/status?just_connected=1", status_code=303)


@router.post("/confirm", response_class=HTMLResponse)
async def confirm(request: Request, _: str = Depends(require_owner)):
    form = await _form(request)
    data = _take(form.get("pending"))
    if data is None or "page" not in data:
        return _page("Connect Instagram", "<h1>Expired</h1><p class='bad'>This confirmation expired. Start again.</p>")
    return await run_in_threadpool(_finalise, data["page"], True)


def _pinned_note() -> str:
    return ("<p class='warn'>PAGE_ACCESS_TOKEN is set in the environment: the bot sends with that token, "
            "and Disconnect cannot switch it off. Remove the variable and restart to manage the "
            "connection here.</p>" if token_pinned_by_environment() else "")


@router.get("/status", response_class=HTMLResponse)
def status_page(just_connected: Optional[str] = None, msg: Optional[str] = None, _: str = Depends(require_owner)):
    checks = run_checks() if current_token() else {"checks": {}, "errors": []}
    connected = bool(current_token())
    def f(action, label, cls="btn", confirm_text=""):
        js = f" onsubmit=\"return confirm('{confirm_text}')\"" if confirm_text else ""
        return (f"<form method='post' action='{action}'{js}><input type='hidden' name='csrf' value='{csrf_token()}'>"
                f"<button class='{cls}'>{label}</button></form>")
    tests = ("<div class='box'><b>Test tools</b> (read-only unless a button says otherwise)<p>"
             + f("/connect/instagram/test/webhook", "Test webhook") + "</p></div>")
    if connected:
        tests = ("<div class='box'><b>Test tools</b> (read-only unless a button says otherwise)<p>"
                 + f("/connect/instagram/test/connection", "Test connection")
                 + f("/connect/instagram/test/webhook", "Test webhook")
                 + "<a class='btn' href='/connect/instagram/test/comment'>Prepare comment test</a>"
                 + "<a class='btn' href='/connect/instagram/test/dm'>Prepare DM test</a>"
                 + f("/connect/instagram/webhook/subscribe", "Subscribe webhook (Page)", confirm_text="Subscribe this Meta app to the Facebook Page for webhooks?")
                 + f("/connect/instagram/disconnect", "Disconnect", "btn danger", "Forget the stored Instagram connection?")
                 + "</p><p style='font-size:.85rem'>Meta also requires: the app in <b>Live</b> mode, <b>Advanced Access</b> for "
                   "instagram_manage_messages / instagram_manage_comments (App Review), and on Instagram "
                   "<i>Settings &gt; Messages and story replies &gt; Message controls &gt; Connected tools</i> switched on. "
                   "Run this server as a single process (one worker).</p></div>")
    body = f"""<h1>Instagram Connection</h1>
{'<p class="ok"><b>Connected.</b> Nothing has been sent to any customer.</p>' if just_connected else ''}
{'<p class="warn">' + html.escape(msg) + '</p>' if msg else ''}
{_pinned_note()}
{_status_table(checks)}{tests}
<p>{'' if connected else '<a class="btn primary" href="/connect/instagram/start">Connect Instagram</a>'}
<a class="btn" href="/connect/instagram">Setup page</a> <a class="btn" href="/dashboard">Dashboard</a></p>"""
    return _page("Instagram Connection", body)


@router.get("/status.json")
def status_json(_: str = Depends(require_owner)):
    checks = run_checks() if current_token() else {"connected": False, "checks": {"token": "MISSING"}, "errors": []}
    checks["token"] = mask_token(current_token())
    checks["account_id"] = mask_id(checks.get("account_id") or config.INSTAGRAM_ACCOUNT_ID)
    return JSONResponse(checks)


@router.post("/test/connection")
async def test_connection(request: Request, _: str = Depends(require_owner)):
    if not _csrf_ok(request, await _form(request)):
        return _forbidden()
    checks = await run_in_threadpool(run_checks)    # blocking Meta calls off the event loop
    conn = load_connection()
    if conn:
        conn["last_verification"] = checks
        conn["last_verification"].pop("scopes", None)
        save_connection(conn)
    return RedirectResponse("/connect/instagram/status?msg=Connection+re-checked", status_code=303)


@router.post("/test/webhook", response_class=HTMLResponse)
async def test_webhook(request: Request, _: str = Depends(require_owner)):
    if not _csrf_ok(request, await _form(request)):
        return _forbidden()
    # The self-test calls THIS server: it must run off the event loop or it
    # would wait for itself.
    r = await run_in_threadpool(self_test_webhook)
    rows = [_row("Webhook URL", r["url"]),
            _row("Publicly reachable address", "OK" if r["public"] else "WARN - localhost: Meta cannot reach this address; use a public HTTPS URL/tunnel and set PUBLIC_BASE_URL"),
            _row("GET /webhook echoes hub.challenge", "PASS" if r.get("echoed") else "FAIL " + str(r.get("error") or r.get("status_code"))),
            _row("Wrong verify token rejected (403)", "PASS" if r.get("rejects_wrong_token") else "FAIL"),
            _row("Signature check (APP_SECRET)", "PASS enabled" if r["signature_check"] else "WARN disabled - set APP_SECRET")]
    meta = (await run_in_threadpool(webhook_readiness, load_connection(), current_token())
            if current_token() else "NOT CHECKED (not connected)")
    rows.append(_row("Meta-side subscription", meta))
    return _page("Webhook test", "<h1>Webhook test</h1><table>" + "".join(rows) + "</table>"
                 "<p><a class='btn' href='/connect/instagram/status'>Back</a></p>")


@router.post("/webhook/subscribe", response_class=HTMLResponse)
async def webhook_subscribe(request: Request, _: str = Depends(require_owner)):
    if not _csrf_ok(request, await _form(request)):
        return _forbidden()
    try:
        res = await run_in_threadpool(subscribe_page, load_connection())
        note = "Page subscription request accepted by Meta." if res.get("success") else f"Meta answered: {json.dumps(res)[:200]}"
    except ConnectError as error:
        note = f"Subscription failed: {error}"
    return RedirectResponse("/connect/instagram/status?" + urlencode({"msg": note}), status_code=303)


@router.get("/test/comment", response_class=HTMLResponse)
def prepare_comment_test(_: str = Depends(require_owner)):
    if not current_token():
        return _page("Comment test", "<h1>Not connected</h1>")
    conn = load_connection()
    ig_id = config.INSTAGRAM_ACCOUNT_ID or str(conn.get("instagram_account_id") or "")
    items = ""
    try:
        media = _graph("GET", f"{ig_id}/media", token=current_token(), params={
            "fields": "id,caption,permalink,media_product_type,timestamp", "limit": 5},
            what="media list").get("data") or []
        def link(m):
            url = _s(m.get("permalink"))
            text = html.escape((_s(m.get("caption")) or "(no caption)")[:60])
            if url.startswith(("https://", "http://")):
                return f"<a href='{html.escape(url)}' target='_blank' rel='noopener'>{text}</a>"
            return text
        items = "".join(f"<li>{html.escape(_s(m.get('media_product_type')))} {html.escape(_s(m.get('timestamp'))[:10])} - {link(m)}</li>"
                        for m in media)
    except ConnectError as error:
        items = f"<li class='bad'>{html.escape(str(error))}</li>"
    body = f"""<h1>Prepare the Reel comment test</h1>
<p>Nothing is posted automatically. Do this yourself, from a <b>personal</b> Instagram account (not the business one):</p>
<ol><li>Open one of these recent posts/Reels:<ul>{items or '<li>(no media found)</li>'}</ul></li>
<li>Comment exactly: <code>GFX pro mats for Alto?</code></li>
<li>Within a few seconds you should see a short public reply under your comment and (if Meta allows a private reply) a DM.</li>
<li>Then reply in the same thread: <code>Alto</code> - the bot should continue the GFX mats topic.</li>
<li>Watch the exchange in the <a href="/dashboard">dashboard</a> (conversation id starts with <code>comment:</code>).</li></ol>
<p class='warn'>If nothing happens: the Meta app must have the <code>comments</code> webhook field subscribed with Advanced Access, and this server must be reachable at <code>{html.escape(config.PUBLIC_BASE_URL)}</code>.</p>
<p><a class='btn' href='/connect/instagram/status'>Back</a></p>"""
    return _page("Comment test", body)


def _recent_dm_senders(limit: int = 5) -> List[Dict[str, str]]:
    """Recent inbound DM senders from our own database (masked for display)."""
    try:
        rows = db.get_connection().execute(
            "SELECT customer_identifier, MAX(last_activity) AS t FROM conversations "
            "WHERE customer_identifier NOT LIKE 'comment:%' AND customer_identifier NOT LIKE 'console-%' "
            "GROUP BY customer_identifier ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["customer_identifier"], "masked": mask_id(r["customer_identifier"]), "when": r["t"]} for r in rows]
    except Exception:
        return []


@router.get("/test/dm", response_class=HTMLResponse)
def prepare_dm_test(_: str = Depends(require_owner)):
    senders = _recent_dm_senders()
    pending = _stash({"senders": senders})
    options = "".join(f"<label style='display:block'><input type='radio' name='choice' value='{i}'> "
                      f"{html.escape(s['masked'])} (last message {html.escape(str(s['when'])[:16])})</label>"
                      for i, s in enumerate(senders))
    body = f"""<h1>Prepare the DM test</h1>
<p>Meta only lets a business reply to people who messaged it first (messaging window). So:</p>
<p class='warn'>Prerequisite on Instagram itself: <i>Settings &gt; Messages and story replies &gt; Message controls &gt; Connected tools</i> must allow access, or Meta delivers no DM webhooks.</p>
<ol><li>From a <b>personal</b> Instagram account, send a DM to the business account, e.g. <code>GFX pro mats for Alto?</code></li>
<li>The bot answers automatically through the webhook - check the <a href="/dashboard">dashboard</a>.</li>
<li>Optionally send ONE manual test reply to that sender below. This sends a real message and requires your confirmation.</li></ol>
<div class="box"><b>Send one test reply</b> (to a sender who already messaged us){'' if senders else '<p class=warn>No inbound DM sender found yet.</p>'}
<form method="post" action="/connect/instagram/test/dm/send">{options}
<p>Message: <input name="text" value="Test message from Car Trends bot - please ignore." size="60"></p>
<input type="hidden" name="pending" value="{pending}">
<button class="btn danger" type="submit" onclick="return confirm('Send this test message to the selected sender? A real DM will be delivered.')">SEND TEST</button>
<a class="btn" href="/connect/instagram/status">CANCEL</a></form></div>"""
    return _page("DM test", body)


@router.post("/test/dm/send", response_class=HTMLResponse)
async def send_dm_test(request: Request, _: str = Depends(require_owner)):
    form = await _form(request)
    pending, choice, text = form.get("pending"), form.get("choice"), form.get("text", "")
    data = _take(pending)
    if data is None:
        return _page("DM test", "<h1>Expired</h1><p class='bad'>Start the DM test again.</p>")
    if not str(choice or "").isdigit():
        return _page("DM test", "<h1>No recipient selected</h1><p><a class='btn' href='/connect/instagram/test/dm'>Back</a></p>")
    try:
        target = data["senders"][int(choice)]
    except (TypeError, ValueError, IndexError, KeyError):
        return _page("DM test", "<h1>No recipient selected</h1><p><a class='btn' href='/connect/instagram/test/dm'>Back</a></p>")
    if not current_token():
        return _page("DM test", "<h1>Not connected</h1>")
    if not text.strip():
        return _page("DM test", "<h1>Empty message</h1><p><a class='btn' href='/connect/instagram/test/dm'>Back</a></p>")
    import bot
    ok = await run_in_threadpool(bot.send_instagram_reply, target["id"], text.strip()[:config.MAX_DM_LENGTH])
    _log(f"manual test DM to {target['masked']}: {'sent' if ok else 'FAILED'}")
    return _page("DM test", f"<h1>{'Test message sent' if ok else 'Send failed'}</h1>"
                 f"<p>Recipient {html.escape(target['masked'])}. {'Check the customer side of the conversation.' if ok else 'See the server log for the Meta error (no secrets are logged).'}</p>"
                 "<p><a class='btn' href='/connect/instagram/status'>Back</a></p>")


def _parse_signed_request(signed: str) -> Optional[Dict[str, Any]]:
    """Meta's signed_request: base64url(sig).base64url(json), HMAC-SHA256 with
    the app secret. Returns the payload only when the signature matches."""
    import base64
    try:
        sig_b64, payload_b64 = signed.split(".", 1)
        pad = lambda x: x + "=" * (-len(x) % 4)
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        payload_raw = base64.urlsafe_b64decode(pad(payload_b64))
        for secret in (config.INSTAGRAM_APP_SECRET, config.META_APP_SECRET):
            if secret and hmac.compare_digest(hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest(), sig):
                return json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None
    return None


@router.post("/deauthorize")
async def deauthorize(request: Request):
    """Meta calls this when the account removes the app: forget the token."""
    form = await _form(request)
    payload = _parse_signed_request(form.get("signed_request", ""))
    if payload is None:
        return JSONResponse({"status": "ignored"}, status_code=400)
    who = str(payload.get("user_id") or "")
    conn = load_connection()
    if who and who in (str(conn.get("app_scoped_id") or ""), str(conn.get("instagram_account_id") or "")):
        clear_connection()
        _log("account deauthorized the app - connection removed")
    return JSONResponse({"status": "ok"})


@router.post("/data-deletion")
async def data_deletion(request: Request):
    """Meta's data deletion request callback. Customer data for this account
    lives only in our own database; the owner handles deletion requests."""
    form = await _form(request)
    payload = _parse_signed_request(form.get("signed_request", ""))
    if payload is None:
        return JSONResponse({"status": "ignored"}, status_code=400)
    code = secrets.token_hex(8)
    _log(f"data deletion request received (confirmation {code})")
    return JSONResponse({"url": f"{config.PUBLIC_BASE_URL.rstrip('/')}/connect/instagram/deletion-status?code={code}",
                         "confirmation_code": code})


@router.get("/deletion-status", response_class=HTMLResponse)
def deletion_status(code: str = ""):
    return _page("Data deletion", "<h1>Data deletion request</h1><p>Request "
                 f"<code>{html.escape(code[:32])}</code> has been received by Car Trends Car Mall and is being "
                 "handled by the team. Contact 6367857737 for questions.</p>")


@router.post("/disconnect")
async def disconnect(request: Request, _: str = Depends(require_owner)):
    if not _csrf_ok(request, await _form(request)):
        return _forbidden()
    if token_pinned_by_environment():
        return RedirectResponse("/connect/instagram/status?" + urlencode({
            "msg": "Not disconnected: PAGE_ACCESS_TOKEN is set in the environment. Remove the variable "
                   "and restart the bot to stop sending with it."}), status_code=303)
    clear_connection()
    _log("connection removed by the owner")
    return RedirectResponse("/connect/instagram/status?msg=Disconnected", status_code=303)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cli_status() -> int:
    conn = load_connection()
    apply_connection(conn)
    ok_flow, why = flow_is_supported()
    lines = ["INSTAGRAM CONNECTION STATUS", "=" * 27]

    def item(label, value, action=None):
        lines.append(f"{label + ':':<28}{value}")
        if action:
            lines.append(f"{'Action:':<28}{action}")

    item("Login flow", "Instagram login (graph.instagram.com)" if flow() == "instagram" else "Facebook login (Page token)")
    if flow() == "instagram":
        item("Instagram App ID", "CONFIGURED" if config.INSTAGRAM_APP_ID else "MISSING", None if config.INSTAGRAM_APP_ID else "copy it from Meta App > Instagram > API setup with Instagram login into .env")
        item("Instagram App Secret", "CONFIGURED" if config.INSTAGRAM_APP_SECRET else "MISSING", None if config.INSTAGRAM_APP_SECRET else "same page ('Show') - enter it locally in .env, never in chat")
        item("Meta App ID", "CONFIGURED" if config.META_APP_ID else "WARN missing (optional)")
        item("Meta App Secret", "CONFIGURED" if config.META_APP_SECRET else "WARN missing (webhook signature check + app checks)")
    else:
        item("Meta App ID", "CONFIGURED" if config.META_APP_ID else "MISSING", None if config.META_APP_ID else "set META_APP_ID (Meta App Dashboard > App settings > Basic)")
        item("App Secret", "CONFIGURED" if config.META_APP_SECRET else "MISSING", None if config.META_APP_SECRET else "set APP_SECRET (same place); it also enables webhook signature checks")
    item("Verify token", "CONFIGURED" if config.VERIFY_TOKEN else "MISSING")
    item("Public base URL", config.PUBLIC_BASE_URL + ("" if not any(h in config.PUBLIC_BASE_URL for h in ("localhost", "127.0.0.1")) else "  (local only - Meta cannot reach it)"),
         None if not any(h in config.PUBLIC_BASE_URL for h in ("localhost", "127.0.0.1")) else "run behind a public HTTPS URL/tunnel and set PUBLIC_BASE_URL")
    item("Redirect URI", redirect_uri(), "register it as an OAuth redirect URI in the Meta app")
    item("Webhook URL", config.PUBLIC_BASE_URL.rstrip("/") + "/webhook", "register it as the webhook callback URL with the verify token")
    item("OAuth flow", "OK" if ok_flow else "INCOMPLETE - " + why)
    item("Instagram Account ID", "CONFIGURED" if config.INSTAGRAM_ACCOUNT_ID else "MISSING", None if config.INSTAGRAM_ACCOUNT_ID else "open /connect/instagram and authorize the account")
    item("Instagram Username", ("@" + config.INSTAGRAM_USERNAME) if config.INSTAGRAM_USERNAME else "MISSING", None if config.INSTAGRAM_USERNAME else "set INSTAGRAM_USERNAME (expected account) or connect")
    item("Access Token", f"CONFIGURED ({mask_token(current_token())})" if current_token() else "MISSING", None if current_token() else "open /connect/instagram and authorize the account")
    ready = True
    if current_token():
        checks = run_checks(conn)
        for key, label in (("token", "Token validity"), ("permissions", "Permissions"), ("account", "Account identity"),
                           ("comments_permission", "Comments permission"), ("messaging_permission", "Messaging permission"),
                           ("comment_capability", "Comment capability"), ("messaging_capability", "Messaging capability"),
                           ("webhook", "Webhook (Meta side)")):
            val = str(checks["checks"].get(key, "NOT CHECKED"))
            lines.append(f"{label + ':':<28}{val}")
            if val.startswith(("MISSING", "INVALID", "ERROR", "DIFFERENT", "CHECK")):
                ready = False
        for e in checks.get("errors", []):
            lines.append(f"{'Note:':<28}{e}")
    else:
        ready = False
    wh = self_test_webhook()
    lines.append(f"{'Webhook self-test:':<28}{'PASS' if wh.get('ok') else 'FAIL - ' + str(wh.get('error') or 'challenge not echoed')}")
    if not wh.get("ok"):
        ready = False
        lines.append(f"{'Action:':<28}start the bot (uvicorn bot:app) and make PUBLIC_BASE_URL reachable")
    lines.append("")
    lines.append(f"READY FOR REAL TEST: {'YES' if ready else 'NO'}")
    print("\n".join(lines))
    return 0 if ready else 1


def cli_url() -> None:
    print(f"Open (owner login required): {config.PUBLIC_BASE_URL.rstrip('/')}/connect/instagram")
    print(f"Redirect URI to register in the Meta app: {redirect_uri()}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "url":
        cli_url()
    else:
        sys.exit(cli_status())
