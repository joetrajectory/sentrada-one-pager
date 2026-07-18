"""Signal module registry.

Each built module is a python file in this package exposing:

  NAME              the signal type string (matches scoring.json points key)
  TEMPLATE          extraction prompt template in sourcing/templates/
  LISTING_SOURCES   default listing-page URLs for the scrape path
  discover(fetch_fn, sources)  ->  list of document URLs to parse

Every module gets both input paths for free from sourcing.py: the scrape path
(discover + fetch + parse) and the paste path (a job ad, case study body or
announcement pasted in, parsed to the same candidate format). Scraping
reliability varies per source; the paste path is the day-one guarantee.
"""

from . import gifting_case_studies

REGISTRY = {
    gifting_case_studies.NAME: gifting_case_studies,
}

# Specced but not built yet. Build order is deliberate: gifting-case-studies
# proves the spine first (see the sourcing section of CLAUDE.md).
PLANNED = {
    "abm-hiring": ("job postings for ABM roles (ABM / Account-Based Marketing "
                   "in title or spec) at UK mid-market B2B tech companies"),
    "new-leader-abm-history": ("CMO / VP Marketing / VP Sales new in role "
                               "within 90 days with ABM in their history; "
                               "press releases and joins-as announcements"),
    "abm-tooling-jd": ("job specs mentioning 6sense, Demandbase, Terminus, "
                       "RollWorks or Folloze"),
    "enterprise-ae-hiring": ("Enterprise Account Executive postings at UK "
                             "mid-market B2B tech companies"),
}
