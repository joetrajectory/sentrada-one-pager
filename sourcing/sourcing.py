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
import calendar
import contextlib
import datetime
import io
import json
import os
import re
import shutil
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


def write_file_atomic(path, content):
    """Tmp file + os.replace, like the runner's write_file: a crash mid-write
    can never truncate the store."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
    if not sc or "signals" not in sc:
        die(f"scoring config missing or malformed: {SCORING_PATH}")
    return sc


def load_config():
    return load_json(CONFIG_PATH, {})


# --- Candidate store ---------------------------------------------------------

def load_store():
    return load_json(STORE_PATH, {"version": 1, "updated": "", "candidates": {}})


def save_store(store):
    store["updated"] = today()
    write_file_atomic(STORE_PATH,
                      json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def _remind_durability():
    print("[durability] candidates.json is GITIGNORED while the repo is "
          "public (it is the sender's target list). A container reset loses "
          "it; run `dump` and keep a copy, or re-import with `import --file`.")


def _dump_store(store, reason=""):
    """Print the full store so the chat log holds a durable copy: the repo is
    public, so the store cannot ride a code branch until the repo goes
    private at the deployment sitting."""
    print(f"\n[durability] full store dump{' (' + reason + ')' if reason else ''} "
          "— candidates.json is gitignored while the repo is public; copy "
          "this somewhere safe. Restore with: sourcing.py import --file "
          "<saved.json> --replace")
    print("----- BEGIN SOURCING STORE DUMP -----")
    print(json.dumps(store, indent=2, ensure_ascii=False))
    print("----- END SOURCING STORE DUMP -----")


def candidate_id(person, company):
    if person:
        return f"{slugify(person)}-{slugify(company)}"
    # Company-level signal with no named person yet; the Search-API resolution
    # step (once approved and wired) fills the person in later.
    return f"co--{slugify(company)}"


def upsert(store, person, title, company, sector, size, signal,
           location=None):
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
            "location": location or None,
            "location_basis": None,
            "tags": [],
            "status": "new",
            "date_discovered": today(),
            "decided_date": None,
            "signals": [],
            "currency": None,
            "enrichment": None,
            "desk_check": None,
            "piece_slug": None,
        }
        cands[cid] = rec
        created = True
    else:
        created = False
        # Fill gaps, never overwrite what a human may have corrected.
        for key, val in (("title", title), ("sector", sector), ("size", size),
                         ("location", location)):
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

def signal_effective_date(sig, scoring):
    """The date a signal ages from: its own date, else (per config) the date
    it entered the store. Returns a datetime.date or None."""
    eff = sig.get("signal_date")
    if not eff and scoring.get("undated_signals_use_discovery_date", True):
        eff = sig.get("date_added")
    if not eff:
        return None
    # Tolerate YYYY or YYYY-MM by pinning to the earliest day they could mean.
    eff = {4: eff + "-01-01", 7: eff + "-01"}.get(len(eff), eff)
    try:
        return datetime.date.fromisoformat(eff)
    except ValueError:
        return None


_warned_unknown_types = set()


def signal_points(sig, scoring):
    """Points for one signal. Ageing is per signal type (scoring.json):
    hiring signals go stale fast, a gifting case study stays meaningful for
    24 months. Signals past their window score zero but stay recorded."""
    entry = scoring["signals"].get(sig["type"])
    if entry is None:
        if sig["type"] not in _warned_unknown_types:
            _warned_unknown_types.add(sig["type"])
            print(f"[scoring] unknown signal type '{sig['type']}' scores 0 "
                  "(not in sourcing/scoring.json; add it there if it is real)")
        entry = {}
    eff_date = signal_effective_date(sig, scoring)
    if eff_date is None:
        return 0
    age = (datetime.date.today() - eff_date).days
    return 0 if age > int(entry.get("max_age_days", 90)) \
        else int(entry.get("points", 0))


def score(rec, scoring):
    return sum(signal_points(s, scoring) for s in rec["signals"])


def needs_currency_check(rec, scoring, config):
    """True when the record's evidence is old enough (any DATED signal past
    currency_check_after_days) that the person must be confirmed still in
    the seat before approval. Undated signals do not trigger this (their age
    is unknowable here; P1 research re-verifies everyone regardless), but a
    PRESENT-yet-unparseable date fails closed: 'March 2023' is a claim of
    age, not an absence of one. A recorded currency check itself expires
    after currency_check_after_days."""
    limit = int(config.get("currency_check_after_days", 365))
    checked = (rec.get("currency") or {}).get("checked")
    if checked:
        try:
            check_age = (datetime.date.today()
                         - datetime.date.fromisoformat(checked)).days
        except (ValueError, TypeError):
            check_age = limit + 1  # unreadable check date: treat as expired
        if check_age <= limit:
            return False
        # An expired check no longer satisfies the gate: fall through.
    for sig in rec["signals"]:
        if not sig.get("signal_date"):
            continue
        eff = signal_effective_date(sig, scoring)
        if eff is None:
            return True  # dated but unparseable: fail closed, verify by hand
        if (datetime.date.today() - eff).days > limit:
            return True
    return False


# --- Thread-aware holds -------------------------------------------------------
# One live thread per company, ever. The check reads the chain's committed
# ledgers and piece folders at shortlist time; nothing is stored, so a hold
# lifts by itself the moment the thread closes, whatever the outcome.

OUTCOMES_PATH = os.path.join(REPO_ROOT, "runner", "outcomes.json")
CAPTURE_PATH = os.path.join(REPO_ROOT, "runner", "capture.json")
PIECES_DIR = os.path.join(REPO_ROOT, "runner", "pieces")


# Trailing corporate furniture: "Acme Ltd", "Acme Limited" and "Acme" are the
# same company for hold purposes.
_COMPANY_SUFFIXES = {"ltd", "limited", "inc", "incorporated", "llc", "plc",
                     "gmbh", "co", "corp", "corporation", "group", "holdings"}


def company_key(name):
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def _parse_outcome_date(value):
    """Ledger date -> datetime.date, or None. Month-only ('2026-07') and
    year-only dates pin to the LAST day they could mean, so a no_response
    hold never closes early on a coarse date."""
    v = (value or "").strip()
    if len(v) == 4 and v.isdigit():
        v += "-12-31"
    elif len(v) == 7:
        try:
            y, m = int(v[:4]), int(v[5:7])
            v = f"{v}-{calendar.monthrange(y, m)[1]:02d}"
        except ValueError:
            return None
    try:
        return datetime.date.fromisoformat(v)
    except ValueError:
        return None


def _outcome_state(rec, config):
    """'live' or 'closed' for one outcomes-ledger record."""
    th = config.get("thread_hold", {})
    result = rec.get("result", "")
    if result in th.get("live_results", ["replied", "opportunity"]):
        return "live"
    if result == "no_response":
        fields = ("first_reply_date", "delivered", "delivered_date", "date")
        # Prefer a full YYYY-MM-DD anywhere on the record over a coarser one.
        when = next((_parse_outcome_date(rec.get(f))
                     for f in fields
                     if len((rec.get(f) or "").strip()) == 10
                     and _parse_outcome_date(rec.get(f))), None)
        if when is None:
            when = next((_parse_outcome_date(rec.get(f))
                         for f in fields if _parse_outcome_date(rec.get(f))),
                        None)
        if when is None:
            return "live"  # unparseable/absent date: hold, don't double-thread
        days = (datetime.date.today() - when).days
        return "live" if days <= int(th.get("followup_window_days", 14)) \
            else "closed"
    return "closed"  # declined, bounced, meeting, swapped, ...


def _piece_meta_company(slug):
    meta_path = os.path.join(PIECES_DIR, slug, "meta.json")
    if os.path.exists(meta_path):
        try:
            return json.loads(read_file(meta_path)).get(
                "recipient_company", "") or ""
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def _company_from_slug_tail(slug, store):
    """Last-resort slug -> company: does the squashed slug end with a known
    candidate company? Returns the matched company name or ''."""
    if not store:
        return ""
    squashed = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    best = ""
    for rec in store.get("candidates", {}).values():
        comp = rec.get("company") or ""
        for key in (re.sub(r"[^a-z0-9]", "", comp.lower()),
                    company_key(comp)):
            if len(key) >= 3 and squashed.endswith(key) and len(key) > len(
                    re.sub(r"[^a-z0-9]", "", best.lower())):
                best = comp
    return best


def live_threads(config, store=None):
    """Map of company_key -> human-readable reason the company is held.
    Entries keyed 'unresolved:<slug>' are holds whose company could not be
    resolved; they cannot match a candidate, so they are surfaced instead of
    silently dropped (the committed ledger may be the only surviving evidence
    after a container restart)."""
    threads = {}
    outcomes = load_json(OUTCOMES_PATH, {})
    for slug, rec in outcomes.items():
        ck = company_key(rec.get("company") or "")
        if not ck:
            continue
        if _outcome_state(rec, config) == "live":
            threads[ck] = (f"live thread: {slug} "
                           f"({rec.get('result')}, {rec.get('date', '?')})")
    # Pieces on disk with no outcome recorded yet are in flight. A closed
    # outcome only clears the folder that recorded it (same slug); a newer
    # piece for the same company is its own thread.
    if os.path.isdir(PIECES_DIR):
        for folder in sorted(os.listdir(PIECES_DIR)):
            if not os.path.isdir(os.path.join(PIECES_DIR, folder)):
                continue
            if folder in outcomes:
                continue  # this piece's thread state came from the ledger
            company = _piece_meta_company(folder)
            if not company and "_" in folder:
                # CP-NN_Surname_Company folder convention
                company = folder.split("_")[-1]
            if not company:
                company = _company_from_slug_tail(folder, store)
            ck = company_key(company)
            if ck:
                if ck not in threads:
                    threads[ck] = f"piece in flight: {folder} (no outcome yet)"
            else:
                threads[f"unresolved:{folder}"] = (
                    f"piece in flight: {folder} (no outcome yet; company "
                    "unresolved — no meta.json, treat as a hold)")
    # Capture flow: a tease/address cycle is a live thread too. The runner's
    # ledger keys are piece slugs and hold no company, so resolve the slug.
    capture = load_json(CAPTURE_PATH, {})
    if isinstance(capture, dict):
        for code, rec in capture.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("status") not in ("printed", "tease-sent",
                                         "address-received"):
                continue
            company = (rec.get("company") or _piece_meta_company(code)
                       or (outcomes.get(code) or {}).get("company", "")
                       or _company_from_slug_tail(code, store))
            ck = company_key(company)
            if ck:
                if ck not in threads:
                    threads[ck] = f"capture flow: {code} ({rec['status']})"
            else:
                threads[f"unresolved:{code}"] = (
                    f"capture flow: {code} ({rec['status']}; company "
                    "unresolved — treat as a hold)")
    return threads


# --- Location ------------------------------------------------------------------

def location_class(rec, config):
    """'uk' | 'non-uk' | 'unknown' for the PERSON's location (never HQ).
    Only a KNOWN-foreign marker banks a name out of the default view; a
    location matching neither list is 'unknown' (shown, flagged), so an
    unlisted UK town never silently disappears."""
    loc = (rec.get("location") or "").lower()
    if not loc:
        return "unknown"
    sl = config.get("shortlist", {})

    def longest_hit(terms):
        return max((len(t) for t in terms
                    if re.search(r"\b" + re.escape(t.lower()) + r"\b", loc)),
                   default=0)

    # Longest matched term wins so 'new york' beats 'york' and 'northern
    # ireland' beats 'ireland'; on a dead tie the name stays visible as UK.
    uk = longest_hit(sl.get("uk_location_terms", ["uk"]))
    foreign = longest_hit(sl.get("foreign_location_terms", []))
    if uk or foreign:
        return "uk" if uk >= foreign else "non-uk"
    return "unknown"


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
               "size": "size", "location": "location"}


def parse_structured(text):
    """Parse KEY: value blocks separated by --- lines. Returns a list of
    extraction dicts, or [] if the text is not structured. The structured
    path only triggers when the FIRST non-blank line is a COMPANY: key
    (freeform prose that merely mentions 'Company:' goes to the model). A
    repeated COMPANY: key inside one chunk starts a new item, so blocks
    separated only by a blank line never bleed fields into each other."""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not re.match(r"(?i)^\s*company\s*:", first):
        return []

    def _finish(item, out):
        if item.get("company"):
            for k in ("person", "role", "quote_or_evidence", "source_url",
                      "date", "sector", "size", "location"):
                item.setdefault(k, None)
                if item[k] in ("", "null", "None", "-"):
                    item[k] = None
            out.append(item)

    out = []
    for chunk in re.split(r"(?m)^\s*-{3,}\s*$", text):
        item, key = {}, None
        for line in chunk.splitlines():
            m = re.match(r"\s*([A-Za-z/ ]+?)\s*:\s*(.*)$", line)
            if m and m.group(1).strip().lower() in _BLOCK_KEYS:
                key = _BLOCK_KEYS[m.group(1).strip().lower()]
                if key == "company" and item.get("company"):
                    _finish(item, out)   # new block without a --- separator
                    item = {}
                item[key] = m.group(2).strip()
            elif key and line.strip():
                item[key] = (item[key] + " " + line.strip()).strip()
        _finish(item, out)
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
    data = json.loads(m.group(1))
    if not isinstance(data, list):
        die("extraction json was not an array")
    valid = [x for x in data
             if isinstance(x, dict)
             and isinstance(x.get("company"), str) and x["company"].strip()]
    if len(valid) < len(data):
        print(f"  ! skipped {len(data) - len(valid)} malformed extraction "
              "item(s) (not a dict, or no company string)")
    return valid


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
            x.get("sector"), x.get("size"), sig,
            location=x.get("location"))
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
    # API-backed modules (e.g. abm-hiring via Adzuna) implement api_ingest and
    # return extraction dicts directly; listing-page modules use discover().
    if hasattr(module, "api_ingest") and not args.source:
        extractions = module.api_ingest(config, load_env_key)
        if extractions is None:  # module printed its own instructions
            return
        n, s = ingest_extractions(store, module.NAME, extractions, "")
        save_store(store)
        print(f"\n[ingest] done: {n} new candidate(s), {s} signal(s) added.")
        _dump_store(store, "after ingest")
        return
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
    _dump_store(store, "after ingest")


def cmd_paste(args):
    module = get_module(args.module)
    config = load_config()
    store = load_store()
    if args.date:
        d = args.date.strip()
        probe = {4: d + "-01-01", 7: d + "-01"}.get(len(d), d)
        try:
            datetime.date.fromisoformat(probe)
        except ValueError:
            die(f"--date {args.date!r} is not a date the scorer can read. "
                "Use YYYY-MM-DD (or YYYY-MM / YYYY); an unreadable date "
                "would silently score zero and dodge the currency check.")
        args.date = d
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
    _dump_store(store, "after paste")


def _sorted_candidates(store, scoring):
    cands = list(store["candidates"].values())
    # At equal score: a confirmed-UK desk outranks unknown outranks non-UK,
    # and a named person outranks an unresolved company-level record. The
    # list Joe reviews leads with real people he can actually send to.
    loc_rank = {"uk": 0, "unknown": 1, "non-uk": 2}
    config = load_config()
    return sorted(cands, key=lambda r: (-score(r, scoring),
                                        -len({s["type"] for s in r["signals"]}),
                                        loc_rank[location_class(r, config)],
                                        0 if r.get("person") else 1,
                                        r["company"].lower()))


def cmd_shortlist(args):
    scoring = load_scoring()
    config = load_config()
    store = load_store()
    threads = live_threads(config, store)
    rows = _sorted_candidates(store, scoring)
    if not args.all:
        rows = [r for r in rows if r["status"] in ("new", "approved")]
    if args.min_score:
        rows = [r for r in rows if score(r, scoring) >= args.min_score]
    banked = 0
    if not args.world:
        kept = []
        for r in rows:
            if location_class(r, config) == "non-uk":
                banked += 1
            else:
                kept.append(r)
        rows = kept
    if args.limit:
        rows = rows[:args.limit]
    if args.json:
        print(json.dumps(
            [dict(r, score=score(r, scoring),
                  hold=threads.get(company_key(r["company"]), ""),
                  location_class=location_class(r, config)) for r in rows],
            indent=2, ensure_ascii=False))
        return
    if not rows:
        print("[shortlist] no candidates in this view."
              + (f" ({banked} non-UK banked; --world shows them.)"
                 if banked else " Feed a module first (ingest or paste)."))
        return
    print(f"\nSHORTLIST — {len(rows)} candidate(s), scored from "
          f"{os.path.relpath(SCORING_PATH, REPO_ROOT)} "
          "(per-signal ageing; stale signals score zero)")
    if not args.world:
        print(f"UK view (the desk, not the HQ): known-non-UK names banked, "
              f"not shown ({banked} banked; --world shows the full list); "
              "unclassified locations stay visible, flagged")
    unresolved = {k: v for k, v in threads.items()
                  if k.startswith("unresolved:")}
    for reason in unresolved.values():
        print(f"** UNRESOLVED HOLD — {reason} **")
    print("=" * 78)
    for i, r in enumerate(rows, 1):
        distinct = len({s["type"] for s in r["signals"]})
        who = r["person"] or "(person unresolved — company-level signal)"
        title = f", {r['title']}" if r.get("title") else ""
        loc = r.get("location") or "location unknown"
        if r.get("location") and location_class(r, config) == "unknown":
            loc += " — UNCLASSIFIED, verify UK/non-UK"
        extra = ", ".join(x for x in (r.get("sector"), r.get("size")) if x)
        extra = f"  [{extra}]" if extra else ""
        tags = f"  <{', '.join(r['tags'])}>" if r.get("tags") else ""
        print(f"\n#{i:<3} {score(r, scoring)} pts | {distinct} signal type(s) | "
              f"{r['status'].upper():<8} | id: {r['id']}")
        print(f"     {who}{title} — {r['company']} ({loc}){extra}{tags}")
        hold = threads.get(company_key(r["company"]))
        if hold:
            print(f"     ** HOLD until that thread resolves — {hold} **")
        if needs_currency_check(r, scoring, config):
            print("     ** CURRENCY CHECK — story older than 12 months; "
                  "confirm still in seat before approving (see `verify`) **")
        by_type = {}
        for s in r["signals"]:
            if signal_points(s, scoring) > 0:
                by_type[s["type"]] = by_type.get(s["type"], 0) + 1
        lean = [f"{n}x {t}" for t, n in sorted(by_type.items()) if n > 2]
        if lean:
            print(f"     ** score leans on {', '.join(lean)} — same-signal "
                  "volume, not breadth **")
        for s in r["signals"]:
            pts = signal_points(s, scoring)
            date = s.get("signal_date") or f"found {s['date_added']}"
            print(f"       [{s['type']} | {pts} pt | {date}]")
            print(f"         {s['evidence']}")
            print(f"         {s['source_url']}")
    print("\n" + "=" * 78)
    print("Approve or reject per name (approval is always you):\n"
          "  python sourcing/sourcing.py approve <id> [<id> ...]\n"
          "  python sourcing/sourcing.py reject  <id> [<id> ...]\n"
          "Holds are computed live from runner/outcomes.json, runner/pieces/ "
          "and the capture ledger;\nthey lift by themselves when the thread "
          "closes, whatever its outcome.")


def _set_status(args, new_status):
    scoring = load_scoring()
    config = load_config()
    store = load_store()
    threads = live_threads(config, store) if new_status == "approved" else {}
    for cid in args.ids:
        rec = store["candidates"].get(cid)
        if not rec:
            print(f"  ! no candidate '{cid}'")
            continue
        if new_status == "approved":
            if needs_currency_check(rec, scoring, config):
                print(f"  ! {cid}: REFUSED — the story behind this name is "
                      "older than 12 months; confirm they are still in the "
                      "seat first:\n"
                      f"      python sourcing/sourcing.py verify --id {cid} "
                      "[--in-seat | --moved ...]")
                continue
            hold = threads.get(company_key(rec["company"]))
            if hold:
                print(f"  ! {cid}: company has a live thread ({hold}). "
                      "One thread per company at a time — approving anyway "
                      "records your decision, but do not start this thread "
                      "until the live one resolves.")
        rec["status"] = new_status
        rec["decided_date"] = today()
        print(f"  {new_status}: {cid} ({rec['person'] or 'unresolved'} — "
              f"{rec['company']})")
    save_store(store)
    print(f"[{new_status}] store updated.")
    _remind_durability()


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


def _harvest_enrich_result(store, result, enrichment_id):
    """Record a FINISHED enrichment: contact data -> enriched.json, summary
    flags -> store. Clears the pending marker on every record submitted
    under this enrichment_id, whether or not results came back for it."""
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
    for rec in store["candidates"].values():
        e = rec.get("enrichment") or {}
        if e.get("pending_id") == enrichment_id and not e.get("done"):
            e.pop("pending_id", None)
            e.pop("pending_date", None)
            rec["enrichment"] = e or None
    write_file_atomic(ENRICHED_PATH,
                      json.dumps(enriched, indent=2, ensure_ascii=False) + "\n")
    save_store(store)
    print(f"[enrich] contact data -> {os.path.relpath(ENRICHED_PATH, REPO_ROOT)} "
          "(GITIGNORED: never commit contact data). Summary flags -> store; "
          "commit the store.")


def _poll_enrichment(enrichment_id, api_key, config):
    """Poll one enrichment to completion. A network failure here NEVER loses
    the id: the store already holds it as enrichment.pending_id, and the
    resume path is enrich --poll, which re-submits nothing."""
    fe = config.get("fullenrich", {})
    poll_path = fe.get("enrich_poll_path",
                       "/contact/enrich/bulk/{enrichment_id}")
    deadline = time.time() + int(fe.get("poll_timeout_seconds", 600))
    resume = (f"Resume WITHOUT re-submitting (a fresh enrich would "
              f"double-spend): sourcing.py enrich --poll {enrichment_id}")
    result = None
    try:
        while time.time() < deadline:
            time.sleep(int(fe.get("poll_interval_seconds", 15)))
            result = _fullenrich_request(
                poll_path.format(enrichment_id=enrichment_id), None, api_key,
                config, method="GET")
            status = (result or {}).get("status", "")
            print(f"  poll: {status}")
            if status in ("FINISHED", "CREDITS_INSUFFICIENT", "CANCELED"):
                break
    except (urllib.error.URLError, OSError) as exc:
        die(f"polling failed mid-flight: {exc}. The batch IS submitted. "
            + resume)
    if not result or result.get("status") != "FINISHED":
        die(f"enrichment did not finish cleanly: "
            f"{(result or {}).get('status')}. " + resume)
    return result


def cmd_enrich(args):
    config = load_config()
    store = load_store()
    api_key = load_env_key("FULLENRICH_API_KEY")

    if args.poll:
        if not api_key:
            die("--poll needs FULLENRICH_API_KEY in env or .env (the batch "
                "was submitted with one)")
        result = _poll_enrichment(args.poll, api_key, config)
        _harvest_enrich_result(store, result, args.poll)
        return

    if args.ids:
        recs = [store["candidates"].get(c) or die(f"no candidate '{c}'")
                for c in args.ids]
    else:
        skipped_pending = [
            r["id"] for r in store["candidates"].values()
            if r["status"] == "approved"
            and (r.get("enrichment") or {}).get("pending_id")]
        if skipped_pending:
            print("[enrich] skipping record(s) with results already pending "
                  "(finish them with enrich --poll <id>): "
                  + ", ".join(skipped_pending))
        recs = [r for r in store["candidates"].values()
                if r["status"] == "approved"
                and not (r.get("enrichment") or {}).get("done")
                and not (r.get("enrichment") or {}).get("pending_id")]
    bad = [r["id"] for r in recs if r["status"] not in ("approved", "exported")]
    if bad:
        die("enrich only ever runs on names Joe approved. Not approved: "
            + ", ".join(bad))
    pending = [r for r in recs
               if (r.get("enrichment") or {}).get("pending_id")]
    if pending:
        die("already submitted, results pending — re-submitting would "
            "double-spend. Finish the pending batch first:\n"
            + "\n".join(f"  sourcing.py enrich --poll "
                        f"{(r['enrichment'])['pending_id']}   # {r['id']}"
                        for r in pending))
    if args.ids and not args.re_enrich:
        done = [r["id"] for r in recs
                if (r.get("enrichment") or {}).get("done")]
        if done:
            die("already enriched (credits were spent once): "
                + ", ".join(done)
                + ". Pass --re-enrich to spend again on purpose.")
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

    try:
        started = _fullenrich_request(fe.get("enrich_start_path",
                                             "/contact/enrich/bulk"),
                                      payload, api_key, config)
    except (urllib.error.URLError, OSError) as exc:
        die(f"enrich start failed: {exc}. The submission may or may not have "
            "landed — check the FullEnrich dashboard BEFORE re-running (a "
            "blind re-run of a batch that did land double-spends).")
    enrichment_id = (started.get("enrichment_id") or started.get("id")
                     or die(f"unexpected start response: {started}"))
    # Persist the id BEFORE polling: a poll failure must never lose it (a
    # retry that re-submits the batch would spend the credits twice).
    for r in recs:
        e = r.get("enrichment") or {}
        e["pending_id"] = enrichment_id
        e["pending_date"] = today()
        r["enrichment"] = e
    save_store(store)
    print(f"[enrich] started: {enrichment_id} (persisted to the store as "
          f"enrichment.pending_id; if polling dies, resume with "
          f"enrich --poll {enrichment_id}); polling...")
    result = _poll_enrichment(enrichment_id, api_key, config)
    _harvest_enrich_result(store, result, enrichment_id)


def _search_people(filters, config, limit=None):
    """FullEnrich v2 people/search. Returns (people, note). Without a key it
    prints the exact request and returns (None, 'stub'). The request shape is
    reconstructed from the documented filter set (docs.fullenrich.com is
    blocked from this container) — verify on the first keyed run; cost is
    0.25 credits per person exported."""
    fe = config.get("fullenrich", {})
    if not fe.get("search_enabled"):
        die("fullenrich.search_enabled is false in sourcing/config.json")
    payload = {"filters": filters,
               "limit": int(limit or fe.get("search_limit", 5))}
    url = (fe.get("base_url_v2", "https://app.fullenrich.com/api/v2")
           + fe.get("search_people_path", "/people/search"))
    print(f"[search] request to {url}:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    api_key = load_env_key("FULLENRICH_API_KEY")
    if not api_key:
        print("[search] STUB MODE: no FULLENRICH_API_KEY, nothing sent, no "
              "credits spent. Cost when live: 0.25 credits per person "
              "exported. Verify the request shape against docs.fullenrich.com "
              "on the first keyed run.")
        return None, "stub"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    for key in ("datas", "data", "people", "results", "items"):
        if isinstance(body.get(key), list):
            return body[key], ""
    die(f"unexpected search response shape: {str(body)[:300]}")


def _person_fields(p):
    """Best-effort name/title/company/location from one search result."""
    name = p.get("name") or " ".join(
        x for x in (p.get("first_name") or p.get("firstname"),
                    p.get("last_name") or p.get("lastname")) if x)
    return (name,
            p.get("job_title") or p.get("title") or p.get("headline") or "",
            p.get("company_name") or p.get("company") or "",
            p.get("location") or "")


def cmd_resolve(args):
    """Person resolution for shortlisted person-less records (id co--*):
    find the current holder of the relevant seat via the Search API. Runs
    ONLY for records in the shortlist view, never the whole store. Enrich
    still only ever runs on names Joe approves."""
    scoring = load_scoring()
    config = load_config()
    store = load_store()
    if args.ids:
        recs = [store["candidates"].get(c) or die(f"no candidate '{c}'")
                for c in args.ids]
        bad = [r["id"] for r in recs
               if r["status"] not in ("new", "approved")]
        if bad:
            die("resolve only runs for new/approved records (credits follow "
                "live candidates, not closed ones): " + ", ".join(bad))
    else:
        visible = [r for r in _sorted_candidates(store, scoring)
                   if r["status"] in ("new", "approved")]
        recs = [r for r in visible if not r.get("person")]
    recs = [r for r in recs if not r.get("person")] or die(
        "nothing to resolve: no person-less records in scope")
    assign_n = None
    if args.assign:
        if len(recs) != 1:
            die(f"--assign applies one search result to ONE record, but "
                f"{len(recs)} are in scope. Run with a single --id.")
        try:
            assign_n = int(args.assign)
        except ValueError:
            die(f"--assign takes the result number, not {args.assign!r}")
    titles = config.get("fullenrich", {}).get("resolve_titles", [])
    for rec in recs:
        print(f"\n[resolve] {rec['id']}: current holder of "
              f"{titles[0] if titles else 'the relevant seat'}-class seat "
              f"at {rec['company']}")
        people, note = _search_people(
            {"job_titles": titles, "company_names": [rec["company"]]}, config)
        if people is None:
            continue
        if not people:
            print("  no match returned")
            continue
        for i, p in enumerate(people, 1):
            name, title, comp, loc = _person_fields(p)
            print(f"  {i}. {name} — {title}, {comp} ({loc or 'location ?'})")
        if assign_n is not None:
            if not (1 <= assign_n <= len(people)):
                die(f"--assign {assign_n} is out of range: the search "
                    f"returned {len(people)} result(s)")
            p = people[assign_n - 1]
            name, title, comp, loc = _person_fields(p)
            if not name:
                die("picked result has no name field to assign")
            _reid_person(store, rec, name, title or None, loc or None,
                         tag="resolved-via-search")
    save_store(store)
    print("\n[resolve] store updated. Assign a result with --assign N (single "
          "--id runs), or record a manual find with:\n"
          "  sourcing.py set --id co--<company> --person \"Name\" --title \"T\"")
    _remind_durability()


# Strongest-wins ranking for record merges. Approved beats new; exported is
# further along still; rejected/superseded are decisions too and beat new.
_STATUS_RANK = {"new": 0, "rejected": 1, "superseded": 1, "approved": 2,
                "exported": 3}


def _merge_records(target, source, verbose=False):
    """Strongest-wins merge of source's decision/enrichment state into
    target: a decision, an enrichment, a desk-check or a currency check must
    never vanish in a merge. Returns the list of fields adopted."""
    adopted = []
    ss, ts = source.get("status", "new"), target.get("status", "new")
    if _STATUS_RANK.get(ss, 0) > _STATUS_RANK.get(ts, 0):
        target["status"] = ss
        adopted.append("status")
        if verbose:
            print(f"    {target.get('id')}: status {ts} -> {ss} "
                  "(incoming record was further along)")
    elif verbose and ss != ts:
        print(f"    {target.get('id')}: status diverges ({ts} kept, "
              f"{ss} incoming)")
    sd, td = source.get("decided_date"), target.get("decided_date")
    if sd and (not td or sd < td):
        target["decided_date"] = sd
        adopted.append("decided_date")
    sd, td = source.get("date_discovered"), target.get("date_discovered")
    if sd and (not td or sd < td):
        target["date_discovered"] = sd
    for field in ("enrichment", "desk_check", "currency", "piece_slug",
                  "superseded_by"):
        if source.get(field) and not target.get(field):
            target[field] = source[field]
            adopted.append(field)
            if verbose:
                print(f"    {target.get('id')}: adopted {field} from the "
                      "incoming record")
        elif verbose and source.get(field) \
                and source.get(field) != target.get(field):
            print(f"    {target.get('id')}: {field} diverges (existing kept)")
    for field in ("person", "title", "location", "location_basis", "sector",
                  "size"):
        if source.get(field) and not target.get(field):
            target[field] = source[field]
    for tag in source.get("tags") or []:
        if tag not in target.setdefault("tags", []):
            target["tags"].append(tag)
    return adopted


def _rekey_enriched(old_id, new_id):
    """Move a gitignored enriched.json entry to a record's new id, so contact
    data does not stay stranded under a dead key."""
    if not os.path.exists(ENRICHED_PATH):
        return
    try:
        enriched = load_json(ENRICHED_PATH, {})
    except (json.JSONDecodeError, OSError):
        return
    if old_id not in enriched:
        return
    if new_id in enriched:
        print(f"  ! enriched.json already has an entry for {new_id}; "
              f"leaving the {old_id} entry in place for a manual look")
        return
    enriched[new_id] = enriched.pop(old_id)
    write_file_atomic(ENRICHED_PATH,
                      json.dumps(enriched, indent=2, ensure_ascii=False) + "\n")
    print(f"  enriched.json re-keyed: {old_id} -> {new_id}")


def _reid_person(store, rec, person, title=None, location=None, tag=None):
    """Fill the person on a company-level record, which changes its id.
    Merges into an existing person record if one is already stored; the
    merge is strongest-wins (approval, enrichment, desk-check and currency
    survive it), and enriched.json follows the id."""
    old_id = rec["id"]
    new_id = candidate_id(person, rec["company"])
    target = store["candidates"].get(new_id)
    if target and target is not rec:
        # Person record already exists: stack the company-level signals on it
        # and carry the decision/enrichment state across.
        for sig in rec["signals"]:
            if not any(s["type"] == sig["type"]
                       and s["source_url"] == sig["source_url"]
                       for s in target["signals"]):
                target["signals"].append(sig)
        _merge_records(target, rec)
        del store["candidates"][old_id]
        _rekey_enriched(old_id, new_id)
        rec = target
    else:
        store["candidates"].pop(old_id, None)
        rec["id"] = new_id
        rec["person"] = person
        store["candidates"][new_id] = rec
        _rekey_enriched(old_id, new_id)
    if title and not rec.get("title"):
        rec["title"] = title
    if location and not rec.get("location"):
        rec["location"] = location
    if tag and tag not in rec.setdefault("tags", []):
        rec["tags"].append(tag)
    print(f"  {old_id} -> {new_id} ({person}{', ' + title if title else ''})")
    return rec


def cmd_set(args):
    """Human corrections and additions to one record (the store's fields are
    machine-filled; this is the hand on the tiller)."""
    store = load_store()
    rec = store["candidates"].get(args.id) or die(f"no candidate '{args.id}'")
    if args.person:
        rec = _reid_person(store, rec, args.person, args.title or None,
                           args.location or None,
                           tag="resolved-manually" if not rec.get("person")
                           else None)
    for field, val in (("title", args.title), ("location", args.location),
                       ("location_basis", args.location_basis),
                       ("sector", args.sector), ("size", args.size)):
        if val:
            rec[field] = val
            print(f"  {rec['id']}.{field} = {val}")
    if args.tag:
        if args.tag not in rec.setdefault("tags", []):
            rec["tags"].append(args.tag)
        print(f"  {rec['id']}.tags += {args.tag}")
    save_store(store)


def cmd_verify(args):
    """Currency check: is the person still in the seat the story put them in?
    Required before approving anyone whose story is older than a year.
    Manual paths record your own check; the API path uses people/search.
    If someone moved, two records replace the stale one: the successor in
    the seat, and the person at their new company tagged champion-moved."""
    config = load_config()
    store = load_store()
    rec = store["candidates"].get(args.id) or die(f"no candidate '{args.id}'")
    if not rec.get("person"):
        die("nothing to verify on a person-less record; resolve it first")

    if args.in_seat:
        rec["currency"] = {"checked": today(), "in_seat": True,
                           "method": "manual", "note": args.note or ""}
        save_store(store)
        print(f"[verify] {args.id}: confirmed still in seat ({today()})")
        return

    if args.moved:
        if not args.new_company:
            die("--moved needs --new-company (and ideally --new-title)")
        if not rec["signals"]:
            die(f"{args.id} has no signals to carry across; record the move "
                "by hand (`set`) or add the signal first (`paste`)")
        rec["currency"] = {"checked": today(), "in_seat": False,
                           "method": "manual",
                           "note": f"moved to {args.new_company}. "
                                   + (args.note or "")}
        rec["status"] = "superseded"
        rec["decided_date"] = today()
        annotated = []
        for sig in rec["signals"]:
            s = dict(sig)
            s["evidence"] = (f"(as {rec.get('title') or 'their prior role'} "
                             f"at {rec['company']}) " + s["evidence"])
            annotated.append(s)
        # Record 1: the seat at the original company, successor named or not.
        seat_id, created, _ = upsert(
            store, args.successor or None, args.successor_title or None,
            rec["company"], rec.get("sector"), rec.get("size"),
            dict(rec["signals"][0],
                 evidence="(inherited seat) " + rec["signals"][0]["evidence"]))
        for sig in rec["signals"][1:]:
            upsert(store, args.successor or None, args.successor_title or None,
                   rec["company"], None, None,
                   dict(sig, evidence="(inherited seat) " + sig["evidence"]))
        seat = store["candidates"][seat_id]
        if "successor-seat" not in seat.setdefault("tags", []):
            seat["tags"].append("successor-seat")
        # Record 2: the champion at the new company, signals travelling along.
        mover_id, _, _ = upsert(
            store, rec["person"], args.new_title or None, args.new_company,
            None, None, annotated[0])
        for s in annotated[1:]:
            upsert(store, rec["person"], args.new_title or None,
                   args.new_company, None, None, s)
        mover = store["candidates"][mover_id]
        if "champion-moved" not in mover.setdefault("tags", []):
            mover["tags"].append("champion-moved")
        if rec.get("location") and not mover.get("location"):
            mover["location"] = rec["location"]
        # The move check IS a currency check on the mover: we just placed
        # them in the new seat today, so approving them is not re-refused.
        mover["currency"] = {"checked": today(), "in_seat": True,
                             "method": "moved-verify",
                             "note": f"seat verified in the move check "
                                     f"from {rec['company']}"}
        rec["superseded_by"] = [seat_id, mover_id]
        save_store(store)
        print(f"[verify] {args.id}: moved. Created/updated:\n"
              f"  seat at {rec['company']}: {seat_id}"
              + (" (person-less; resolve it)" if not seat.get("person") else "")
              + f"\n  champion-moved: {mover_id} at {args.new_company}")
        _remind_durability()
        return

    # API path: does the person still appear in the seat at the company?
    print(f"[verify] {args.id}: checking {rec['person']} at {rec['company']} "
          "via people/search")
    titles = ([rec["title"]] if rec.get("title") else []) \
        + config.get("fullenrich", {}).get("resolve_titles", [])
    people, note = _search_people(
        {"job_titles": titles, "company_names": [rec["company"]]}, config)
    if people is None:
        print("[verify] record the check manually meanwhile:\n"
              f"  verify --id {args.id} --in-seat\n"
              f"  verify --id {args.id} --moved --new-company \"X\" "
              "[--new-title T] [--successor \"Name\" --successor-title T]")
        return
    names = [_person_fields(p)[0].lower() for p in people]
    if rec["person"].lower() in names:
        rec["currency"] = {"checked": today(), "in_seat": True,
                           "method": "search-api", "note": ""}
        save_store(store)
        print(f"[verify] {args.id}: still in seat (search-api, {today()})")
    else:
        print(f"[verify] {rec['person']} NOT among current seat-holders:")
        for i, p in enumerate(people, 1):
            name, title, comp, loc = _person_fields(p)
            print(f"  {i}. {name} — {title} ({loc or 'location ?'})")
        print("Record the move (creates successor + champion-moved records):\n"
              f"  verify --id {args.id} --moved --new-company \"X\" "
              "[--new-title T] [--successor \"Name\" --successor-title T]")


def cmd_dump(args):
    _dump_store(load_store(), "on demand")


def cmd_import(args):
    incoming = json.loads(read_file(args.file))
    if "candidates" not in incoming:
        die("that file is not a store dump (no 'candidates' key)")
    if args.replace:
        save_store(incoming)
        print(f"[import] store replaced: {len(incoming['candidates'])} "
              "candidate(s)")
        return
    store = load_store()
    added, merged, advanced = 0, 0, 0
    for cid, rec in incoming["candidates"].items():
        if cid not in store["candidates"]:
            store["candidates"][cid] = rec
            added += 1
        else:
            existing = store["candidates"][cid]
            for sig in rec.get("signals", []):
                if not any(s["type"] == sig["type"]
                           and s["source_url"] == sig["source_url"]
                           for s in existing["signals"]):
                    existing["signals"].append(sig)
                    merged += 1
            # A dump made after decisions were taken must not lose them:
            # adopt status/enrichment/desk-check/currency when the incoming
            # record is further along (strongest-wins, same as _reid_person).
            if _merge_records(existing, rec, verbose=True):
                advanced += 1
    save_store(store)
    print(f"[import] merged: {added} new record(s), {merged} new signal(s), "
          f"{advanced} record(s) advanced from the dump")


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

def _export_dir_allowed(path):
    """(ok, why). Export files hold FullEnrich contact data, so the output
    dir must be gitignored: under research/ or ignored per git check-ignore.
    Anything else risks contact data landing on a code branch."""
    ab = os.path.abspath(path)
    research_root = os.path.abspath(RESEARCH_DIR)
    if ab == research_root or ab.startswith(research_root + os.sep):
        return True, ""
    try:
        proc = subprocess.run(["git", "-C", REPO_ROOT, "check-ignore", "-q",
                               ab], capture_output=True, text=True)
        if proc.returncode == 0:
            return True, ""
    except OSError:
        pass
    return False, (f"{path} is not under research/ and git check-ignore "
                   "does not ignore it, so the contact data in the export "
                   "could be committed to a code branch. Use the default "
                   "(research/sourcing-<date>/) or a gitignored path.")


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
    ok, why = _export_dir_allowed(out_dir)
    if not ok:
        die("--out refused: " + why)
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
        cur = r.get("currency") or {}
        lines += [f"- Title: {r.get('title') or 'unknown'}",
                  f"- Location (the desk, not HQ): "
                  f"{r.get('location') or 'unknown'}"
                  + (f" ({r['location_basis']})"
                     if r.get("location_basis") else ""),
                  f"- Sector: {r.get('sector') or 'unknown'}",
                  f"- Size: {r.get('size') or 'unknown'}",
                  f"- Tags: {', '.join(r.get('tags') or []) or 'none'}",
                  "- Currency: "
                  + (f"confirmed in seat {cur['checked']} ({cur['method']})"
                     if cur.get("in_seat")
                     else "not checked (story fresh enough not to require it)"),
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


# --- probe: regression-test the sourcing logic --------------------------------
# Free (no model or network calls) and safe (every store and ledger path is
# pointed at a tempdir before a byte is written). Pins the fixes from the
# July 2026 adversarial review: hold bypasses (H1-H5), approval bypasses
# (A1-A3), money leaks (M1-M3), data loss/corruption (D1-D6), scoring
# visibility (S1-S2), location classing (L1) and parsing (P1-P4). Run after
# any edit to sourcing.py, its modules or config/scoring.

def cmd_probe(args):
    global STORE_PATH, ENRICHED_PATH, OUTCOMES_PATH, CAPTURE_PATH, PIECES_DIR
    td = tempfile.mkdtemp(prefix="sourcing-probe-")
    STORE_PATH = os.path.join(td, "candidates.json")
    ENRICHED_PATH = os.path.join(td, "enriched.json")
    OUTCOMES_PATH = os.path.join(td, "outcomes.json")
    CAPTURE_PATH = os.path.join(td, "capture.json")
    PIECES_DIR = os.path.join(td, "pieces")
    real_load_env_key = globals()["load_env_key"]
    globals()["load_env_key"] = lambda name: ""  # stub mode, never keyed

    results = []

    def check(label, ok, detail=""):
        results.append((label, ok))
        line = ("  PASS  " if ok else "  FAIL  ") + label
        if detail and not ok:
            line += "\n          " + str(detail).strip().replace(
                "\n", "\n          ")
        print(line)

    def quiet(fn):
        """Run fn with stdout captured. Returns (value, output)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            val = fn()
        return val, buf.getvalue()

    def expect_die(fn):
        """True when fn halts via die() (clean refusal, not a traceback)."""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fn()
        except SystemExit:
            return True, buf.getvalue()
        except Exception as exc:  # a traceback is exactly the failure mode
            return False, f"raised {type(exc).__name__}: {exc}"
        return False, buf.getvalue()

    def days_ago(n):
        return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

    def base_rec(cid, status="new", person=None, company="Acme", **kw):
        rec = {"id": cid, "person": person, "title": None, "company": company,
               "sector": None, "size": None, "location": None,
               "location_basis": None, "tags": [], "status": status,
               "date_discovered": today(), "decided_date": None,
               "signals": [], "currency": None, "enrichment": None,
               "desk_check": None, "piece_slug": None}
        rec.update(kw)
        return rec

    def set_ledgers(outcomes, capture, pieces=None):
        write_file(OUTCOMES_PATH, json.dumps(outcomes))
        write_file(CAPTURE_PATH, json.dumps(capture))
        shutil.rmtree(PIECES_DIR, ignore_errors=True)
        os.makedirs(PIECES_DIR)
        for folder, meta in (pieces or {}).items():
            os.makedirs(os.path.join(PIECES_DIR, folder))
            if meta is not None:
                write_file(os.path.join(PIECES_DIR, folder, "meta.json"),
                           json.dumps(meta))

    scoring = {"signals": {"abm-hiring": {"points": 3, "max_age_days": 90},
                           "gifting-case-studies": {"points": 3,
                                                    "max_age_days": 730}},
               "undated_signals_use_discovery_date": True}
    cfg = {"thread_hold": {"live_results": ["replied", "opportunity"],
                           "followup_window_days": 14},
           "currency_check_after_days": 365}

    print("\n" + "=" * 70)
    print("SOURCING PROBE — pure logic + store safety (no model/network calls)")
    print("=" * 70)
    try:
        print("\n[company_key H3]")
        check("'Acme Ltd.' == 'acme ltd' == 'Acme'",
              company_key("Acme Ltd.") == company_key("acme ltd")
              == company_key("Acme") == "acme")
        check("empty stays empty", company_key("") == "")
        check("suffix-only name survives", company_key("Ltd") == "ltd")

        print("\n[_outcome_state H4]")
        st = lambda rec: _outcome_state(rec, cfg)
        check("no_response 14d ago is live",
              st({"result": "no_response", "date": days_ago(14)}) == "live")
        check("no_response 15d ago is closed",
              st({"result": "no_response", "date": days_ago(15)}) == "closed")
        check("garbage date fails open (hold stays)",
              st({"result": "no_response", "date": "soonish"}) == "live")
        check("absent date fails open",
              st({"result": "no_response"}) == "live")
        check("replied is live", st({"result": "replied"}) == "live")
        check("declined is closed", st({"result": "declined"}) == "closed")
        check("meeting is closed", st({"result": "meeting"}) == "closed")
        this_month = datetime.date.today().strftime("%Y-%m")
        check("month-only date pins to month END: current month stays live",
              st({"result": "no_response", "date": this_month}) == "live")
        check("full first_reply_date beats a coarse month",
              st({"result": "no_response", "date": "2020-01",
                  "first_reply_date": days_ago(5)}) == "live")

        print("\n[live_threads H1/H2/H5]")
        set_ledgers({"old-piece-acme": {"company": "Acme Ltd",
                                        "result": "declined",
                                        "date": "2026-01"}},
                    {}, pieces={"jane-doe-acme":
                                {"recipient_company": "Acme"}})
        threads, _ = quiet(lambda: live_threads(cfg))
        check("closed outcome for Acme does not hide a NEWER in-flight piece",
              "acme" in threads and "in flight" in threads["acme"],
              str(threads))
        set_ledgers({"bob-roe-globex": {"company": "Globex",
                                        "result": "meeting",
                                        "date": "2026-06"}},
                    {"bob-roe-globex": {"first_name": "Bob",
                                        "status": "tease-sent"}})
        threads, _ = quiet(lambda: live_threads(cfg))
        check("capture slug resolves company via outcomes.json",
              "globex" in threads and "capture" in threads["globex"],
              str(threads))
        store_fix = {"version": 1, "candidates": {
            "lisa-may-initech": base_rec("lisa-may-initech",
                                         person="Lisa May",
                                         company="Initech")}}
        set_ledgers({}, {"cp-31-lisa-initech": {"first_name": "Lisa",
                                                "status": "printed"}})
        threads, _ = quiet(lambda: live_threads(cfg, store_fix))
        check("capture slug tail matches a candidate company",
              "initech" in threads, str(threads))
        set_ledgers({}, {"zzz-mystery": {"first_name": "Z",
                                         "status": "tease-sent"}})
        threads, _ = quiet(lambda: live_threads(cfg))
        check("unresolvable capture entry surfaces as an UNRESOLVED hold",
              "unresolved:zzz-mystery" in threads, str(threads))
        set_ledgers({}, {}, pieces={"cp-40-sam-hooli": None})
        threads, _ = quiet(lambda: live_threads(cfg))
        check("meta-less hyphenated piece folder is an UNRESOLVED hold, "
              "not invisible",
              "unresolved:cp-40-sam-hooli" in threads, str(threads))
        set_ledgers({}, {})
        threads, _ = quiet(lambda: live_threads(cfg))
        check("empty ledgers mean no holds", threads == {}, str(threads))

        print("\n[needs_currency_check A1/A2]")
        def cur_rec(sig_date, currency=None):
            return {"signals": [{"type": "abm-hiring", "evidence": "",
                                 "source_url": "u", "signal_date": sig_date,
                                 "date_added": today()}],
                    "currency": currency}
        check("'March 2023' (present, unparseable) fails closed",
              needs_currency_check(cur_rec("March 2023"), scoring, cfg))
        check("old ISO date requires the check",
              needs_currency_check(cur_rec("2023-01-01"), scoring, cfg))
        check("364d-old date does not",
              not needs_currency_check(cur_rec(days_ago(364)), scoring, cfg))
        check("undated does not",
              not needs_currency_check(cur_rec(None), scoring, cfg))
        stale = {"checked": days_ago(400), "in_seat": True}
        check("a currency check EXPIRES after currency_check_after_days",
              needs_currency_check(cur_rec("2023-01-01", stale),
                                   scoring, cfg))
        fresh = {"checked": days_ago(10), "in_seat": True}
        check("a fresh check still satisfies the gate",
              not needs_currency_check(cur_rec("2023-01-01", fresh),
                                       scoring, cfg))

        print("\n[signal_points S2 + ageing]")
        def sig(t, sd=None, da=None):
            return {"type": t, "evidence": "", "source_url": "u",
                    "signal_date": sd, "date_added": da or today()}
        check("age == max_age_days still scores",
              signal_points(sig("abm-hiring", days_ago(90)), scoring) == 3)
        check("max_age_days + 1 scores zero",
              signal_points(sig("abm-hiring", days_ago(91)), scoring) == 0)
        check("undated falls back to date_added",
              signal_points(sig("abm-hiring"), scoring) == 3)
        val, out = quiet(lambda: signal_points(sig("mystery-signal",
                                                   days_ago(1)), scoring))
        check("unknown type scores 0 and warns once",
              val == 0 and "unknown signal type" in out, out)
        val, out = quiet(lambda: signal_points(sig("mystery-signal",
                                                   days_ago(1)), scoring))
        check("second unknown-type hit does not re-warn", out == "")
        check("gifting window: 730d scores, 731d does not",
              signal_points(sig("gifting-case-studies", days_ago(730)),
                            scoring) == 3
              and signal_points(sig("gifting-case-studies", days_ago(731)),
                                scoring) == 0)

        print("\n[upsert]")
        st_up = {"version": 1, "candidates": {}}
        cid, created, a1 = upsert(st_up, "Jane Doe", "VP", "Acme", None,
                                  None, make_signal("abm-hiring", "e",
                                                    "http://a", None))
        _, _, a2 = upsert(st_up, "Jane Doe", "VP", "Acme", None, None,
                          make_signal("abm-hiring", "e2", "http://a", None))
        check("same (type, source_url) twice is one signal",
              a1 and not a2
              and len(st_up["candidates"][cid]["signals"]) == 1)
        _, _, a3 = upsert(st_up, "Jane Doe", "VP", "Acme", None, None,
                          make_signal("abm-hiring", "e3", "http://b", None))
        check("same type, new url is a second signal",
              a3 and len(st_up["candidates"][cid]["signals"]) == 2)
        bid, _, _ = upsert(st_up, "Bob Roe", "", "Globex", None, None,
                           make_signal("abm-hiring", "e", "http://c", None))
        upsert(st_up, "Bob Roe", "CMO", "Globex", None, None,
               make_signal("abm-hiring", "e", "http://d", None))
        upsert(st_up, "Bob Roe", "CEO", "Globex", None, None,
               make_signal("abm-hiring", "e", "http://e", None))
        check("gap-fill fills empties but never overwrites",
              st_up["candidates"][bid]["title"] == "CMO")

        print("\n[parse_structured D5/P3]")
        r = parse_structured("COMPANY: A\nURL: http://a\n---\n"
                             "COMPANY: B\nURL: http://b\n")
        check("--- separated blocks parse to 2 records with own URLs",
              len(r) == 2 and r[0]["source_url"] == "http://a"
              and r[1]["source_url"] == "http://b", str(r))
        r = parse_structured("COMPANY: A\nPERSON: Jane\nURL: http://a\n\n"
                             "COMPANY: B\n")
        check("blank-line separated blocks do not bleed fields",
              len(r) == 2 and r[1].get("person") is None
              and r[1].get("source_url") is None, str(r))
        r = parse_structured("We spoke to Acme.\n"
                             "Company: Acme is a fine business.\n")
        check("freeform prose mentioning 'Company:' is NOT structured",
              r == [], str(r))

        print("\n[_reid_person D1]")
        write_file(ENRICHED_PATH,
                   json.dumps({"co--acme": {"date": "x", "raw": {"e": 1}}}))
        src = base_rec("co--acme", status="approved",
                       decided_date="2026-01-01",
                       enrichment={"done": True, "date": "2026-01-02",
                                   "fields_found": ["emails"]},
                       desk_check={"verdict": "office-confirmed",
                                   "sources": [{"evidence": "x",
                                                "url": "u"}],
                                   "date": "2026-01-03"},
                       currency={"checked": today(), "in_seat": True,
                                 "method": "manual", "note": ""},
                       signals=[make_signal("abm-hiring", "co ev",
                                            "http://co", None)])
        tgt = base_rec("jane-doe-acme", person="Jane Doe",
                       signals=[make_signal("gifting-case-studies", "p ev",
                                            "http://p", None)])
        st_re = {"version": 1,
                 "candidates": {"co--acme": src, "jane-doe-acme": tgt}}
        _, out = quiet(lambda: _reid_person(st_re, src, "Jane Doe"))
        got = st_re["candidates"].get("jane-doe-acme") or {}
        check("merge keeps approval, enrichment, desk-check and currency",
              "co--acme" not in st_re["candidates"]
              and got.get("status") == "approved"
              and (got.get("enrichment") or {}).get("done")
              and got.get("desk_check") and got.get("currency")
              and got.get("decided_date") == "2026-01-01"
              and len(got.get("signals", [])) == 2, str(got))
        enr = load_json(ENRICHED_PATH, {})
        check("enriched.json re-keyed to the new id",
              "jane-doe-acme" in enr and "co--acme" not in enr, str(enr))

        print("\n[import merge D2]")
        _, _ = quiet(lambda: save_store(
            {"version": 1, "candidates":
             {"jane-acme": base_rec("jane-acme", person="Jane",
                                    company="Acme")}}))
        dump_path = os.path.join(td, "dump.json")
        write_file(dump_path, json.dumps(
            {"version": 1, "candidates":
             {"jane-acme": base_rec(
                 "jane-acme", person="Jane", company="Acme",
                 status="approved", decided_date="2026-02-01",
                 enrichment={"done": True, "date": "2026-02-02",
                             "fields_found": []})}}))
        _, out = quiet(lambda: cmd_import(argparse.Namespace(
            file=dump_path, replace=False)))
        got = load_store()["candidates"]["jane-acme"]
        check("merge import adopts the further-along status + enrichment",
              got["status"] == "approved"
              and (got.get("enrichment") or {}).get("done")
              and got["decided_date"] == "2026-02-01", str(got))
        replace_dump = {"version": 1, "candidates":
                        {"solo-corp": base_rec("solo-corp", person="Solo",
                                               company="Corp")}}
        write_file(dump_path, json.dumps(replace_dump))
        _, _ = quiet(lambda: cmd_import(argparse.Namespace(
            file=dump_path, replace=True)))
        check("--replace roundtrips the dump exactly",
              load_store()["candidates"] == replace_dump["candidates"])

        print("\n[verify --moved A3/D6]")
        _, _ = quiet(lambda: save_store(
            {"version": 1, "candidates":
             {"old-guy-acme": base_rec(
                 "old-guy-acme", person="Old Guy", company="Acme",
                 title="CMO", location="London, UK",
                 signals=[make_signal("gifting-case-studies", "bought",
                                      "http://cs", "2024-01-01")])}}))
        ns_moved = argparse.Namespace(
            id="old-guy-acme", in_seat=False, moved=True,
            new_company="Globex", new_title="CMO", successor="",
            successor_title="", note="")
        _, out = quiet(lambda: cmd_verify(ns_moved))
        st_v = load_store()["candidates"]
        old = st_v.get("old-guy-acme") or {}
        seat = st_v.get("co--acme") or {}
        mover = st_v.get("old-guy-globex") or {}
        check("move creates exactly the seat + champion records",
              len(st_v) == 3 and old.get("status") == "superseded"
              and old.get("superseded_by") == ["co--acme",
                                               "old-guy-globex"],
              str(sorted(st_v)))
        check("seat record tagged successor-seat",
              "successor-seat" in (seat.get("tags") or []))
        check("mover tagged champion-moved with currency stamped "
              "moved-verify",
              "champion-moved" in (mover.get("tags") or [])
              and (mover.get("currency") or {}).get("method")
              == "moved-verify"
              and (mover.get("currency") or {}).get("in_seat") is True,
              str(mover.get("currency")))
        _, _ = quiet(lambda: save_store(
            {"version": 1, "candidates":
             {"no-sig-acme": base_rec("no-sig-acme", person="No Sig",
                                      company="Acme")}}))
        ok, out = expect_die(lambda: cmd_verify(argparse.Namespace(
            id="no-sig-acme", in_seat=False, moved=True,
            new_company="Globex", new_title="", successor="",
            successor_title="", note="")))
        check("zero-signal --moved refuses cleanly (no traceback)", ok, out)

        print("\n[location_class L1]")
        real_cfg = load_config()
        lc = lambda loc: location_class({"location": loc}, real_cfg)
        check("'Newcastle upon Tyne' is uk", lc("Newcastle upon Tyne") == "uk")
        check("'Boston, MA' is non-uk", lc("Boston, MA") == "non-uk")
        check("'New York, NY' is non-uk (york does not hijack it)",
              lc("New York, NY") == "non-uk")
        check("'Erewhon' is unknown, not banked", lc("Erewhon") == "unknown")
        check("empty location is unknown", lc("") == "unknown")

        print("\n[enrich guards M1/M2 (stub, no key)]")
        _, _ = quiet(lambda: save_store({"version": 1, "candidates": {
            "r1-acme": base_rec("r1-acme", person="R One", company="Acme1"),
            "r2-globex": base_rec("r2-globex", status="approved",
                                  person="R Two", company="Globex2",
                                  enrichment={"done": True, "date": "x",
                                              "fields_found": []}),
            "r3-hooli": base_rec("r3-hooli", status="approved",
                                 person="R Three", company="Hooli3",
                                 enrichment={"pending_id": "enr-abc",
                                             "pending_date": "x"}),
            "r4-initech": base_rec("r4-initech", status="approved",
                                   person="R Four", company="Initech4"),
        }}))
        def ens(ids, re_enrich=False):
            return argparse.Namespace(ids=ids, phones=False, domain="",
                                      re_enrich=re_enrich, poll="")
        ok, out = expect_die(lambda: cmd_enrich(ens(["r1-acme"])))
        check("enrich --id refuses a non-approved record", ok, out)
        ok, out = expect_die(lambda: cmd_enrich(ens(["r2-globex"])))
        check("enrich --id refuses an already-done record without "
              "--re-enrich", ok, out)
        ok, out = expect_die(lambda: cmd_enrich(ens(["r3-hooli"])))
        check("enrich --id refuses a pending_id record (points at --poll)",
              ok and "--poll" in out, out)
        _, out = quiet(lambda: cmd_enrich(ens(["r4-initech"])))
        check("clean approved record reaches the stub without spending",
              "STUB MODE" in out, out)
        _, out = quiet(lambda: cmd_enrich(ens(None)))
        check("default batch submits only clean records, flags the pending",
              "STUB MODE" in out
              and '"candidate_id": "r4-initech"' in out
              and '"candidate_id": "r2-globex"' not in out
              and '"candidate_id": "r3-hooli"' not in out
              and "enrich --poll" in out, out)
        got = load_store()["candidates"]["r4-initech"]
        check("stub run leaves no pending_id behind",
              not (got.get("enrichment") or {}).get("pending_id"))
    finally:
        globals()["load_env_key"] = real_load_env_key
        shutil.rmtree(td, ignore_errors=True)

    fails = [label for label, ok in results if not ok]
    print("\n" + "-" * 70)
    if fails:
        print(f"{len(fails)} of {len(results)} probe(s) FAILED. Do not trust "
              "holds, gates or the store until fixed.")
        sys.exit(1)
    print(f"All {len(results)} probes passed. Holds, gates, merges and "
          "parsers behave.")


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

    p = sub.add_parser("shortlist", help="ranked candidates with evidence "
                                         "(UK view by default)")
    p.add_argument("--all", action="store_true",
                   help="include rejected, exported and superseded")
    p.add_argument("--world", action="store_true",
                   help="show non-UK names too (they stay banked either way)")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_shortlist)

    p = sub.add_parser("set", help="hand-correct one record's fields")
    p.add_argument("--id", required=True)
    p.add_argument("--person", default="",
                   help="name the person on a company-level record (re-ids it)")
    p.add_argument("--title", default="")
    p.add_argument("--location", default="",
                   help="where the person sits (the desk, not company HQ)")
    p.add_argument("--location-basis", default="",
                   help="one line on where the location came from")
    p.add_argument("--sector", default="")
    p.add_argument("--size", default="")
    p.add_argument("--tag", default="")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("verify",
                       help="currency check: still in the seat? Required "
                            "before approving stories older than a year")
    p.add_argument("--id", required=True)
    p.add_argument("--in-seat", action="store_true",
                   help="record a manual confirmation they are still in role")
    p.add_argument("--moved", action="store_true",
                   help="record a move: supersedes this record and creates "
                        "the successor seat + a champion-moved record")
    p.add_argument("--new-company", default="")
    p.add_argument("--new-title", default="")
    p.add_argument("--successor", default="",
                   help="who now holds the seat at the original company")
    p.add_argument("--successor-title", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("dump", help="print the full store (durability copy)")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("import", help="restore/merge a store dump")
    p.add_argument("--file", required=True)
    p.add_argument("--replace", action="store_true",
                   help="replace the store wholesale instead of merging")
    p.set_defaults(func=cmd_import)

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
    p.add_argument("--re-enrich", dest="re_enrich", action="store_true",
                   help="spend again on records already enriched (--id runs)")
    p.add_argument("--poll", default="",
                   help="resume polling a submitted enrichment_id without "
                        "re-submitting (never double-spends)")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("resolve",
                       help="Search-API person resolution for shortlisted "
                            "person-less records (0.25 credits per person "
                            "exported; stub without key)")
    p.add_argument("--id", dest="ids", action="append")
    p.add_argument("--assign", default="",
                   help="assign search result N to the record (single-id runs)")
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

    p = sub.add_parser("probe",
                       help="regression-test the sourcing logic (free: no "
                            "model or network calls, temp stores only)")
    p.set_defaults(func=cmd_probe)

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
