"""
AEC Venture Scout
Scrapes Cemex Ventures for new funded AEC startups, searches for relevant jobs
via SerpApi, locates LinkedIn contacts, and emails an HTML digest.
"""

import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "state.json"
CEMEX_URL = "https://www.cemexventures.com/"
SERPAPI_URL = "https://serpapi.com/search.json"
RECIPIENT_EMAIL = "tomasko.kovalcik@gmail.com"

ROLE_KEYWORDS = [
    "machine learning",
    "computer vision",
    "data engineer",
    "data science",
    "data scientist",
    "pyspark",
    "yolo",
    "ml engineer",
    "mlops",
]

LOCATION_KEYWORDS = [
    "san francisco",
    "sf bay",
    "bay area",
    "oakland",
    "remote",
    "united states",
    "usa",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json corrupted, reinitializing")
    return {"startups": {}, "jobs_seen": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


NAME_FROM_HEADLINE = re.compile(
    r"^(.+?)\s+(?:Raises|Secures|Closes|Announces|Lands|Receives|Gets|Scores|Nets)\b",
    re.IGNORECASE,
)
FUNDING_HEADLINE = re.compile(
    r"(raises?|secures?|closes?|announces?|lands?|receives?|funding|series\s+[a-e]\b|seed\b|\$\s?\d)",
    re.IGNORECASE,
)
DATE_HEADLINE = re.compile(
    r"^(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*\d{4}$",
    re.IGNORECASE,
)


def scrape_cemex() -> list[dict]:
    """Scrape the Cemex Ventures homepage 'Recent deals' section.

    Deal headlines appear as <h4> tags like "Zero Homes Raises $16.8M in
    Series A Funding", preceded by a date <h4> and accompanied by a link
    to the source article in the surrounding container.
    """
    log(f"Fetching {CEMEX_URL}")
    resp = requests.get(CEMEX_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    findings: list[dict] = []
    seen_names: set[str] = set()

    for h4 in soup.find_all("h4"):
        title = h4.get_text(strip=True)
        if not title or DATE_HEADLINE.match(title):
            continue
        if not FUNDING_HEADLINE.search(title):
            continue

        m = NAME_FROM_HEADLINE.match(title)
        if not m:
            continue
        name = m.group(1).strip().strip('"\u201c\u201d')
        if not (2 <= len(name) <= 60):
            continue

        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        prev_h4 = h4.find_previous("h4")
        date_str = prev_h4.get_text(strip=True) if prev_h4 and DATE_HEADLINE.match(prev_h4.get_text(strip=True)) else ""

        source_url = ""
        scope = h4
        for _ in range(6):
            scope = scope.find_parent() if scope else None
            if not scope:
                break
            for a in scope.find_all("a", href=True):
                href = a["href"]
                if "cemexventures.com" in href:
                    continue
                if href.startswith("#") or href.startswith("mailto:"):
                    continue
                source_url = href
                break
            if source_url:
                break

        findings.append({
            "name": name,
            "headline": title,
            "date": date_str,
            "summary": f"{date_str}: {title}" if date_str else title,
            "source_url": source_url or CEMEX_URL,
        })

    log(f"Cemex scrape found {len(findings)} candidate startups")
    return findings[:25]


def serpapi_search(query: str, num: int = 10) -> list[dict]:
    """Run a SerpApi Google search and return organic results."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        log("SERPAPI_KEY missing, skipping search")
        return []
    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": num,
        "hl": "en",
    }
    try:
        r = requests.get(SERPAPI_URL, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("organic_results", []) or []
    except requests.RequestException as e:
        log(f"SerpApi error for '{query}': {e}")
        return []


def find_jobs(startup: str) -> list[dict]:
    """Search for relevant technical roles at the given startup."""
    role_clause = " OR ".join(f'"{r}"' for r in [
        "machine learning", "computer vision", "data engineer",
        "data scientist", "PySpark", "YOLO"
    ])
    queries = [
        f'"{startup}" careers ({role_clause})',
        f'"{startup}" ("machine learning" OR "computer vision" OR "data engineer") (remote OR "Bay Area" OR "San Francisco" OR Oakland)',
    ]

    jobs: list[dict] = []
    seen_links: set[str] = set()

    for q in queries:
        for result in serpapi_search(q, num=10):
            link = result.get("link", "")
            title = result.get("title", "")
            snippet = result.get("snippet", "") or ""
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            blob = f"{title} {snippet}".lower()
            if not any(k in blob for k in ROLE_KEYWORDS):
                continue

            location_match = any(k in blob for k in LOCATION_KEYWORDS)
            jobs.append({
                "title": title,
                "link": link,
                "snippet": snippet[:260],
                "priority": location_match,
            })

    jobs.sort(key=lambda j: (not j["priority"], j["title"]))
    return jobs[:5]


def find_linkedin_contacts(startup: str) -> list[dict]:
    """Dork LinkedIn profiles for founders / CTOs / recruiters."""
    query = f'site:linkedin.com/in/ "{startup}" (founder OR CTO OR recruiter)'
    contacts: list[dict] = []
    seen_links: set[str] = set()

    for result in serpapi_search(query, num=10):
        link = result.get("link", "")
        if not link or "linkedin.com/in/" not in link:
            continue
        if link in seen_links:
            continue
        seen_links.add(link)
        contacts.append({
            "name": result.get("title", "").split(" - ")[0].strip(),
            "headline": result.get("title", ""),
            "snippet": result.get("snippet", "")[:220],
            "link": link,
        })
        if len(contacts) >= 3:
            break
    return contacts


def render_email(digest: list[dict]) -> str:
    parts = [
        "<html><body style=\"font-family:Helvetica,Arial,sans-serif;line-height:1.5;color:#222;\">",
        f"<h2>AEC Venture Scout — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</h2>",
        f"<p>{len(digest)} new funded startup(s) detected at Cemex Ventures.</p>",
    ]
    for item in digest:
        parts.append("<hr>")
        parts.append(f"<h3>{item['startup']['name']}</h3>")
        parts.append(f"<p><em>{item['startup']['summary']}</em></p>")
        parts.append(f"<p>Source: <a href=\"{item['startup']['source_url']}\">{item['startup']['source_url']}</a></p>")

        parts.append("<h4>Relevant roles</h4>")
        if item["jobs"]:
            parts.append("<ul>")
            for j in item["jobs"]:
                tag = " <strong>[Bay Area / Remote]</strong>" if j["priority"] else ""
                parts.append(
                    f"<li><a href=\"{j['link']}\">{j['title']}</a>{tag}<br>"
                    f"<span style=\"color:#555;font-size:0.9em;\">{j['snippet']}</span></li>"
                )
            parts.append("</ul>")
        else:
            parts.append("<p style=\"color:#888;\">No matching roles found this run.</p>")

        parts.append("<h4>LinkedIn contacts</h4>")
        if item["contacts"]:
            parts.append("<ul>")
            for c in item["contacts"]:
                parts.append(
                    f"<li><a href=\"{c['link']}\">{c['headline'] or c['name']}</a><br>"
                    f"<span style=\"color:#555;font-size:0.9em;\">{c['snippet']}</span></li>"
                )
            parts.append("</ul>")
        else:
            parts.append("<p style=\"color:#888;\">No LinkedIn contacts surfaced.</p>")

    parts.append("</body></html>")
    return "".join(parts)


def send_email(html: str, subject: str) -> None:
    sender = os.environ.get("SENDER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not sender or not password:
        log("SENDER_EMAIL or EMAIL_PASSWORD missing, skipping email send")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(sender, password)
        server.sendmail(sender, [RECIPIENT_EMAIL], msg.as_string())
    log(f"Email delivered to {RECIPIENT_EMAIL}")


def main() -> int:
    state = load_state()
    known_startups: dict = state.setdefault("startups", {})
    jobs_seen: list = state.setdefault("jobs_seen", [])
    jobs_seen_set = set(jobs_seen)

    try:
        candidates = scrape_cemex()
    except requests.RequestException as e:
        log(f"Cemex scrape failed: {e}")
        return 1

    digest: list[dict] = []

    for startup in candidates:
        key = startup["name"].lower().strip()
        already_known = key in known_startups

        jobs = find_jobs(startup["name"])
        new_jobs = [j for j in jobs if j["link"] not in jobs_seen_set]

        if already_known and not new_jobs:
            continue

        contacts = find_linkedin_contacts(startup["name"]) if not already_known else []

        digest.append({"startup": startup, "jobs": new_jobs or jobs, "contacts": contacts})

        known_startups[key] = {
            "name": startup["name"],
            "first_seen": known_startups.get(key, {}).get("first_seen", datetime.utcnow().isoformat()),
            "last_seen": datetime.utcnow().isoformat(),
            "source_url": startup["source_url"],
        }
        for j in jobs:
            jobs_seen_set.add(j["link"])

    state["jobs_seen"] = sorted(jobs_seen_set)
    state["last_run"] = datetime.utcnow().isoformat()
    save_state(state)

    if not digest:
        log("No new startups or roles to report this run.")
        return 0

    html = render_email(digest)
    subject = f"AEC Scout: {len(digest)} new Cemex-funded startup(s) w/ roles"
    try:
        send_email(html, subject)
    except Exception as e:
        log(f"Email send failed: {e}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
