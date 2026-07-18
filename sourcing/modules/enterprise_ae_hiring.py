"""enterprise-ae-hiring: Enterprise Account Executive postings at UK
mid-market B2B tech companies.

Enterprise AE headcount means big-ticket outbound targets are being chased —
exactly when one-of-one physical outreach earns its keep. Tier three: cheap
corroboration, stacks with the marketing-side signals. Company-level
records; resolution finds the budget owner (usually the VP Sales or CMO).
"""

import re

from . import _adzuna

NAME = "enterprise-ae-hiring"
TEMPLATE = "extract_job_posting.md"
LISTING_SOURCES = []  # API-backed; paste path always works.

QUERIES = ["enterprise account executive"]

_EAE_RE = re.compile(r"enterprise\s+(account\s+executive|ae\b|sales\s+executive)",
                     re.I)


def _relevant(title, description):
    return bool(_EAE_RE.search(title) or _EAE_RE.search(description))


def api_ingest(config, load_env_key):
    return _adzuna.api_ingest(
        config, load_env_key, NAME, QUERIES, _relevant,
        "Enterprise AE headcount going in")
