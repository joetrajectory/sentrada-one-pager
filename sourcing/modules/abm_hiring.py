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

import re

from . import _adzuna

NAME = "abm-hiring"
TEMPLATE = "extract_job_posting.md"
LISTING_SOURCES = []  # API-backed: see api_ingest. Paste path always works.

QUERIES = ["account based marketing", "ABM manager"]

_ABM_RE = re.compile(r"\babm\b|account.based marketing", re.I)


def _relevant(title, description):
    return bool(_ABM_RE.search(title) or _ABM_RE.search(description))


def api_ingest(config, load_env_key):
    return _adzuna.api_ingest(config, load_env_key, NAME, QUERIES, _relevant,
                              "ABM budget is live")
