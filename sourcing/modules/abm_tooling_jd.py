"""abm-tooling-jd: job specs mentioning 6sense, Demandbase, Terminus,
RollWorks or Folloze.

A company writing ABM platforms into a job spec has bought the tooling and
is staffing the motion — weaker than hiring an ABM role outright (tier
three), but cheap corroboration that stacks on other signals. Postings are
company-level records; resolution finds the budget owner.
"""

import re

from . import _adzuna

NAME = "abm-tooling-jd"
TEMPLATE = "extract_job_posting.md"
LISTING_SOURCES = []  # API-backed; paste path always works.

TOOLS = ["6sense", "Demandbase", "Terminus", "RollWorks", "Folloze"]

_TOOLS_RE = re.compile("|".join(re.escape(t) for t in TOOLS), re.I)


def _relevant(title, description):
    return bool(_TOOLS_RE.search(title) or _TOOLS_RE.search(description))


def api_ingest(config, load_env_key):
    return _adzuna.api_ingest(
        config, load_env_key, NAME, TOOLS, _relevant,
        "ABM tooling named in the spec")
