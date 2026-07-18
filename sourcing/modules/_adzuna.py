"""Shared Adzuna UK jobs API client for the job-posting signal modules
(abm-hiring, abm-tooling-jd, enterprise-ae-hiring).

Free tier keys from developer.adzuna.com: ADZUNA_APP_ID + ADZUNA_APP_KEY in
env or .env. Job boards themselves (LinkedIn, Indeed) are never scraped:
LinkedIn automation is banned outright and Indeed blocks it; their postings
go through the paste path instead.
"""

import json
import urllib.parse
import urllib.request


def api_ingest(config, load_env_key, module_name, queries, relevant_fn,
               evidence_suffix):
    """Pull recent UK postings for `queries`, keep those `relevant_fn`
    accepts, return extraction dicts. Returns None (after printing
    instructions) when no keys are configured."""
    app_id = load_env_key("ADZUNA_APP_ID")
    app_key = load_env_key("ADZUNA_APP_KEY")
    az = config.get("adzuna", {})
    if not (app_id and app_key):
        print(f"[{module_name}] no ADZUNA_APP_ID / ADZUNA_APP_KEY in env or "
              ".env.\n  Adzuna's jobs API is free (developer.adzuna.com): "
              "register, add both keys, re-run.\n"
              "  Until then use the paste path with any job ad:\n"
              f"    python sourcing/sourcing.py paste --module {module_name} "
              "--url <posting-url> --file <ad.txt>")
        return None
    out, seen = [], set()
    for q in queries:
        params = urllib.parse.urlencode({
            "app_id": app_id, "app_key": app_key,
            "what_phrase": q,
            "results_per_page": az.get("results_per_page", 50),
            "max_days_old": az.get("max_days_old", 90),
            "content-type": "application/json"})
        url = az.get("base_url",
                     "https://api.adzuna.com/v1/api/jobs/gb/search/1") \
            + "?" + params
        print(f"[{module_name}] adzuna query: {q!r}")
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        for job in body.get("results", []):
            company = (job.get("company") or {}).get("display_name") or ""
            title = job.get("title") or ""
            if not company or not relevant_fn(title,
                                              job.get("description") or ""):
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
                                     + f" — {evidence_suffix}",
                "source_url": job.get("redirect_url") or "",
                "date": (job.get("created") or "")[:10] or None,
                "location": location,
                "sector": (job.get("category") or {}).get("label"),
                "size": None,
            })
    print(f"[{module_name}] {len(out)} relevant posting(s)")
    return out
