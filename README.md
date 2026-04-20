# AEC Venture Scout

Monitors [Cemex Ventures](https://www.cemexventures.com/) for newly funded AEC
startups, finds relevant technical roles (ML, CV, Data Engineer, PySpark, YOLO)
prioritizing SF Bay Area / Oakland / Remote, surfaces 2–3 LinkedIn contacts per
startup (founder / CTO / recruiter), and emails an HTML digest.

Runs 4x daily via GitHub Actions. State is persisted in `state.json` and
committed back to the repo so alerts are de-duplicated across runs.

## Required GitHub repository secrets

| Secret | Purpose |
| --- | --- |
| `SENDER_EMAIL` | Gmail address used as SMTP sender |
| `EMAIL_PASSWORD` | Gmail **App Password** (not your account password) |
| `SERPAPI_KEY` | SerpApi key for Google search |

## Local run

```bash
pip install -r requirements.txt
export SENDER_EMAIL=...
export EMAIL_PASSWORD=...
export SERPAPI_KEY=...
python scraper.py
```

## Files

- `scraper.py` — main pipeline (scrape → enrich → dedupe → email).
- `state.json` — persisted dedupe state; rewritten each run.
- `.github/workflows/scout.yml` — cron (`0 0,6,12,18 * * *`) + auto-commit of state.
