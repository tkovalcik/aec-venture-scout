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


def scrape_cemex() -> list[dict]:
    """Scrape Cemex Ventures homepage for recent deals / portfolio mentions."""
    log(f"Fetching {CEMEX_URL}")
    resp = requests.get(CEMEX_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    findings: list[dict] = []
    seen_names: set[str] = set()

    deal_section = None
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = heading.get_text(strip=True).lower()
        if "recent deal" in text or "latest deal" in text or "portfolio" in text or "news" in text:
            deal_section = heading.find_parent()
            break

    search_scope = deal_section if deal_section else soup

    for article in search_scope.find_all(["article", "div", "li"], limit=400):
        text = article.get_text(" ", strip=True)
        if not text or len(text) < 20 or len(text) > 800:
            continue

        funding_hit = re.search(
            r"(raised|secures?|closes?|announces?|funding|seed|series\s+[a-e]|million|\$\d)",
            text,
            re.IGNORECASE,
        )
        if not funding_hit:
            continue

        link = article.find("a", href=True)
        if not link:
            continue

        name = link.get_text(strip=True)
        if not name or len(name) < 2 or len(name) > 80:
            continue
        if name.lower() in {"read more", "learn more", "see more", "view all", "news"}:
            continue

        key = name.strip().lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        href = link["href"]
        if href.startswith("/"):
            href = CEMEX_URL.rstrip("/") + href

        findings.append({
            "name": name.strip(),
            "summary": text[:400],
            "source_url": href,
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
