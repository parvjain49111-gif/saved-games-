"""
=============================================================================
 Car Trends Car Mall - CENTRAL CONFIGURATION
=============================================================================

 Every environment-driven setting lives here so that no secret is ever
 hard-coded into application logic. Each value falls back to a safe,
 non-secret default so the project still runs out of the box for local
 testing - but anything genuinely secret (tokens, passwords) defaults to
 EMPTY, never to a real value.

 Set these in PowerShell before starting the bot:

     $env:PAGE_ACCESS_TOKEN = "..."      # Meta send-message token
     $env:APP_SECRET        = "..."      # Meta app secret (signature check)
     $env:VERIFY_TOKEN      = "..."      # webhook handshake token
     $env:DASHBOARD_PASSWORD = "..."     # owner dashboard login

=============================================================================
"""

import os
import re
from typing import Tuple


def _load_dotenv() -> None:
    """Read KEY=VALUE lines from a local .env file next to this module.

    The environment itself always wins; the file only fills in what is not
    set. It is git-ignored, so secrets typed there stay on this machine.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path) or os.getenv("CARTRENDS_IGNORE_DOTENV") == "1":
        return                       # test runs must not pick up the owner's file
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                if value.upper() in ("FILL-IN", "FILL_IN", "CHANGEME", "TODO", ""):
                    continue                 # an unfilled placeholder is not a value
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as error:      # a damaged .env must never stop the bot
        print(f"[CONFIG] .env ignored: {type(error).__name__}")


_load_dotenv()
DOTENV_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def update_dotenv_value(key: str, value: str, path: str = "") -> bool:
    """Rewrite one KEY=VALUE line of the local .env (atomically, keeping every
    other line and comment) and mirror it into this process's environment.
    Used when the bot refreshes its own access token or the tunnel address
    changes, so the next start picks the new value up. Returns False when the
    file does not exist (the value was set in the real environment instead)."""
    path = path or DOTENV_PATH
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    done = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            done = True
    if not done:
        if lines and lines[-1] != "":
            lines.append("")
        lines.insert(len(lines) - 1, f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(nl.join(lines))
    os.replace(tmp, path)
    os.environ[key] = value
    return True

# ---------------------------------------------------------------------------
# META / INSTAGRAM
# ---------------------------------------------------------------------------
VERIFY_TOKEN: str = os.getenv("VERIFY_TOKEN", "my_secret_token")
# The send token. INSTAGRAM_ACCESS_TOKEN (the name Meta's "API setup with
# Instagram login" page uses when you generate a token) is accepted too.
PAGE_ACCESS_TOKEN: str = (os.getenv("PAGE_ACCESS_TOKEN", "") or os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
                          or os.getenv("ACCESS_TOKEN", ""))
APP_SECRET: str = os.getenv("APP_SECRET", "")

# Two current Meta setups exist for an Instagram professional account:
#   * "API setup with Instagram login": an Instagram user token from
#     api.instagram.com, Graph calls to graph.instagram.com, permissions
#     instagram_business_basic / _manage_messages / _manage_comments.
#     Needs INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET (shown on that page).
#   * "API setup with Facebook login": a Page token, graph.facebook.com.
# META_LOGIN_FLOW picks one; by default the Instagram-login flow is used
# whenever INSTAGRAM_APP_ID is configured.
INSTAGRAM_APP_ID: str = os.getenv("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET: str = os.getenv("INSTAGRAM_APP_SECRET", "")
META_LOGIN_FLOW: str = (os.getenv("META_LOGIN_FLOW", "").strip().lower()
                        or ("instagram" if INSTAGRAM_APP_ID else "facebook"))
# Graph API versions live about two years; set GRAPH_API_VERSION before Meta
# retires the default (v21.0 is available until 2027-01-21; newer versions
# such as v25.0/v26.0 serve the same endpoints this project uses).
GRAPH_API_VERSION: str = os.getenv("GRAPH_API_VERSION", "v21.0")
GRAPH_API_URL: str = os.getenv(
    "GRAPH_API_URL",
    f"https://graph.instagram.com/{GRAPH_API_VERSION}/me/messages"
    if META_LOGIN_FLOW == "instagram"
    else f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages",
)
GRAPH_TIMEOUT: Tuple[int, int] = (10, 30)

# Instagram rejects DMs longer than 1000 characters; 900 leaves a margin.
MAX_DM_LENGTH: int = 900

# ---------------------------------------------------------------------------
# INSTAGRAM COMMENT AUTOMATION (Reels / posts)
# ---------------------------------------------------------------------------
# Comments arrive on the same webhook (field "comments") and are answered by
# the same brain. Two delivery paths exist on Meta's side:
#   * a PUBLIC reply:  POST /{comment_id}/replies        (short, generic)
#   * a PRIVATE reply: POST /me/messages with recipient {"comment_id": ...}
#     (Meta "private replies": one per comment, within 7 days, own media only)
COMMENT_REPLIES_ENABLED: bool = os.getenv("COMMENT_REPLIES_ENABLED", "1") == "1"
COMMENT_PRIVATE_REPLY: bool = os.getenv("COMMENT_PRIVATE_REPLY", "1") == "1"
COMMENT_MAX_LENGTH: int = 300
# Instagram locks accounts that behave like bots. Three limits keep the
# public surface human: never repeat the same sentence within the window,
# leave a gap between public replies, and stop after a daily ceiling.
COMMENT_REPEAT_WINDOW_HOURS: int = int(os.getenv("COMMENT_REPEAT_WINDOW_HOURS", "24"))
COMMENT_MIN_INTERVAL_SECONDS: int = int(os.getenv("COMMENT_MIN_INTERVAL_SECONDS", "20"))
COMMENT_DAILY_LIMIT: int = int(os.getenv("COMMENT_DAILY_LIMIT", "40"))
# The public reply never carries the phone number: the same number repeated
# under comment after comment is the clearest automation signal there is.
# The number still goes out in the private reply, where Meta expects it.
COMMENT_PHONE_IN_PUBLIC: bool = os.getenv("COMMENT_PHONE_IN_PUBLIC", "0") == "1"
# The Graph API base for comment replies - derived from GRAPH_API_URL so the
# Facebook-Login / Instagram-Login host choice is made in ONE place. Works
# for .../me/messages, .../<IG_ID>/messages and .../<PAGE_ID>/messages.


def graph_base(url: str) -> str:
    """'https://graph.instagram.com/v21.0/1784.../messages' -> 'https://graph.instagram.com/v21.0'."""
    m = re.match(r"^(https?://[^/]+/v\d+\.\d+)", url)
    if m:
        return m.group(1)
    return url.rsplit("/messages", 1)[0].rsplit("/", 1)[0]


GRAPH_API_BASE: str = os.getenv("GRAPH_API_BASE", graph_base(GRAPH_API_URL))
# Our own Instagram professional account id (and username). Comments written
# by this account are our own replies echoed back and are never answered.
INSTAGRAM_ACCOUNT_ID: str = os.getenv("INSTAGRAM_ACCOUNT_ID", "")
INSTAGRAM_USERNAME: str = os.getenv("INSTAGRAM_USERNAME", "").lstrip("@").lower()
PAGE_ID: str = os.getenv("PAGE_ID", "")

# ---------------------------------------------------------------------------
# CONNECT INSTAGRAM (OAuth setup flow - see instagram_connect.py)
# ---------------------------------------------------------------------------
# The Meta app id is public; the app secret is the same secret that signs
# webhooks (APP_SECRET). META_APP_SECRET is accepted as an alias.
META_APP_ID: str = os.getenv("META_APP_ID", "")
META_APP_SECRET: str = os.getenv("META_APP_SECRET", "") or APP_SECRET
# Where Meta can reach THIS server (https://... in production, or a tunnel).
# The OAuth redirect URI is PUBLIC_BASE_URL + /connect/instagram/callback.
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
# Hosted on Railway: the platform publishes the service's domain itself, so a
# generated domain is picked up with no manual configuration.
if not os.getenv("PUBLIC_BASE_URL") and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    PUBLIC_BASE_URL = "https://" + os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")


def detect_quick_tunnel() -> str:
    """A Cloudflare QUICK tunnel gets a new random *.trycloudflare.com name on
    every restart; cloudflared publishes it on its local metrics port. Returns
    'https://<host>' or '' (never raises)."""
    try:
        import json as _json
        import urllib.request
        for port in range(20241, 20251):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/quicktunnel", timeout=0.4) as resp:
                    host = (_json.loads(resp.read().decode("utf-8")) or {}).get("hostname", "")
                    if host:
                        return "https://" + host
            except Exception:
                continue
    except Exception:
        pass
    return ""


# The live tunnel wins over a stale .env value: whenever cloudflared runs a
# quick tunnel on this machine, its current hostname is the public address.
_LIVE_TUNNEL: str = detect_quick_tunnel() if os.getenv("CARTRENDS_IGNORE_DOTENV") != "1" else ""
if _LIVE_TUNNEL and _LIVE_TUNNEL != PUBLIC_BASE_URL:
    if PUBLIC_BASE_URL and "trycloudflare.com" in PUBLIC_BASE_URL or "localhost" in PUBLIC_BASE_URL:
        print(f"[CONFIG] quick tunnel detected: {_LIVE_TUNNEL} (replaces {PUBLIC_BASE_URL})")
        PUBLIC_BASE_URL = _LIVE_TUNNEL
# Extra OAuth scopes, comma separated (e.g. business_management), if the
# Pages live inside a Business Manager that needs it.
META_EXTRA_SCOPES: str = os.getenv("META_EXTRA_SCOPES", "")
# Where the OAuth result is stored: ONLY the Page token, ids and username.
# Never committed (see .gitignore); environment variables override it. The
# default lives next to this file, whatever the working directory is.
CONNECTION_FILE: str = os.getenv("CONNECTION_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "instagram_connection.json")
# Facebook Login for Business: a login CONFIGURATION id (App Dashboard >
# Facebook Login for Business > Configurations). When set it replaces the
# classic "scope" parameter, as Meta now recommends.
META_LOGIN_CONFIG_ID: str = os.getenv("META_LOGIN_CONFIG_ID", "")


def clean_token(value) -> str:
    """A usable access token: a non-empty string with no control characters.
    Anything else counts as 'no token' (and is never echoed anywhere)."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) < 20 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return ""
    return value


PAGE_ACCESS_TOKEN = clean_token(PAGE_ACCESS_TOKEN)


def _load_stored_connection() -> None:
    """A connection made through /connect/instagram fills in the Meta values
    that the environment did not set. The environment always wins."""
    global PAGE_ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID, INSTAGRAM_USERNAME, PAGE_ID
    try:
        if os.getenv("CARTRENDS_IGNORE_DOTENV") == "1" and not os.getenv("CONNECTION_FILE"):
            return                   # a test run must never pick up the owner's real token
        if not os.path.exists(CONNECTION_FILE):
            return
        import json
        with open(CONNECTION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return
        if not PAGE_ACCESS_TOKEN and clean_token(data.get("page_access_token")):
            PAGE_ACCESS_TOKEN = clean_token(data.get("page_access_token"))
        elif not PAGE_ACCESS_TOKEN and data.get("page_access_token"):
            print("[CONFIG] stored Instagram token is not usable - ignored")
        if not INSTAGRAM_ACCOUNT_ID and data.get("instagram_account_id"):
            INSTAGRAM_ACCOUNT_ID = str(data["instagram_account_id"])
        if not INSTAGRAM_USERNAME and data.get("instagram_username"):
            INSTAGRAM_USERNAME = str(data["instagram_username"]).lstrip("@").lower()
        if not PAGE_ID and data.get("page_id"):
            PAGE_ID = str(data["page_id"])
    except Exception as error:      # a damaged file must never stop the bot
        print(f"[CONFIG] stored Instagram connection ignored: {type(error).__name__}")


_load_stored_connection()

# ---------------------------------------------------------------------------
# OLLAMA (local LLM)
# ---------------------------------------------------------------------------
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT: Tuple[int, int] = (10, 180)

# Keeps the model resident in RAM between DMs. Without this Ollama unloads
# after ~5 minutes idle and the next customer waits over a minute.
OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
# SQLite is used because it needs no server process, handles the volume a
# single Instagram account produces comfortably, and keeps the project a
# copy-and-run affair. The DAO layer in database.py is written so the SQL can
# be pointed at Postgres later without touching application code.
DB_PATH: str = os.getenv("DB_PATH", "cartrends.db")

# ---------------------------------------------------------------------------
# CONVERSATION CONTEXT
# ---------------------------------------------------------------------------
# How long a conversation stays "open" before a new customer message starts a
# fresh conversation row. Also bounds how long remembered context (car model,
# service of interest) is considered current.
CONTEXT_TTL_MINUTES: int = int(os.getenv("CONTEXT_TTL_MINUTES", "180"))

# ---------------------------------------------------------------------------
# OWNER DASHBOARD
# ---------------------------------------------------------------------------
DASHBOARD_USER: str = os.getenv("DASHBOARD_USER", "owner")

# NO DEFAULT ON PURPOSE. When this is empty the dashboard refuses every
# request rather than shipping a guessable password.
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")

# ---------------------------------------------------------------------------
# CONFIDENCE THRESHOLDS
# ---------------------------------------------------------------------------
# Below LOW_CONFIDENCE the answer is treated as unreliable: it is logged as a
# knowledge gap and the customer is handed to the human team.
FAQ_MATCH_THRESHOLD: float = float(os.getenv("FAQ_MATCH_THRESHOLD", "0.42"))
LOW_CONFIDENCE: float = float(os.getenv("LOW_CONFIDENCE", "0.35"))


def dashboard_is_configured() -> bool:
    """True only when a dashboard password has actually been set."""
    return bool(DASHBOARD_PASSWORD)


def send_is_configured() -> bool:
    """True only when a real Meta send-token has been supplied."""
    return bool(PAGE_ACCESS_TOKEN)
