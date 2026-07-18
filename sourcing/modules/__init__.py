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

from . import abm_hiring
from . import abm_tooling_jd
from . import enterprise_ae_hiring
from . import gifting_case_studies
from . import new_leader_abm_history

REGISTRY = {
    gifting_case_studies.NAME: gifting_case_studies,
    abm_hiring.NAME: abm_hiring,
    new_leader_abm_history.NAME: new_leader_abm_history,
    abm_tooling_jd.NAME: abm_tooling_jd,
    enterprise_ae_hiring.NAME: enterprise_ae_hiring,
}

# All five specced modules are built. A new module is a file here exposing
# NAME, TEMPLATE, LISTING_SOURCES + discover(), or api_ingest() for
# API-backed sources; both input paths come free from sourcing.py.
PLANNED = {}
