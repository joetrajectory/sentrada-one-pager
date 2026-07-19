"""new-leader-abm-history: CMO, VP Marketing or VP Sales new in role within
90 days whose history includes ABM.

A new leader with ABM in their past re-runs their old playbook in the first
two quarters — budget forms exactly then, which is why this signal ages in
90 days (scoring.json). Scrapeable sources are press releases and joins-as
announcements; the scrape path walks Google News RSS queries (plain XML,
fetchable where egress allows) and follows each item to the announcement.
The paste path takes any announcement text. These are person-level records:
the new leader IS the target.
"""

import re

NAME = "new-leader-abm-history"
TEMPLATE = "extract_leader_announcement.md"

# Google News RSS: UK edition, appointment phrasings. Each query is one
# listing source; discover() parses the item links out of the XML.
_RSS = ("https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en")
LISTING_SOURCES = [
    _RSS.format(q="%22joins%20as%20CMO%22%20OR%20%22joins%20as%20chief%20"
                  "marketing%20officer%22"),
    _RSS.format(q="%22appointed%20CMO%22%20OR%20%22appoints%20chief%20"
                  "marketing%20officer%22"),
    _RSS.format(q="%22joins%20as%20VP%20of%20Marketing%22%20OR%20%22joins%20"
                  "as%20VP%20Marketing%22"),
    _RSS.format(q="%22joins%20as%20VP%20of%20Sales%22%20OR%20%22appoints%20"
                  "VP%20of%20Sales%22"),
]

_LINK_RE = re.compile(r"<link>(https?://[^<]+)</link>")


def discover(fetch_fn, sources):
    """Parse announcement links out of the RSS feeds. Google News links
    redirect to the publisher; urllib follows them at fetch time."""
    found, seen = [], set()
    for feed in sources:
        xml = fetch_fn(feed)
        for url in _LINK_RE.findall(xml):
            # Skip only the feed's own self/search links. Modern item links
            # are news.google.com/rss/articles/... and must pass through.
            if url == feed or "/rss/search" in url:
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append(url)
    return found
