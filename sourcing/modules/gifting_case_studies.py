"""gifting-case-studies: published customer stories on Sendoso, Reachdesk,
Alyce and similar corporate-gifting sites.

The strongest tier-one signal: these companies already pay for physical
outreach, and the case study names the person who owns the budget. The scrape
path walks the vendors' listing pages; the paste path takes a case study URL
or body. Both produce the same candidate shape (company, person, role,
evidence quote, source URL).
"""

import re

NAME = "gifting-case-studies"
TEMPLATE = "extract_case_study.md"

LISTING_SOURCES = [
    "https://sendoso.com/success-stories",
    "https://www.reachdesk.com/customer-stories",
    # Alyce was folded into Sendoso; kept for any surviving stories.
    "https://www.alyce.com/customers",
]

# Case-study detail pages across the vendor sites.
_LINK_RE = re.compile(
    r'href=["\']([^"\'#?]*/(?:success-stories|case-stud(?:y|ies)|'
    r'customer-stor(?:y|ies)|customers)/[^"\'#?]+)["\']', re.I)


def discover(fetch_fn, sources):
    """Walk the listing pages and return distinct case-study URLs."""
    found, seen = [], set()
    for listing in sources:
        html = fetch_fn(listing)
        base = re.match(r"https?://[^/]+", listing).group(0)
        for href in _LINK_RE.findall(html):
            url = href if href.startswith("http") else base + href
            url = url.rstrip("/")
            if url in seen or url.rstrip("/") == listing.rstrip("/"):
                continue
            seen.add(url)
            found.append(url)
    return found
