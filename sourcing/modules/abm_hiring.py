"""abm-hiring: job postings for ABM roles (titles or specs containing ABM or
Account-Based Marketing) at UK mid-market B2B tech companies.

A company hiring for ABM is budgeting for exactly the motion Sentrada serves,
right now — which is why this signal ages fast (90 days in scoring.json).
Postings are company-level signals: the person who owns the budget gets
filled in by Search-API resolution or a manual find.

Scrape path: the Adzuna UK jobs API (free tier; ADZUNA_APP_ID +
ADZUNA_APP_KEY in env or .env). Job boards themselves (LinkedIn, Indeed)
are not scraped: LinkedIn automation is banned outright and Indeed blocks
it; paste their postings in instead.
"""

import json
import re
import urllib.parse
import urllib.request

NAME = "abm-hiring"
TEMPLATE = "extract_job_posting.md"
LISTING_SOURCES = []  # API-backed: see api_ingest. Paste path always works.

QUERIES = ["account based marketing", "ABM manager"]

_ABM_RE = re.compile(r"\babm\b|account.based marketing", re.I)


def _relevant(title, description):
    return bool(_ABM_RE.search(title or "") or _ABM_RE.search(description or ""))


def api_ingest(config, load_env_key):
    """Pull recent UK ABM postings from Adzuna. Returns extraction dicts, or
    None after printing instructions when no keys are configured."""
    app_id = load_env_key("ADZUNA_APP_ID")
    app_key = load_env_key("ADZUNA_APP_KEY")
    az = config.get("adzuna", {})
    if not (app_id and app_key):
        print("[abm-hiring] no ADZUNA_APP_ID / ADZUNA_APP_KEY in env or .env.\n"
              "  Adzuna's jobs API is free (developer.adzuna.com): register, "
              "add both keys, re-run.\n"
              "  Until then use the paste path with any job ad:\n"
              "    python sourcing/sourcing.py paste --module abm-hiring "
              "--url <posting-url> --file <ad.txt>")
        return None
    out, seen = [], set()
    for q in QUERIES:
        params = urllib.parse.urlencode({
            "app_id": app_id, "app_key": app_key,
            "what_phrase": q,
            "results_per_page": az.get("results_per_page", 50),
            "max_days_old": az.get("max_days_old", 90),
            "content-type": "application/json"})
        url = az.get("base_url",
                     "https://api.adzuna.com/v1/api/jobs/gb/search/1") \
            + "?" + params
        print(f"[abm-hiring] adzuna query: {q!r}")
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        for job in body.get("results", []):
            company = (job.get("company") or {}).get("display_name") or ""
            title = job.get("title") or ""
            if not company or not _relevant(title, job.get("description")):
                continue
            key = (company.lower(), title.lower())
            if key in seen:
                continue
            seen.add(key)
            location = (job.get("location") or {}).get("display_name")
            out.append({
                "company": company,
                "person": None, "role": None,
                "quote_or_evidence": f"Hiring: {title}"
                                     + (f" ({location})" if location else "")
                                     + " — ABM budget is live",
                "source_url": job.get("redirect_url") or "",
                "date": (job.get("created") or "")[:10] or None,
                "location": location,
                "sector": job.get("category", {}).get("label"),
                "size": None,
            })
    print(f"[abm-hiring] {len(out)} relevant posting(s)")
    return out
