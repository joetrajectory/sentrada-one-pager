#!/usr/bin/env python3
"""
Sentrada sourcing pipeline.

Discovers, scores and shortlists Sentrada targets. Signal modules feed one
candidate store (sourcing/candidates.json, committed: containers are
ephemeral), a scorer ranks it from sourcing/scoring.json, Joe approves names,
and only approved names get enriched and desk-checked before handoff to the
chain as P1 candidates. Money and heavy effort only ever flow after Joe's
judgment, never before.

Every signal module has two input paths:
  scrape   python sourcing/sourcing.py ingest --module gifting-case-studies
  paste    python sourcing/sourcing.py paste --module gifting-case-studies \
               --url https://... --file story.txt
The paste path parses a job ad, case study or announcement into the same
candidate format, so every module works even where scraping is blocked or
brittle. No LinkedIn automation, ever.

Contact data returned by FullEnrich lands in sourcing/enriched.json, which is
GITIGNORED (personal contact data never goes on a code branch). The committed
store keeps only a found/not-found summary.

Model calls (freeform paste extraction) go through `claude -p` on the
logged-in subscription, exactly like the chain runner. Structured pastes and
everything else are free and offline.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

SOURCING_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SOURCING_DIR)
sys.path.insert(0, SOURCING_DIR)

from modules import REGISTRY, PLANNED  # noqa: E402

STORE_PATH = os.path.join(SOURCING_DIR, "candidates.json")
ENRICHED_PATH = os.path.join(SOURCING_DIR, "enriched.json")   # gitignored
SCORING_PATH = os.path.join(SOURCING_DIR, "scoring.json")
CONFIG_PATH = os.path.join(SOURCING_DIR, "config.json")
TEMPLATE_DIR = os.path.join(SOURCING_DIR, "templates")
RAW_DIR = os.path.join(SOURCING_DIR, "raw")                   # gitignored
RESEARCH_DIR = os.path.join(REPO_ROOT, "research")

STATUSES = ("new", "approved", "rejected", "exported")


# --- Small helpers (mirror the runner's) ------------------------------------

def die(msg):
    print(f"\n[halt] {msg}")
    sys.exit(1)


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "x"


def today():
    return datetime.date.today().isoformat()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    return json.loads(read_file(path))


def load_env_key(name):
    """Env first, then .env (KEY=VALUE lines), like the runner's secrets."""
    if os.environ.get(name):
        return os.environ[name].strip()
    env_path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(env_path):
        for line in read_file(env_path).splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_scoring():
    sc = load_json(SCORING_PATH, None)
    if not sc or "points" not in sc:
        die(f"scoring config missing or malformed: {SCORING_PATH}")
    return sc


def load_config():
    return load_json(CONFIG_PATH, {})


# --- Candidate store ---------------------------------------------------------

def load_store():
    return load_json(STORE_PATH, {"version": 1, "updated": "", "candidates": {}})


def save_store(store):
    store["updated"] = today()
    write_file(STORE_PATH, json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def candidate_id(person, company):
    if person:
        return f"{slugify(person)}-{slugify(company)}"
    # Company-level signal with no named person yet; the Search-API resolution
    # step (once approved and wired) fills the person in later.
    return f"co--{slugify(company)}"


def upsert(store, person, title, company, sector, size, signal):
    """One record per person+company; signals stack on it. Returns
    (id, created, signal_added)."""
    if not company:
        return None, False, False
    cid = candidate_id(person, company)
    cands = store["candidates"]
    rec = cands.get(cid)
    if rec is None:
        rec = {
            "id": cid,
            "person": person or None,
            "title": title or None,
            "company": company,
            "sector": sector or None,
            "size": size or None,
            "status": "new",
            "date_discovered": today(),
            "decided_date": None,
            "signals": [],
            "enrichment": None,
            "desk_check": None,
            "piece_slug": None,
        }
        cands[cid] = rec
        created = True
    else:
        created = False
        # Fill gaps, never overwrite what a human may have corrected.
        for key, val in (("title", title), ("sector", sector), ("size", size)):
            if val and not rec.get(key):
                rec[key] = val
    # Same signal type from the same source is the same signal.
    for s in rec["signals"]:
        if s["type"] == signal["type"] and s["source_url"] == signal["source_url"]:
            return cid, created, False
    rec["signals"].append(signal)
    return cid, created, True


def make_signal(sig_type, evidence, source_url, signal_date):
    return {
        "type": sig_type,
        "evidence": (evidence or "").strip(),
        "source_url": source_url or "",
        "signal_date": signal_date or None,
        "date_added": today(),
    }


# --- Scoring -----------------------------------------------------------------

def signal_points(sig, scoring):
    """Points for one signal. Signals older than max_signal_age_days score
    zero. Undated signals fall back to the date they entered the store (an
    undated case study is 'live' evidence the day we find it) when the config
    says so; otherwise undated scores zero."""
    points = scoring["points"].get(sig["type"], 0)
    max_age = int(scoring.get("max_signal_age_days", 90))
    eff = sig.get("signal_date")
    if not eff and scoring.get("undated_signals_use_discovery_date", True):
        eff = sig.get("date_added")
    if not eff:
        return 0
    # Tolerate YYYY or YYYY-MM by pinning to the earliest day they could mean.
    eff = {4: eff + "-01-01", 7: eff + "-01"}.get(len(eff), eff)
    try:
        eff_date = datetime.date.fromisoformat(eff)
    except ValueError:
        return 0
    age = (datetime.date.today() - eff_date).days
    return 0 if age > max_age else points


def score(rec, scoring):
    return sum(signal_points(s, scoring) for s in rec["signals"])


# --- Fetching (scrape path) --------------------------------------------------

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "en-GB,en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise RuntimeError(
            f"fetch failed for {url}: {exc}. If this container's egress policy "
            "blocks the host (403 at the proxy), use the paste path instead: "
            "sourcing.py paste --module <m> --url <url> --file <saved-page>")


def html_to_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# --- Extraction: structured paste blocks or claude -p ------------------------

# Keys accepted in a structured paste block (case-insensitive).
_BLOCK_KEYS = {"company": "company", "person": "person", "name": "person",
               "role": "role", "title": "role", "quote": "quote_or_evidence",
               "evidence": "quote_or_evidence",
               "quote/evidence": "quote_or_evidence", "url": "source_url",
               "source": "source_url", "date": "date", "sector": "sector",
               "size": "size"}


def parse_structured(text):
    """Parse KEY: value blocks separated by --- lines. Returns a list of
    extraction dicts, or [] if the text is not structured (no COMPANY: line)."""
    if not re.search(r"(?im)^\s*company\s*:", text):
        return []
    out = []
    for chunk in re.split(r"(?m)^\s*-{3,}\s*$", text):
        item, key = {}, None
        for line in chunk.splitlines():
            m = re.match(r"\s*([A-Za-z/ ]+?)\s*:\s*(.*)$", line)
            if m and m.group(1).strip().lower() in _BLOCK_KEYS:
                key = _BLOCK_KEYS[m.group(1).strip().lower()]
                item[key] = m.group(2).strip()
            elif key and line.strip():
                item[key] = (item[key] + " " + line.strip()).strip()
        if item.get("company"):
            for k in ("person", "role", "quote_or_evidence", "source_url",
                      "date", "sector", "size"):
                item.setdefault(k, None)
                if item[k] in ("", "null", "None", "-"):
                    item[k] = None
            out.append(item)
    return out


# A neutral cwd so the project CLAUDE.md never loads into extraction calls
# (same reasoning as the chain runner).
CLI_CWD = os.path.join(tempfile.gettempdir(), "sentrada_cli_cwd")
os.makedirs(CLI_CWD, exist_ok=True)

APPEND_SYSTEM = (
    "For this task, act strictly as the data extraction role described in the "
    "user message and produce exactly the output it specifies, including the "
    "fenced JSON block. Do not use any tools.")


def claude_extract(document, source_url, module, config):
    """Freeform paste/scrape extraction via `claude -p` (subscription pool)."""
    template = read_file(os.path.join(TEMPLATE_DIR, module.TEMPLATE))
    prompt = (template.replace("{{source_url}}", source_url or "unknown")
                      .replace("{{document}}", document[:60000]))
    model = config.get("extract_model", "sonnet")
    cmd = ["claude", "-p", "--output-format", "json", "--model", model,
           "--append-system-prompt", APPEND_SYSTEM]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, cwd=CLI_CWD, timeout=300)
    except FileNotFoundError:
        die("`claude` CLI not found. Freeform extraction needs the logged-in "
            "Claude CLI (like the chain runner); or paste in the structured "
            "COMPANY:/PERSON:/EVIDENCE: block format, which needs no model.")
    except subprocess.TimeoutExpired:
        die("claude -p timed out during extraction")
    if proc.returncode != 0:
        die(f"claude -p exited {proc.returncode}: "
            + (proc.stderr or proc.stdout)[:300])
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        die("claude -p did not return a JSON envelope: " + proc.stdout[:300])
    if env.get("is_error") or env.get("subtype") != "success":
        die("claude -p error: " + str(env.get("result", env))[:300])
    text = env.get("result") or ""
    m = re.search(r"```json\s*(\[.*?\])\s*```", text, re.S)
    if not m:
        die("extraction returned no fenced json array:\n" + text[:400])
    return json.loads(m.group(1))


def ingest_extractions(store, module_name, extractions, default_url,
                       default_date=None):
    """Common tail of both input paths: extraction dicts -> store upserts."""
    added, new_records = 0, 0
    for x in extractions:
        sig = make_signal(module_name, x.get("quote_or_evidence"),
                          x.get("source_url") or default_url,
                          x.get("date") or default_date)
        if not sig["source_url"]:
            print(f"  ! skipped {x.get('company')}: no source URL "
                  "(pass --url or add URL: to the block)")
            continue
        cid, created, sig_added = upsert(
            store, (x.get("person") or "").strip() or None,
            x.get("role"), (x.get("company") or "").strip(),
            x.get("sector"), x.get("size"), sig)
        if cid is None:
            continue
        tag = "new" if created else ("+signal" if sig_added else "dup")
        print(f"  [{tag:>7}] {cid}: "
              f"{x.get('person') or '(no named person)'} — "
              f"{x.get('role') or '?'}, {x.get('company')}")
        added += 1 if sig_added else 0
        new_records += 1 if created else 0
    return new_records, added


# --- Commands ----------------------------------------------------------------

def get_module(name):
    if name in REGISTRY:
        return REGISTRY[name]
    if name in PLANNED:
        die(f"module '{name}' is specced but not built yet ({PLANNED[name]}). "
            "Build order: gifting-case-studies proves the spine first.")
    die(f"unknown module '{name}'. Built: {', '.join(sorted(REGISTRY))}. "
        f"Planned: {', '.join(sorted(PLANNED))}.")


def cmd_ingest(args):
    module = get_module(args.module)
    config = load_config()
    store = load_store()
    sources = args.source or module.LISTING_SOURCES
    print(f"[ingest] {module.NAME}: discovering from {len(sources)} listing page(s)")
    docs = module.discover(lambda u: fetch(u, config.get("fetch_timeout_seconds", 30)),
                           sources)
    if args.limit:
        docs = docs[:args.limit]
    print(f"[ingest] {len(docs)} document(s) to parse")
    total_new, total_sig = 0, 0
    for i, url in enumerate(docs, 1):
        print(f"[{i}/{len(docs)}] {url}")
        try:
            html = fetch(url, config.get("fetch_timeout_seconds", 30))
        except RuntimeError as exc:
            print(f"  ! {exc}")
            continue
        text = html_to_text(html)
        write_file(os.path.join(RAW_DIR, module.NAME,
                                slugify(url.split("//", 1)[-1]) + ".txt"), text)
        extractions = claude_extract(text, url, module, config)
        n, s = ingest_extractions(store, module.NAME, extractions, url)
        total_new += n
        total_sig += s
        save_store(store)  # keep progress on every doc; ingest can be slow
        time.sleep(config.get("fetch_delay_seconds", 2))
    print(f"\n[ingest] done: {total_new} new candidate(s), "
          f"{total_sig} signal(s) added. Store: {os.path.relpath(STORE_PATH, REPO_ROOT)}")
    print("[ingest] commit the store: it is the committed system of record.")


def cmd_paste(args):
    module = get_module(args.module)
    config = load_config()
    store = load_store()
    if args.file:
        text = read_file(args.file)
    else:
        print("Paste the document (job ad, case study body, announcement, or "
              "structured COMPANY:/PERSON:/EVIDENCE: blocks). End with Ctrl-D:")
        text = sys.stdin.read()
    if not text.strip():
        die("nothing to parse")
    if re.match(r"https?://\S+$", text.strip()):
        die("input is just a URL. Fetch it yourself and paste the body, or "
            "run: sourcing.py ingest --module {m} --source <listing>".format(
                m=module.NAME))
    extractions = parse_structured(text)
    how = "structured blocks"
    if not extractions:
        how = "claude extraction"
        extractions = claude_extract(text, args.url, module, config)
    print(f"[paste] {module.NAME}: {len(extractions)} candidate(s) via {how}")
    n, s = ingest_extractions(store, module.NAME, extractions, args.url,
                              default_date=args.date)
    save_store(store)
    print(f"\n[paste] done: {n} new candidate(s), {s} signal(s) added.")


def _sorted_candidates(store, scoring):
    cands = list(store["candidates"].values())
    # At equal score, a named person outranks an unresolved company-level
    # record: the list Joe reviews should lead with real people.
    return sorted(cands, key=lambda r: (-score(r, scoring),
                                        -len({s["type"] for s in r["signals"]}),
                                        0 if r.get("person") else 1,
                                        r["company"].lower()))


def cmd_shortlist(args):
    scoring = load_scoring()
    store = load_store()
    rows = _sorted_candidates(store, scoring)
    if not args.all:
        rows = [r for r in rows if r["status"] in ("new", "approved")]
    if args.min_score:
        rows = [r for r in rows if score(r, scoring) >= args.min_score]
    if args.limit:
        rows = rows[:args.limit]
    if args.json:
        print(json.dumps([dict(r, score=score(r, scoring)) for r in rows],
                         indent=2, ensure_ascii=False))
        return
    if not rows:
        print("[shortlist] no candidates. Feed a module first "
              "(ingest or paste).")
        return
    print(f"\nSHORTLIST — {len(rows)} candidate(s), scored from "
          f"{os.path.relpath(SCORING_PATH, REPO_ROOT)} "
          f"(signals older than {scoring.get('max_signal_age_days', 90)} days "
          "score zero)\n" + "=" * 78)
    for i, r in enumerate(rows, 1):
        distinct = len({s["type"] for s in r["signals"]})
        who = r["person"] or "(person unresolved — company-level signal)"
        title = f", {r['title']}" if r.get("title") else ""
        extra = ", ".join(x for x in (r.get("sector"), r.get("size")) if x)
        extra = f"  [{extra}]" if extra else ""
        print(f"\n#{i:<3} {score(r, scoring)} pts | {distinct} signal type(s) | "
              f"{r['status'].upper():<8} | id: {r['id']}")
        print(f"     {who}{title} — {r['company']}{extra}")
        for s in r["signals"]:
            pts = signal_points(s, scoring)
            date = s.get("signal_date") or f"found {s['date_added']}"
            print(f"       [{s['type']} | {pts} pt | {date}]")
            print(f"         {s['evidence']}")
            print(f"         {s['source_url']}")
    print("\n" + "=" * 78)
    print("Approve or reject per name (approval is always you):\n"
          "  python sourcing/sourcing.py approve <id> [<id> ...]\n"
          "  python sourcing/sourcing.py reject  <id> [<id> ...]")


def _set_status(args, new_status):
    store = load_store()
    for cid in args.ids:
        rec = store["candidates"].get(cid)
        if not rec:
            print(f"  ! no candidate '{cid}'")
            continue
        rec["status"] = new_status
        rec["decided_date"] = today()
        print(f"  {new_status}: {cid} ({rec['person'] or 'unresolved'} — "
              f"{rec['company']})")
    save_store(store)
    print(f"[{new_status}] store updated — commit it.")


def cmd_approve(args):
    _set_status(args, "approved")


def cmd_reject(args):
    _set_status(args, "rejected")


def cmd_status(args):
    scoring = load_scoring()
    store = load_store()
    cands = store["candidates"].values()
    by_status = {}
    for r in cands:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"[status] {len(store['candidates'])} candidate(s): "
          + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
             or "none"))
    by_type = {}
    for r in cands:
        for s in r["signals"]:
            by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    for t, n in sorted(by_type.items()):
        print(f"         {t}: {n} signal(s)")
    enriched = sum(1 for r in cands if (r.get("enrichment") or {}).get("done"))
    checked = sum(1 for r in cands if (r.get("desk_check") or {}).get("verdict"))
    print(f"         enriched: {enriched}, desk-checked: {checked}")
    print(f"         built modules: {', '.join(sorted(REGISTRY))}")
    print(f"         planned: {', '.join(sorted(PLANNED))}")


# --- Enrich (FullEnrich, approved names only) --------------------------------

def _split_name(person):
    parts = (person or "").strip().split()
    if len(parts) < 2:
        return person or "", ""
    return parts[0], " ".join(parts[1:])


def _fullenrich_request(path, payload, api_key, config, method="POST"):
    fe = config.get("fullenrich", {})
    url = fe.get("base_url", "https://app.fullenrich.com/api/v1") + path
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def cmd_enrich(args):
    config = load_config()
    store = load_store()
    if args.ids:
        recs = [store["candidates"].get(c) or die(f"no candidate '{c}'")
                for c in args.ids]
    else:
        recs = [r for r in store["candidates"].values()
                if r["status"] == "approved"
                and not (r.get("enrichment") or {}).get("done")]
    bad = [r["id"] for r in recs if r["status"] not in ("approved", "exported")]
    if bad:
        die("enrich only ever runs on names Joe approved. Not approved: "
            + ", ".join(bad))
    unresolved = [r["id"] for r in recs if not r.get("person")]
    if unresolved:
        die("no named person on: " + ", ".join(unresolved)
            + ". Resolve the person first (see `resolve`).")
    if not recs:
        print("[enrich] nothing to do: no approved, un-enriched candidates.")
        return

    fe = config.get("fullenrich", {})
    fields = (fe.get("enrich_fields_with_phones")
              if args.phones else fe.get("enrich_fields",
                                         ["contact.work_emails"]))
    datas = []
    for r in recs:
        first, last = _split_name(r["person"])
        d = {"firstname": first, "lastname": last,
             "company_name": r["company"],
             "enrich_fields": fields,
             "custom": {"candidate_id": r["id"]}}
        if args.domain and len(recs) == 1:
            d["domain"] = args.domain
        datas.append(d)
    payload = {"name": f"sentrada sourcing {today()}", "datas": datas}

    api_key = load_env_key("FULLENRICH_API_KEY")
    print(f"[enrich] {len(recs)} approved candidate(s); fields: "
          + ", ".join(fields))
    print("[enrich] request that will be sent to "
          + fe.get("base_url", "") + fe.get("enrich_start_path",
                                            "/contact/enrich/bulk") + ":")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if not api_key:
        print("\n[enrich] STUB MODE: no FULLENRICH_API_KEY in env or .env, so "
              "nothing was sent and no credits were spent.\n"
              "Cost when live (charged only on success): 1 credit per work "
              "email, 10 per mobile (--phones), 3 per personal email.\n"
              "Add the key and re-run this exact command. Verify the endpoint "
              "shapes against docs.fullenrich.com on the first keyed run.")
        return

    started = _fullenrich_request(fe.get("enrich_start_path",
                                         "/contact/enrich/bulk"),
                                  payload, api_key, config)
    enrichment_id = (started.get("enrichment_id") or started.get("id")
                     or die(f"unexpected start response: {started}"))
    print(f"[enrich] started: {enrichment_id}; polling...")
    poll_path = fe.get("enrich_poll_path",
                       "/contact/enrich/bulk/{enrichment_id}")
    deadline = time.time() + int(fe.get("poll_timeout_seconds", 600))
    result = None
    while time.time() < deadline:
        time.sleep(int(fe.get("poll_interval_seconds", 15)))
        result = _fullenrich_request(
            poll_path.format(enrichment_id=enrichment_id), None, api_key,
            config, method="GET")
        status = (result or {}).get("status", "")
        print(f"  poll: {status}")
        if status in ("FINISHED", "CREDITS_INSUFFICIENT", "CANCELED"):
            break
    if not result or result.get("status") != "FINISHED":
        die(f"enrichment did not finish cleanly: "
            f"{(result or {}).get('status')}. Poll it manually: "
            f"{enrichment_id}")

    enriched = load_json(ENRICHED_PATH, {})
    for item in result.get("datas", []):
        cid = (item.get("custom") or {}).get("candidate_id")
        if not cid or cid not in store["candidates"]:
            continue
        enriched[cid] = {"date": today(), "raw": item}
        found = sorted(k for k in ("emails", "phones", "most_probable_email")
                       if item.get(k))
        store["candidates"][cid]["enrichment"] = {
            "done": True, "date": today(), "fields_found": found}
        print(f"  {cid}: found {', '.join(found) or 'nothing'}")
    write_file(ENRICHED_PATH,
               json.dumps(enriched, indent=2, ensure_ascii=False) + "\n")
    save_store(store)
    print(f"[enrich] contact data -> {os.path.relpath(ENRICHED_PATH, REPO_ROOT)} "
          "(GITIGNORED: never commit contact data). Summary flags -> store; "
          "commit the store.")


def cmd_resolve(args):
    """Search-API person resolution: deliberately NOT wired. The addendum's
    condition is that Joe sees the credit cost model before any wiring."""
    config = load_config()
    fe = config.get("fullenrich", {})
    print(
        "[resolve] FullEnrich Search API person resolution is NOT wired yet.\n"
        "\n"
        "Cost model (from FullEnrich's published docs, July 2026):\n"
        "  Search export:  0.25 credits per person or company exported\n"
        "                  (re-exporting the same record is free; the raw\n"
        "                  search call's own cost beyond export is not\n"
        "                  clearly documented)\n"
        "  Enrich:         1 credit per work email found, 3 per personal\n"
        "                  email, 10 per mobile; only charged on success\n"
        "  So resolving the current CMO/VP Marketing/Head of ABM/VP Sales\n"
        "  for a shortlisted company costs ~0.25 credits per person, an\n"
        "  order of magnitude cheaper than an enrich call. On the ~$29/500-\n"
        "  credit Starter tier that is ~1.6 cents per resolution.\n"
        "\n"
        "Planned behaviour once you approve: resolution runs ONLY for\n"
        "candidates that reach the shortlist view with no named person\n"
        "(id co--*) or a person marked as departed; never over the whole\n"
        "store. Enrich still only ever runs on names you approve.\n"
        "\n"
        "To approve the wiring: say so, and set fullenrich.search_enabled\n"
        "to true in sourcing/config.json in the same sitting.")
    if not fe.get("search_enabled"):
        sys.exit(1)
    die("search_enabled is true but the Search client is not implemented "
        "yet; implement it in the sitting where the flag is flipped.")


# --- Desk-check ---------------------------------------------------------------

def cmd_desk_check(args):
    config = load_config()
    store = load_store()
    rec = store["candidates"].get(args.id) or die(f"no candidate '{args.id}'")
    if rec["status"] not in ("approved", "exported"):
        die("desk-check runs for approved names only (heavy effort follows "
            "judgment). Approve the candidate first.")
    rec.setdefault("desk_check", None)
    dc = rec["desk_check"] or {"verdict": None, "sources": [], "date": None}

    if args.gather:
        print(f"[desk-check] gathering office evidence for {rec['company']}:")
        domain = args.domain
        if domain:
            for path in ("/contact", "/contact-us", "/about", "/careers",
                         "/about-us"):
                url = f"https://{domain}{path}"
                try:
                    text = html_to_text(fetch(url, 20))
                except RuntimeError as exc:
                    print(f"  ! {exc}")
                    continue
                hits = [ln.strip() for ln in text.splitlines()
                        if re.search(r"(?i)\b(office|address|headquarter|"
                                     r"registered|hybrid|remote|london|"
                                     r"manchester|street|road)\b", ln)
                        and 8 < len(ln.strip()) < 200][:6]
                if hits:
                    print(f"  {url}:")
                    for h in hits:
                        print(f"    - {h}")
        else:
            print("  (pass --domain to fetch the company site's office pages)")
        ch_key = load_env_key("COMPANIES_HOUSE_API_KEY")
        if ch_key:
            import base64
            q = urllib.parse.quote(rec["company"])
            req = urllib.request.Request(
                "https://api.company-information.service.gov.uk/search/"
                f"companies?q={q}",
                headers={"Authorization": "Basic "
                         + base64.b64encode((ch_key + ":").encode()).decode()})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode())
                for item in data.get("items", [])[:3]:
                    print(f"  Companies House: {item.get('title')} — "
                          f"{item.get('address_snippet')}")
            except (urllib.error.URLError, OSError) as exc:
                print(f"  ! Companies House lookup failed: {exc}")
        else:
            print("  Companies House (manual): "
                  + config.get("companies_house_search_url", "")
                  + urllib.parse.quote(rec["company"]))
        print("  LinkedIn location: check the person's profile by hand "
              "(no LinkedIn automation, ever).")

    if args.source and args.evidence:
        dc["sources"].append({"evidence": args.evidence, "url": args.source})
        print(f"[desk-check] evidence recorded ({len(dc['sources'])} total)")
    elif args.evidence or args.source:
        die("record evidence with BOTH --evidence and --source")

    if args.verdict:
        if args.verdict not in ("office-confirmed", "hybrid-likely",
                                "remote-likely"):
            die("verdict must be office-confirmed, hybrid-likely or "
                "remote-likely")
        if not dc["sources"]:
            die("a verdict needs at least one recorded evidence source")
        dc["verdict"] = args.verdict
        dc["date"] = today()
        print(f"[desk-check] {args.id}: {args.verdict}")
        if args.verdict == "remote-likely":
            print("[desk-check] remote-likely is not a rejection: it routes "
                  "to the address-capture play (tease/capture flow).")
    rec["desk_check"] = dc
    save_store(store)
    if not any((args.gather, args.verdict, args.evidence)):
        print(json.dumps(dc, indent=2))


# --- Export: handoff to the chain as P1 candidates ----------------------------

def cmd_export(args):
    scoring = load_scoring()
    store = load_store()
    ready, held = [], []
    for r in _sorted_candidates(store, scoring):
        if r["status"] != "approved":
            continue
        missing = []
        if not r.get("person"):
            missing.append("person unresolved")
        if not (r.get("enrichment") or {}).get("done"):
            missing.append("not enriched")
        if not (r.get("desk_check") or {}).get("verdict"):
            missing.append("no desk-check verdict")
        (held if missing and not args.force else ready).append((r, missing))
    for r, missing in held:
        print(f"[export] HELD {r['id']}: " + ", ".join(missing))
    if not ready:
        die("nothing to export. The handoff takes approved, enriched, "
            "desk-checked names (--force overrides for a dry run).")

    out_dir = args.out or os.path.join(RESEARCH_DIR, f"sourcing-{today()}")
    os.makedirs(out_dir, exist_ok=True)
    enriched = load_json(ENRICHED_PATH, {})
    manifest = []
    for r, missing in ready:
        slug = f"{slugify(r['person'] or 'unresolved')}-{slugify(r['company'])}"
        signals = [{"type": s["type"], "evidence": s["evidence"],
                    "source_url": s["source_url"],
                    "signal_date": s.get("signal_date")}
                   for s in r["signals"]]
        manifest.append({
            "name": r["person"] or "",
            "title": r.get("title") or "",
            "company": r["company"],
            # Format is an intake decision (pipeline step 1), made per
            # recipient when research lands; batch-brief refuses until set.
            "format": "",
            "research": f"{slug}.md",
            "source_signals": signals,
            "desk_check": (r.get("desk_check") or {}).get("verdict") or "",
        })
        lines = [f"# P1 candidate: {r['person'] or 'UNRESOLVED'} — "
                 f"{r['company']}", ""]
        if missing:
            lines += ["**INCOMPLETE (exported with --force): "
                      + ", ".join(missing) + "**", ""]
        lines += [f"- Title: {r.get('title') or 'unknown'}",
                  f"- Sector: {r.get('sector') or 'unknown'}",
                  f"- Size: {r.get('size') or 'unknown'}",
                  f"- Discovered: {r['date_discovered']}, approved: "
                  f"{r.get('decided_date')}", "",
                  "## Source signals (keep these with the piece: response "
                  "rate per signal source is computed from them)", ""]
        for s in r["signals"]:
            when = s.get("signal_date") or ("undated, found " + s["date_added"])
            lines += [f"- **{s['type']}** ({when}): {s['evidence']}",
                      f"  - {s['source_url']}"]
        dc = r.get("desk_check") or {}
        lines += ["", f"## Desk check: {dc.get('verdict') or 'NOT DONE'}"]
        for src in dc.get("sources", []):
            lines += [f"- {src['evidence']}", f"  - {src['url']}"]
        if dc.get("verdict") == "remote-likely":
            lines += ["", "Routing: remote-likely -> address-capture play "
                      "(tease link, not a rejection)."]
        contact = enriched.get(r["id"], {}).get("raw")
        lines += ["", "## Contact data (from FullEnrich; this file lives in "
                  "gitignored research/, never commit contact data elsewhere)"]
        lines += [("```json\n" + json.dumps(contact, indent=2,
                                            ensure_ascii=False) + "\n```")
                  if contact else "(not enriched)"]
        write_file(os.path.join(out_dir, f"{slug}-signals.md"),
                   "\n".join(lines) + "\n")
        r["status"] = "exported"
        r["piece_slug"] = slug
        print(f"[export] {r['id']} -> {slug}-signals.md")

    write_file(os.path.join(out_dir, "manifest.json"),
               json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    save_store(store)
    print(f"\n[export] {len(ready)} candidate(s) -> {out_dir}")
    print("[export] next: run Prompt 1 research per candidate (the signals "
          "file is P1 input, not a research substitute), set each entry's "
          "format, point 'research' at the research file, then batch-brief.\n"
          "[export] source_signals ride the manifest into each piece's "
          "meta.json and the outcome ledger, so response rate per signal "
          "source is computable later. Commit the store.")


# --- main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="sourcing.py",
        description="Sentrada sourcing: signal modules -> one candidate store "
                    "-> scored shortlist -> Joe approves -> enrich + "
                    "desk-check -> P1 handoff.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="scrape path: walk a module's sources")
    p.add_argument("--module", required=True)
    p.add_argument("--source", action="append",
                   help="override listing URL(s); repeatable")
    p.add_argument("--limit", type=int, default=40,
                   help="max documents to parse (default 40)")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("paste", help="paste path: parse a pasted document")
    p.add_argument("--module", required=True)
    p.add_argument("--url", default="",
                   help="source URL for the pasted document")
    p.add_argument("--file", help="read the document from a file instead of stdin")
    p.add_argument("--date", default="",
                   help="signal date YYYY-MM-DD if known (else undated)")
    p.set_defaults(func=cmd_paste)

    p = sub.add_parser("shortlist", help="ranked candidates with evidence")
    p.add_argument("--all", action="store_true",
                   help="include rejected and exported")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_shortlist)

    p = sub.add_parser("approve", help="approve candidates (always Joe)")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject candidates")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("status", help="store counts")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("enrich",
                       help="FullEnrich on APPROVED names only (stub without key)")
    p.add_argument("--id", dest="ids", action="append",
                   help="specific candidate id(s); default: all approved "
                        "un-enriched")
    p.add_argument("--phones", action="store_true",
                   help="also request mobiles (10 credits each when found)")
    p.add_argument("--domain", default="",
                   help="company domain hint (single-candidate runs only)")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("resolve",
                       help="Search-API person resolution (NOT wired: prints "
                            "the cost model)")
    p.add_argument("--id", dest="ids", action="append")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("desk-check",
                       help="office evidence for an approved name")
    p.add_argument("--id", required=True)
    p.add_argument("--gather", action="store_true",
                   help="fetch office evidence (company site, Companies House)")
    p.add_argument("--domain", default="", help="company website domain")
    p.add_argument("--evidence", default="", help="evidence line to record")
    p.add_argument("--source", default="", help="URL the evidence came from")
    p.add_argument("--verdict", default="",
                   help="office-confirmed | hybrid-likely | remote-likely")
    p.set_defaults(func=cmd_desk_check)

    p = sub.add_parser("export",
                       help="handoff: approved+enriched+desk-checked -> P1 "
                            "candidates")
    p.add_argument("--out", default="",
                   help="output dir (default research/sourcing-<today>/)")
    p.add_argument("--force", action="store_true",
                   help="export approved names even if enrichment/desk-check "
                        "is missing")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
