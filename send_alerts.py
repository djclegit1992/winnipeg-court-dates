#!/usr/bin/env python3
"""
Courtready: send court date alert emails.

Runs straight after the scraper, in the same workflow job. Two jobs:

  1. Send the "dates are available" email to anyone waiting on a
     category that now has dates, then close that subscription.

  2. Catch any confirmation email the Edge Function failed to send.
     The webhook fires once when someone signs up. If it fails, nothing
     retries it, so this sweeps up anything older than an hour with no
     confirmation recorded.

Matching is on jurisdiction + location_code + hearing_code together.
Never on hearing_code alone, because those codes are only unique within
a location and the table is built to hold more than one city.

Environment:
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  service role key, NOT the anon key
  POSTMARK_TOKEN        server API token
  POSTMARK_FROM         admin@courtdates.ca
  POSTMARK_STREAM       optional, defaults to "outbound"
  ALERTS_DRY_RUN=1      log what would be sent, send nothing

Safety rules, in order of how much they matter:

1. It only reads data/latest.json, which the scraper writes ONLY when a
   run passes its health checks. A quarantined run cannot trigger email.
2. It refuses to act on a stale file, so a failed scrape cannot cause
   emails based on yesterday's picture.
3. A row is marked sent only after Postmark accepts the message. If
   sending fails, the row stays active and the next run tries again.
4. If nothing is configured it exits quietly rather than failing the
   workflow. Scraping keeps working even with email switched off.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

WINNIPEG = ZoneInfo("America/Winnipeg")
LATEST = "data/latest.json"
MAX_AGE_MINUTES = 90
CONFIRMATION_BACKSTOP_MINUTES = 60

JURISDICTION = "MB"
LOCATION_CODE = "01  "
LOCATION_NAME = "Winnipeg-KB"

SENDER_NAME = "Courtready"
REPLY_TO = "tom@courtready.ca"
PHONE = "(204) 945-0344"
TOOL_URL = "https://courtready.ca/winnipeg-court-dates-finder/"
REGISTRY_URL = "https://web43.gov.mb.ca/Registry/AvailableCourtDates"
FOOTER = "Courtready.ca's Winnipeg Court Dates Finder"

MONTHS = {
    "JAN": "January", "FEB": "February", "MAR": "March", "APR": "April",
    "MAY": "May", "JUN": "June", "JUL": "July", "AUG": "August",
    "SEP": "September", "OCT": "October", "NOV": "November", "DEC": "December",
}

KEEP = {"ISO", "I.S.O.", "MVA", "AM", "PM", "KB", "BDO", "MNP"}
MINOR = {"OF", "TO", "THE", "AND", "OR", "FOR", "IN", "ON", "AT", "A", "AN",
         "BY", "WITH", "FROM", "INTO", "PER"}


# --------------------------------------------------------------- helpers


def env(name, default=None):
    v = os.environ.get(name, "").strip()
    return v if v else default


def tidy(name):
    """Same rules as the website, so the email reads how the page did."""
    import re

    def fix(m):
        tok = m.group(0)
        ordinal = re.match(r"^(\d+)(ST|ND|RD|TH)$", tok, re.I)
        if ordinal:
            return ordinal.group(1) + ordinal.group(2).lower()
        up = tok.upper()
        if up in KEEP:
            return up
        if any(c.isdigit() for c in tok):
            return tok
        if m.start() > 0 and up in MINOR:
            return tok.lower()
        return tok[:1].upper() + tok[1:].lower()

    out = re.sub(r"[A-Za-z0-9.]+", fix, str(name))
    return re.sub(r"'S\b", "'s", out)


def label_for(sub):
    """'Small Claims Winnipeg - 1st App (9:00AM), Option B'"""
    name = tidy(sub.get("hearing_name") or "your hearing type")
    opt = (sub.get("option_label") or "").strip()
    return f"{name}, {opt}" if opt else name


def pretty_date(s):
    """'06-Aug-2026' -> '6 August 2026'."""
    parts = str(s).split("-")
    if len(parts) != 3:
        return str(s)
    month = MONTHS.get(parts[1].upper())
    if not month:
        return str(s)
    try:
        return f"{int(parts[0])} {month} {parts[2]}"
    except ValueError:
        return str(s)


def pretty_day(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%-d %B %Y")
    except (ValueError, TypeError):
        return ""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ data


def load_latest():
    if not os.path.exists(LATEST):
        print(f"No {LATEST}. Nothing to do.")
        return None

    with open(LATEST) as f:
        data = json.load(f)

    stamp = data.get("scraped_at")
    if not stamp:
        print("latest.json has no timestamp. Refusing to act on it.")
        return None

    age = (datetime.now(WINNIPEG) - datetime.fromisoformat(stamp)).total_seconds() / 60
    if age > MAX_AGE_MINUTES:
        print(f"latest.json is {age:.0f} minutes old, limit is {MAX_AGE_MINUTES}.")
        print("The last scrape probably failed. Not sending anything.")
        return None

    return data


def categories_with_dates(data):
    out = {}
    for r in data.get("results", []):
        if r.get("status") == "OK" and r.get("slots_total"):
            out[str(r.get("code", "")).strip()] = r
    return out


class Store:
    def __init__(self, url, key):
        self.url = url
        self.head = {"apikey": key, "Authorization": f"Bearer {key}"}

    def active(self):
        r = requests.get(
            f"{self.url}/rest/v1/court_alerts",
            headers=self.head,
            params={
                "status": "eq.active",
                "jurisdiction": f"eq.{JURISDICTION}",
                "location_code": f"eq.{LOCATION_CODE}",
                "select": "*",
                "order": "created_at.asc",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def patch(self, row_id, body):
        r = requests.patch(
            f"{self.url}/rest/v1/court_alerts",
            headers={**self.head, "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            params={"id": f"eq.{row_id}"},
            json=body,
            timeout=30,
        )
        r.raise_for_status()


# ----------------------------------------------------------------- email


def confirmation_email(sub):
    label = label_for(sub)
    subject = f"Alert confirmed: {label}"

    text = (
        f"This confirms you have signed up for an alert for {label} at the "
        f"Winnipeg Court of King's Bench.\n\n"
        f"We will email you once, from this address, when that category next "
        f"has available dates. This is not a subscription and you will not "
        f"hear from us about anything else.\n\n"
        f"If you did not sign up for this, reply to this email and we will "
        f"remove you.\n\n"
        f"Questions: Tom Macintosh Zheng, {REPLY_TO}\n\n"
        f"---\n{FOOTER}\n{TOOL_URL}\n"
    )

    html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        f'line-height:1.6;color:#2f2f2f;max-width:520px">'
        f'<p style="margin:0 0 16px">This confirms you have signed up for an '
        f'alert for <strong>{esc(label)}</strong> at the Winnipeg Court of '
        f"King's Bench.</p>"
        f'<p style="margin:0 0 16px">We will email you once, from this '
        f'address, when that category next has available dates. This is not a '
        f'subscription and you will not hear from us about anything else.</p>'
        f'<p style="margin:0 0 16px">If you did not sign up for this, reply to '
        f'this email and we will remove you.</p>'
        f'<p style="margin:0 0 16px">Questions: Tom Macintosh Zheng, '
        f'<a href="mailto:{REPLY_TO}" style="color:#c87040">{REPLY_TO}</a></p>'
        f'<hr style="border:none;border-top:1px solid #e5e1db;margin:20px 0">'
        f'<p style="margin:0;font-size:12.5px;color:#857a72">'
        f'<a href="{TOOL_URL}" style="color:#857a72">{FOOTER}</a></p></div>'
    )

    return subject, text, html


def alert_email(sub, cat):
    label = label_for(sub)
    first = pretty_date(cat.get("earliest_date"))
    total = cat.get("slots_total", 0)
    link = cat.get("url") or REGISTRY_URL
    word = "date" if total == 1 else "dates"

    since = pretty_day(sub.get("created_at"))
    asked = f" on {since}" if since else ""

    subject = f"Hearing dates available: {label}"

    text = (
        f"{label} now has {total} available {word} at the Winnipeg Court of "
        f"King's Bench.\n\n"
        f"The earliest is {first}.\n\n"
        f"See the full list:\n{TOOL_URL}\n\n"
        f"Check the official registry:\n{link}\n\n"
        f"To book, contact the Manitoba Court Registry on {PHONE}. Courtready "
        f"cannot book a date for you, and dates can be taken by someone else "
        f"at any time, so confirm before you rely on this.\n\n"
        f"You asked us to tell you when this category next had a date{asked}. "
        f"This is that one message. You will not hear from us again.\n\n"
        f"---\n{FOOTER}\n{TOOL_URL}\n"
    )

    html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        f'line-height:1.6;color:#2f2f2f;max-width:520px">'
        f'<p style="margin:0 0 14px"><strong>{esc(label)}</strong> now has '
        f'{total} available {word} at the Winnipeg Court of King\'s Bench.</p>'
        f'<p style="margin:0 0 18px;font-size:20px;color:#c87040">'
        f'<strong>The earliest is {esc(first)}.</strong></p>'
        f'<p style="margin:0 0 16px">'
        f'<a href="{TOOL_URL}" style="color:#c87040">See the full list</a><br>'
        f'<a href="{link}" style="color:#c87040">Check the official registry'
        f'</a></p>'
        f'<p style="margin:0 0 16px">To book, contact the Manitoba Court '
        f'Registry on <strong>{PHONE}</strong>. Courtready cannot book a date '
        f'for you, and dates can be taken by someone else at any time, so '
        f'confirm before you rely on this.</p>'
        f'<hr style="border:none;border-top:1px solid #e5e1db;margin:20px 0">'
        f'<p style="margin:0 0 8px;font-size:12.5px;color:#857a72">You asked '
        f'us to tell you when this category next had a date{asked}. This is '
        f'that one message. You will not hear from us again.</p>'
        f'<p style="margin:0;font-size:12.5px;color:#857a72">'
        f'<a href="{TOOL_URL}" style="color:#857a72">{FOOTER}</a></p></div>'
    )

    return subject, text, html


class Postmark:
    def __init__(self, token, sender, stream):
        self.token = token
        self.sender = sender
        self.stream = stream

    def send(self, to, subject, text, html):
        """Returns (ok, permanent, note)."""
        try:
            r = requests.post(
                "https://api.postmarkapp.com/email",
                headers={
                    "X-Postmark-Server-Token": self.token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "From": f"{SENDER_NAME} <{self.sender}>",
                    "To": to,
                    "ReplyTo": REPLY_TO,
                    "Subject": subject,
                    "TextBody": text,
                    "HtmlBody": html,
                    "MessageStream": self.stream,
                    "Headers": [{
                        "Name": "List-Unsubscribe",
                        "Value": f"<mailto:{REPLY_TO}?subject=unsubscribe>",
                    }],
                },
                timeout=30,
            )
        except requests.RequestException as e:
            return False, False, f"network: {e}"

        if r.status_code == 200:
            return True, False, "sent"

        try:
            payload = r.json()
        except ValueError:
            payload = {}

        code = payload.get("ErrorCode", 0)
        msg = payload.get("Message", r.text[:200])
        # 300 bad address, 406 inactive recipient. Retrying never works.
        return False, code in (300, 406), f"postmark {r.status_code} code {code}: {msg}"


# ------------------------------------------------------------------ main


def main():
    sb_url = env("SUPABASE_URL")
    sb_key = env("SUPABASE_SERVICE_KEY")
    pm_token = env("POSTMARK_TOKEN")
    pm_from = env("POSTMARK_FROM")
    pm_stream = env("POSTMARK_STREAM", "outbound")
    dry = env("ALERTS_DRY_RUN", "") == "1"

    if not sb_url or not sb_key:
        print("Alerts are not configured. Missing Supabase settings.")
        return
    if not dry and (not pm_token or not pm_from):
        print("Alerts are not configured. Missing Postmark settings.")
        return

    data = load_latest()
    if data is None:
        return

    available = categories_with_dates(data)
    print(f"{len(available)} hearing types currently have dates.")

    store = Store(sb_url, sb_key)
    try:
        subs = store.active()
    except requests.RequestException as e:
        print(f"Could not read subscriptions: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"{len(subs)} people waiting in {LOCATION_NAME}.")
    if not subs:
        return

    cutoff = datetime.now(WINNIPEG) - timedelta(minutes=CONFIRMATION_BACKSTOP_MINUTES)

    # Anything the Edge Function failed to confirm, older than the cutoff.
    missing = []
    for s in subs:
        if s.get("confirmation_sent_at"):
            continue
        try:
            if datetime.fromisoformat(s["created_at"]) < cutoff:
                missing.append(s)
        except (ValueError, KeyError):
            continue

    # Anyone whose category now has dates.
    due = [s for s in subs
           if available.get(str(s.get("hearing_code", "")).strip())]

    print(f"{len(missing)} missing a confirmation, {len(due)} ready to alert.")

    if dry:
        for s in missing:
            print(f"  DRY RUN confirmation to {s['email']} for {s['hearing_code']}")
        for s in due:
            print(f"  DRY RUN alert to {s['email']} for {s['hearing_code']}")
        print("\nDry run. Nothing sent.")
        return

    if not missing and not due:
        return

    pm = Postmark(pm_token, pm_from, pm_stream)
    sent = 0
    failed = 0

    for sub in missing:
        subject, text, html = confirmation_email(sub)
        ok, permanent, note = pm.send(sub["email"], subject, text, html)
        stamp = datetime.now(WINNIPEG).isoformat()
        if ok:
            store.patch(sub["id"], {"confirmation_sent_at": stamp})
            print(f"  late confirmation to {sub['email']}")
            sent += 1
        else:
            failed += 1
            if permanent:
                store.patch(sub["id"], {"status": "failed", "notified_at": stamp,
                                        "note": note[:500]})
                print(f"  giving up on {sub['email']}: {note}", file=sys.stderr)
            else:
                print(f"  will retry {sub['email']}: {note}", file=sys.stderr)

    for sub in due:
        cat = available[str(sub["hearing_code"]).strip()]
        subject, text, html = alert_email(sub, cat)
        ok, permanent, note = pm.send(sub["email"], subject, text, html)
        stamp = datetime.now(WINNIPEG).isoformat()
        if ok:
            store.patch(sub["id"], {"status": "sent", "notified_at": stamp})
            print(f"  alerted {sub['email']} about {sub['hearing_code']}")
            sent += 1
        else:
            failed += 1
            if permanent:
                store.patch(sub["id"], {"status": "failed", "notified_at": stamp,
                                        "note": note[:500]})
                print(f"  giving up on {sub['email']}: {note}", file=sys.stderr)
            else:
                print(f"  will retry {sub['email']}: {note}", file=sys.stderr)

    print(f"\nSent {sent}, failed {failed}.")


if __name__ == "__main__":
    main()
