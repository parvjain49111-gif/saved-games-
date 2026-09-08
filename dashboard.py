"""
=============================================================================
 Car Trends Car Mall - OWNER DASHBOARD
=============================================================================

 Everything here is behind HTTP Basic authentication and served over the
 same Uvicorn process as the webhook, at /dashboard.

 SECURITY NOTES
 --------------
 * There is NO default password. If DASHBOARD_PASSWORD is unset, every
   request is refused - a dashboard full of real customer conversations must
   never be reachable with a guessable default.
 * Credentials are compared with hmac.compare_digest, so an attacker cannot
   learn the password one character at a time by timing the responses.
 * Customer identifiers are masked in list views. The owner can still open a
   single conversation, but a shoulder-surfed screen does not leak a whole
   customer list.
 * Nothing here is cached or logged.

 NO FAKE DATA
 ------------
 Every figure comes from a COUNT/SUM/AVG over real rows. A fresh install
 shows zeros and "No data yet", which is the honest answer.

 Run it behind HTTPS (the same ngrok tunnel works) before using it over the
 public internet - Basic auth sends the password base64-encoded, not
 encrypted.
=============================================================================
"""

import hmac
import html
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
import database as db
import knowledge as kb

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
_security = HTTPBasic(auto_error=True)


# ---------------------------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------------------------
def require_owner(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    """Reject anyone who is not the owner. Used on every single route."""
    if not config.dashboard_is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Dashboard is disabled because DASHBOARD_PASSWORD is not "
                    "set. Set it in the environment and restart."),
        )

    user_ok = hmac.compare_digest(creds.username, config.DASHBOARD_USER)
    pass_ok = hmac.compare_digest(creds.password, config.DASHBOARD_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def mask(identifier: str) -> str:
    """Show enough of a customer id to recognise it, not enough to harvest."""
    if not identifier:
        return "unknown"
    if len(identifier) <= 6:
        return identifier[0] + "***"
    return f"{identifier[:4]}***{identifier[-3:]}"


# ---------------------------------------------------------------------------
# DATE RANGE
# ---------------------------------------------------------------------------
RANGES = {"today": 1, "7d": 7, "30d": 30, "all": None}


def resolve_range(period: str, start: Optional[str], end: Optional[str]):
    """Turn the UI's period selector into arguments for the query layer.

    A custom range arrives from the date pickers as plain "YYYY-MM-DD", but
    timestamps are stored as full ISO strings. Comparing "2026-08-10" against
    "2026-08-10T14:05:00+00:00" lexicographically would EXCLUDE everything
    that happened on the end date, so the end is widened to the last instant
    of that day.
    """
    if start and end:
        if len(start) == 10:
            start = f"{start}T00:00:00+00:00"
        if len(end) == 10:
            end = f"{end}T23:59:59+00:00"
        return None, start, end
    return RANGES.get(period, 7), None, None


# ---------------------------------------------------------------------------
# JSON API - authenticated
# ---------------------------------------------------------------------------
@router.get("/api/overview")
def api_overview(period: str = "7d", start: Optional[str] = None,
                 end: Optional[str] = None, _: str = Depends(require_owner)):
    days, s, e = resolve_range(period, start, end)
    return {
        "period": period,
        "overview": db.overview_metrics(days, s, e),
        "services": db.service_breakdown(days, s, e),
        "price_questions": db.price_question_breakdown(days, s, e),
        "intents": db.intent_breakdown(days, s, e),
        "ai_performance": db.ai_performance(days, s, e),
    }


@router.get("/api/conversations")
def api_conversations(period: str = "7d", service: Optional[str] = None,
                      intent: Optional[str] = None,
                      lead_status: Optional[str] = None,
                      escalated: Optional[bool] = None,
                      customer: Optional[str] = None,
                      keyword: Optional[str] = None,
                      start: Optional[str] = None, end: Optional[str] = None,
                      limit: int = Query(50, le=200), offset: int = 0,
                      _: str = Depends(require_owner)):
    days, s, e = resolve_range(period, start, end)
    rows, total = db.search_conversations(
        service=service, intent=intent, lead_status=lead_status,
        escalated=escalated, customer=customer, keyword=keyword,
        days=days, start=s, end=e, limit=limit, offset=offset)
    for r in rows:
        r["customer_identifier"] = mask(r["customer_identifier"])
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@router.get("/api/conversation/{conversation_id}")
def api_conversation(conversation_id: str, _: str = Depends(require_owner)):
    messages = [dict(m) for m in db.get_messages(conversation_id)]
    intel = db.get_intelligence(conversation_id)
    lead = db.get_lead(conversation_id)
    if not messages and intel is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {
        "conversation_id": conversation_id,
        "messages": messages,
        "intelligence": dict(intel) if intel else None,
        "lead": dict(lead) if lead else None,
    }


@router.get("/api/leads")
def api_leads(status_filter: Optional[str] = None,
              limit: int = Query(50, le=200), offset: int = 0,
              _: str = Depends(require_owner)):
    rows, total = db.list_leads(status_filter, limit, offset)
    for r in rows:
        r["customer_identifier"] = mask(r["customer_identifier"])
    return {"total": total, "rows": rows}


@router.get("/api/knowledge-gaps")
def api_gaps(limit: int = Query(100, le=500), offset: int = 0,
             _: str = Depends(require_owner)):
    rows = [dict(r) for r in db.list_knowledge_gaps(limit, offset)]
    return {"total": len(rows), "rows": rows}


@router.get("/api/faqs")
def api_faqs(confirmed: Optional[bool] = None,
             _: str = Depends(require_owner)):
    faqs = kb.APPROVED_FAQS
    if confirmed is not None:
        faqs = [f for f in faqs if f["confirmed"] is confirmed]
    return {"total": len(faqs), "rows": faqs}


@router.get("/api/insights")
def api_insights(period: str = "30d", _: str = Depends(require_owner)):
    """Plain-language insights, each derived from a real query.

    Returns an empty list when there is no data rather than inventing an
    encouraging-sounding statistic.
    """
    days, s, e = resolve_range(period, None, None)
    label = {"today": "today", "7d": "in the last 7 days",
             "30d": "in the last 30 days", "all": "all time"}.get(period, period)

    overview = db.overview_metrics(days, s, e)
    services = db.service_breakdown(days, s, e)
    prices = db.price_question_breakdown(days, s, e)
    gaps = db.list_knowledge_gaps(limit=1)

    insights: List[str] = []
    if overview["total_conversations"] == 0:
        return {"period": period, "insights": [],
                "note": "No conversations recorded yet."}

    if services:
        top = services[0]
        insights.append(
            f"{top['service'].replace('_', ' ').title()} was the most "
            f"requested service {label} ({top['conversations']} conversations).")
    if prices:
        top = prices[0]
        insights.append(
            f"Price questions were highest for "
            f"{top['service'].replace('_', ' ').title()} "
            f"({top['price_questions']} {label}).")
    if overview["unknown_questions"]:
        insights.append(
            f"{overview['unknown_questions']} conversations {label} contained "
            f"questions the current knowledge base could not answer.")
    if overview["human_escalations"]:
        insights.append(
            f"{overview['human_escalations']} conversations {label} were "
            f"handed to the human team.")
    if gaps:
        g = gaps[0]
        insights.append(
            f"The most common missing information is \"{g['topic']}\", asked "
            f"{g['occurrences']} times.")
    if overview["total_leads"]:
        insights.append(
            f"{overview['total_leads']} leads were captured {label} "
            f"({overview['conversion_rate']}% of conversations).")
    return {"period": period, "insights": insights}


# ---------------------------------------------------------------------------
# HTML VIEW
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--tx:#e8eaed;--dim:#9aa3af;
      --acc:#4f8cff;--warn:#ffb020;--bad:#ff5c5c;--ok:#35c56a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
     font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:20px 28px;border-bottom:1px solid var(--line);
       display:flex;justify-content:space-between;align-items:center;
       flex-wrap:wrap;gap:12px}
h1{font-size:18px;margin:0}
h2{font-size:14px;margin:0 0 12px;color:var(--dim);text-transform:uppercase;
   letter-spacing:.06em}
main{padding:24px 28px;max-width:1200px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));
      margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px}
.kpi{font-size:26px;font-weight:600}
.kpi.zero{color:var(--dim)}
.lbl{color:var(--dim);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500}
.empty{color:var(--dim);padding:18px;text-align:center}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tabs a{padding:6px 12px;border-radius:6px;color:var(--dim);
        text-decoration:none;border:1px solid var(--line)}
.tabs a.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.bar{height:6px;background:var(--acc);border-radius:3px;min-width:2px}
.pill{padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line)}
.esc{color:var(--warn)} .gap{color:var(--bad)}
ul.ins{padding-left:18px;margin:0} ul.ins li{margin-bottom:6px}
section{margin-bottom:30px}
a{color:var(--acc)}
.range{color:var(--dim);margin-bottom:12px}
.filters{background:var(--card);border:1px solid var(--line);border-radius:10px;
         padding:14px;margin-bottom:26px;display:flex;gap:12px;
         flex-wrap:wrap;align-items:flex-end}
.filters label{display:flex;flex-direction:column;font-size:11px;
               color:var(--dim);gap:4px}
.filters select,.filters input{background:var(--bg);color:var(--tx);
   border:1px solid var(--line);border-radius:6px;padding:6px 8px;
   font-size:13px;min-width:130px}
.filters button{background:var(--acc);color:#fff;border:0;border-radius:6px;
   padding:8px 16px;font-size:13px;cursor:pointer}
.filters .clear{align-self:center;font-size:12px;color:var(--dim)}
.pager{display:flex;gap:14px;align-items:center;margin-top:10px;
       font-size:13px}
@media (prefers-color-scheme: light){
 :root{--bg:#f7f8fa;--card:#fff;--line:#e3e6ec;--tx:#12141a;--dim:#666e7a}
}
"""


def _kpi(label: str, value: Any, suffix: str = "") -> str:
    zero = "" if value else " zero"
    return (f'<div class="card"><div class="kpi{zero}">{html.escape(str(value))}'
            f'{suffix}</div><div class="lbl">{html.escape(label)}</div></div>')


def _table(headers: List[str], rows: List[List[str]], empty: str) -> str:
    if not rows:
        return f'<div class="card"><div class="empty">{html.escape(empty)}</div></div>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="card"><table><tr>{head}</tr>{body}</table></div>'



# ---------------------------------------------------------------------------
# FILTER OPTIONS
# ---------------------------------------------------------------------------
# Built from the taxonomy rather than from whatever happens to be in the
# database, so a filter for a service nobody has asked about yet still
# appears - and correctly returns "no conversations".
SERVICE_OPTIONS = [v for k, v in vars(kb.Service).items()
                   if not k.startswith("_") and isinstance(v, str)]
INTENT_OPTIONS = [v for k, v in vars(kb.Intent).items()
                  if not k.startswith("_") and isinstance(v, str)]
LEAD_OPTIONS = [v for k, v in vars(kb.LeadStatus).items()
                if not k.startswith("_") and isinstance(v, str)]

PERIOD_LABELS = [("today", "Today"), ("7d", "Last 7 days"),
                 ("30d", "Last 30 days"), ("all", "All time")]


def _select(name: str, options: List[str], current: Optional[str],
            any_label: str) -> str:
    opts = [f'<option value="">{html.escape(any_label)}</option>']
    for o in sorted(options):
        sel = " selected" if current == o else ""
        label = o.replace("_", " ").title()
        opts.append(f'<option value="{html.escape(o)}"{sel}>'
                    f'{html.escape(label)}</option>')
    return (f'<label>{html.escape(any_label)}'
            f'<select name="{name}">{"".join(opts)}</select></label>')


def _qs(**overrides) -> str:
    """Build a query string, dropping empty values."""
    parts = [f"{k}={quote_plus(str(v))}" for k, v in overrides.items()
             if v not in (None, "", False)]
    return "?" + "&".join(parts) if parts else ""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_home(period: str = "7d",
                   start: Optional[str] = None, end: Optional[str] = None,
                   service: Optional[str] = None,
                   intent: Optional[str] = None,
                   lead_status: Optional[str] = None,
                   escalated: Optional[str] = None,
                   customer: Optional[str] = None,
                   keyword: Optional[str] = None,
                   page: int = 1,
                   _: str = Depends(require_owner)):
    if start and end:
        period = "custom"
    days, s, e = resolve_range(period, start, end)

    # Headline metrics follow the DATE RANGE only. The conversation table
    # below follows every filter - mixing the two would make the KPIs mean
    # something different depending on which dropdown was touched last.
    ov = db.overview_metrics(days, s, e)
    services = db.service_breakdown(days, s, e)
    prices = db.price_question_breakdown(days, s, e)
    intents = db.intent_breakdown(days, s, e)
    perf = db.ai_performance(days, s, e)
    gaps = [dict(g) for g in db.list_knowledge_gaps(limit=25)]
    leads, lead_total = db.list_leads(limit=25)

    per_page = 25
    page = max(1, page)
    esc_flag = {"1": True, "0": False}.get(escalated)
    convos, convo_total = db.search_conversations(
        service=service, intent=intent, lead_status=lead_status,
        escalated=esc_flag, customer=customer, keyword=keyword,
        days=days, start=s, end=e,
        limit=per_page, offset=(page - 1) * per_page)

    insights = api_insights(period if period != "custom" else "all",
                            _="owner")["insights"]

    # ---- date range controls ---------------------------------------------
    base_filters = dict(service=service, intent=intent,
                        lead_status=lead_status, escalated=escalated,
                        customer=customer, keyword=keyword)
    tabs = "".join(
        f'<a class="{"on" if period == k else ""}" '
        f'href="{_qs(period=k, **base_filters)}">{v}</a>'
        for k, v in PERIOD_LABELS)

    filter_form = f"""
    <form class="filters" method="get">
      <input type="hidden" name="period" value="{html.escape(period)}">
      {_select("service", SERVICE_OPTIONS, service, "Any service")}
      {_select("intent", INTENT_OPTIONS, intent, "Any intent")}
      {_select("lead_status", LEAD_OPTIONS, lead_status, "Any lead status")}
      <label>Escalated<select name="escalated">
        <option value="">Any</option>
        <option value="1"{' selected' if escalated == '1' else ''}>Yes</option>
        <option value="0"{' selected' if escalated == '0' else ''}>No</option>
      </select></label>
      <label>Customer<input name="customer" value="{html.escape(customer or '')}"
             placeholder="id contains"></label>
      <label>Keyword<input name="keyword" value="{html.escape(keyword or '')}"
             placeholder="in message text"></label>
      <label>From<input type="date" name="start" value="{html.escape(start or '')}"></label>
      <label>To<input type="date" name="end" value="{html.escape(end or '')}"></label>
      <button type="submit">Apply</button>
      <a class="clear" href="{_qs(period=period)}">Clear</a>
    </form>"""

    # ---- tables ----------------------------------------------------------
    top = max((x["conversations"] for x in services), default=1) or 1
    service_rows = [[
        html.escape(x["service"].replace("_", " ").title()),
        str(x["conversations"]),
        f'<div class="bar" style="width:{int(x["conversations"] / top * 100)}%"></div>',
        f'<a href="{_qs(period=period, start=start, end=end, service=x["service"])}">view</a>',
    ] for x in services]

    price_rows = [[html.escape(x["service"].replace("_", " ").title()),
                   str(x["price_questions"]),
                   f'<a href="{_qs(period=period, start=start, end=end, service=x["service"], intent="PRICE_INQUIRY")}">view</a>']
                  for x in prices]

    intent_rows = [[html.escape(x["intent"].replace("_", " ").title()),
                    str(x["conversations"]),
                    f'<a href="{_qs(period=period, start=start, end=end, intent=x["intent"])}">view</a>']
                   for x in intents]

    gap_rows = [[
        html.escape(g["topic"]),
        f'<b class="gap">{g["occurrences"]}</b>',
        html.escape(g["service"] or "-"),
        html.escape(g["first_seen"][:10]),
        html.escape(g["last_seen"][:10]),
        f'<span class="pill">{html.escape(g["status"])}</span>',
    ] for g in gaps]

    convo_rows = [[
        f'<a href="conversation/{html.escape(c["conversation_id"])}">'
        f'{html.escape(mask(c["customer_identifier"]))}</a>',
        html.escape(c["service"] or "-"),
        html.escape(c["primary_intent"] or "-"),
        html.escape(c["buying_intent"] or "-"),
        html.escape(c["lead_status"] or "-"),
        '<span class="esc">yes</span>' if c["human_escalation"] else "no",
        str(c["message_count"]),
        html.escape(c["last_activity"][:16].replace("T", " ")),
    ] for c in convos]

    lead_rows = [[
        f'<a href="conversation/{html.escape(l["conversation_id"])}">'
        f'{html.escape(mask(l["customer_identifier"]))}</a>',
        html.escape(((l["car_brand"] or "") + " " + (l["car_model"] or "")).strip() or "-"),
        html.escape(l["service"] or "-"),
        f'<span class="pill">{html.escape(l["status"])}</span>',
        html.escape(l["updated_at"][:16].replace("T", " ")),
    ] for l in leads]

    # ---- pagination ------------------------------------------------------
    pages = max(1, (convo_total + per_page - 1) // per_page)
    nav = []
    if page > 1:
        nav.append(f'<a href="{_qs(period=period, start=start, end=end, page=page - 1, **base_filters)}">&larr; Previous</a>')
    nav.append(f'<span class="lbl">Page {page} of {pages} '
               f'({convo_total} conversations)</span>')
    if page < pages:
        nav.append(f'<a href="{_qs(period=period, start=start, end=end, page=page + 1, **base_filters)}">Next &rarr;</a>')
    pager = f'<div class="pager">{" ".join(nav)}</div>'

    ins_html = ("<ul class='ins'>"
                + "".join(f"<li>{html.escape(i)}</li>" for i in insights)
                + "</ul>") if insights else \
               '<div class="empty">No data available yet.</div>'

    range_note = (f"{start} to {end}" if period == "custom"
                  else dict(PERIOD_LABELS).get(period, period))

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(kb.BUSINESS['name'])} - Owner Dashboard</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>{html.escape(kb.BUSINESS['name'])} &mdash; Owner Dashboard</h1>
  <div class="tabs">{tabs}</div>
</header>
<main>
  <div class="range">Showing: <b>{html.escape(str(range_note))}</b></div>
  {filter_form}

  <section>
    <h2>Overview &mdash; {html.escape(str(range_note))}</h2>
    <div class="grid">
      {_kpi("Conversations", ov["total_conversations"])}
      {_kpi("Unique customers", ov["unique_customers"])}
      {_kpi("Leads", ov["total_leads"])}
      {_kpi("Booking requests", ov["booking_requests"])}
      {_kpi("Human escalations", ov["human_escalations"])}
      {_kpi("Unknown questions", ov["unknown_questions"])}
      {_kpi("Low confidence", ov["low_confidence"])}
      {_kpi("Conversion rate", ov["conversion_rate"], "%")}
    </div>
  </section>

  <section><h2>AI performance</h2>
    <div class="grid">
      {_kpi("Avg confidence", perf["average_confidence"])}
      {_kpi("Knowledge coverage", perf["knowledge_coverage"], "%")}
      {_kpi("Escalation rate", perf["escalation_rate"], "%")}
      {_kpi("Unknown rate", perf["unknown_rate"], "%")}
      {_kpi("Unresolved", perf["unresolved"])}
    </div>
  </section>

  <section><h2>Insights</h2><div class="card">{ins_html}</div></section>

  <section><h2>Service analytics</h2>
    {_table(["Service", "Conversations", "", ""], service_rows,
            "No service data yet.")}</section>

  <section><h2>Price questions by service</h2>
    {_table(["Service", "Price questions", ""], price_rows,
            "No price questions recorded yet.")}</section>

  <section><h2>Intent breakdown</h2>
    {_table(["Intent", "Conversations", ""], intent_rows,
            "No intent data yet.")}</section>

  <section><h2>Knowledge gaps &mdash; information required</h2>
    {_table(["Topic", "Times asked", "Service", "First seen", "Last seen",
             "Status"], gap_rows, "No knowledge gaps recorded yet.")}</section>

  <section><h2>Conversations</h2>
    {_table(["Customer", "Service", "Intent", "Buying", "Lead", "Escalated",
             "Msgs", "Last activity"], convo_rows,
            "No conversations match these filters.")}
    {pager}</section>

  <section><h2>Leads ({lead_total} total)</h2>
    {_table(["Customer", "Vehicle", "Service", "Status", "Updated"],
            lead_rows, "No leads recorded yet.")}</section>
</main></body></html>"""


@router.get("/conversation/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(conversation_id: str, _: str = Depends(require_owner)):
    """The full transcript of one conversation, with its classification.

    This is the only place an unmasked customer identifier is shown - the
    owner has deliberately opened this one record, rather than being handed
    a harvestable list.
    """
    messages = db.get_messages(conversation_id, limit=500)
    intel = db.get_intelligence(conversation_id)
    lead = db.get_lead(conversation_id)
    if not messages and intel is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conn = db.get_connection()
    head = conn.execute(
        "SELECT * FROM conversations WHERE conversation_id = ?",
        (conversation_id,)).fetchone()

    bubbles = []
    for m in messages:
        side = "in" if m["direction"] == "IN" else "out"
        who = "Customer" if m["direction"] == "IN" else "Bot"
        tag = f' &middot; {html.escape(m["source"])}' if m["source"] else ""
        rt = (f' &middot; {m["response_time_ms"]}ms'
              if m["response_time_ms"] else "")
        bubbles.append(
            f'<div class="msg {side}"><div class="meta">{who}'
            f' &middot; {html.escape(m["timestamp"][:19].replace("T", " "))}'
            f'{tag}{rt}</div>{html.escape(m["message_text"])}</div>')

    def row(label, value):
        return (f'<tr><th>{html.escape(label)}</th>'
                f'<td>{html.escape(str(value if value not in (None, "") else "-"))}</td></tr>')

    intel_rows = ""
    if intel:
        intel_rows = "".join([
            row("Detected service", intel["service"]),
            row("Detected intent", intel["primary_intent"]),
            row("Sub topic / vehicle", intel["sub_topic"]),
            row("Buying intent", intel["buying_intent"]),
            row("Lead status", intel["lead_status"]),
            row("Booking status", intel["booking_status"]),
            row("Bot confidence", intel["bot_confidence"]),
            row("Knowledge base covered",
                "yes" if intel["knowledge_base_coverage"] else "no"),
            row("Human escalation",
                "YES" if intel["human_escalation"] else "no"),
            row("Resolution", intel["resolution_status"]),
        ])
    else:
        intel_rows = '<tr><td class="empty">Not classified yet.</td></tr>'

    lead_rows = ""
    if lead:
        lead_rows = "".join([
            row("Status", lead["status"]),
            row("Name", lead["customer_name"]),
            row("Phone", lead["phone"]),
            row("Vehicle", f'{lead["car_brand"] or ""} {lead["car_model"] or ""}'.strip()),
            row("Year / variant", lead["variant_year"]),
            row("Service", lead["service"]),
            row("Requirement", lead["requirement"]),
            row("Preferred date", lead["preferred_date"]),
            row("Preferred time", lead["preferred_time"]),
            row("Pickup / drop", "yes" if lead["pickup_drop"] else "no"),
        ])
    else:
        lead_rows = '<tr><td class="empty">No lead recorded.</td></tr>'

    customer = head["customer_identifier"] if head else "unknown"
    started = head["start_time"][:19].replace("T", " ") if head else "-"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conversation &mdash; {html.escape(kb.BUSINESS['name'])}</title>
<style>{_CSS}
.msg{{max-width:70%;padding:10px 13px;border-radius:12px;margin:8px 0;
      border:1px solid var(--line);background:var(--card)}}
.msg.in{{margin-right:auto}}
.msg.out{{margin-left:auto;background:var(--acc);color:#fff;border-color:var(--acc)}}
.msg .meta{{font-size:11px;opacity:.75;margin-bottom:4px}}
.two{{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}}
table.kv th{{width:45%;color:var(--dim);font-weight:500}}
</style></head><body>
<header>
  <h1>Conversation</h1>
  <div class="tabs"><a href="../">&larr; Back to dashboard</a></div>
</header>
<main>
  <section><div class="card">
    <div class="lbl">Customer</div><div class="kpi">{html.escape(customer)}</div>
    <div class="lbl">Started {html.escape(started)}
      &middot; {len(messages)} messages</div>
  </div></section>

  <section><h2>Transcript</h2>
    <div class="card">{"".join(bubbles) or '<div class="empty">No messages.</div>'}</div>
  </section>

  <div class="two">
    <section><h2>Classification</h2>
      <div class="card"><table class="kv">{intel_rows}</table></div></section>
    <section><h2>Lead</h2>
      <div class="card"><table class="kv">{lead_rows}</table></div></section>
  </div>
</main></body></html>"""
