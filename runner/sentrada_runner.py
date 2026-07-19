#!/usr/bin/env python3
"""
Sentrada chain runner.

Runs the Sentrada prompt chain to produce one-of-one physical outreach pieces.
Model calls go through the Claude Code CLI in headless mode (`claude -p`), which
draws on the logged-in Max subscription's credit pool rather than a pay-per-token
API key. Research (Prompt 1) happens outside this runner: you paste your deep
research in as a file. The runner then executes Prompt 2 (brief), pauses for your
approval, runs Prompt 4 (copy), and either renders a newspaper with the layout
engine or assembles a paste-ready claymation image prompt.

Three commands:

  generate  Run the main pipeline for one piece.
  qc        Run Prompts 6 and 6B (vision) against a final image.
  followup  Run Prompt 7 to write the companion card and 3-touch follow-up.

Prompt text lives in runner/templates/*.md, never in this file. Per-prompt models
live in config.json under "models". This file only fills placeholders, calls
`claude -p`, gates the output, and drives the layout engine.

Requires the `claude` CLI logged in (claude /login). No ANTHROPIC_API_KEY is
needed unless config sets "vision_backend": "sdk".

Usage:
  python sentrada_runner.py generate --name "Jane Doe" --title "VP Sales" \\
      --company "Acme" --format newspaper --research path/to/research.md

  python sentrada_runner.py qc --folder pieces/jane-doe-acme --image final.png
  python sentrada_runner.py followup --folder pieces/jane-doe-acme --delivery-date "16 June 2026"
  python sentrada_runner.py ship-check --all   # gate before staging PNGs for print
  python sentrada_runner.py birch-csv --manifest batch.json  # shipping CSV (addresses; never commit)
"""

import argparse
import base64
import csv
import datetime
import filecmp
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

import tempfile
import urllib.error
import urllib.request

# --- Paths -----------------------------------------------------------------

RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(RUNNER_DIR)
TEMPLATE_DIR = os.path.join(RUNNER_DIR, "templates")
PIECES_DIR = os.path.join(RUNNER_DIR, "pieces")
DEFAULT_CONFIG = os.path.join(RUNNER_DIR, "config.json")

# --- Model calls go through `claude -p` (Claude Code headless) --------------
# This draws on the logged-in Max subscription's credit pool rather than a
# pay-per-token API key. Per-prompt models live in config.json under "models";
# these are the defaults if a key is missing. "opus"/"sonnet"/"haiku" are
# aliases the CLI resolves to the current model; full IDs also work.
DEFAULT_MODELS = {
    "p1b": "sonnet",   # research gate: completeness check on pasted research
    "p2": "opus",      # brief: picks the problem angle (quality-critical)
    "p4": "opus",      # copy: writes what appears on the piece (quality-critical)
    "p4b": "opus",     # factual-grounding gate on the copy (accuracy-critical)
    "p5": "sonnet",    # claymation image-prompt assembly (mechanical)
    "p7": "sonnet",    # follow-up copy (template-driven transformation)
    "p6": "opus",      # review (vision) — strongest available
    "p6b": "opus",     # recipient simulation (vision) — strongest available
}

# Appended (not replacing) so the CLI keeps its OAuth/credential loading intact;
# replacing the system prompt or using --bare breaks subscription auth.
APPEND_SYSTEM = (
    "For this task, act strictly as the role described in the user message and "
    "produce exactly the output it specifies, including any fenced JSON block. "
    "Do not use any tools unless the user gives you a file path to read.")

# A neutral working directory so the project CLAUDE.md is never auto-loaded into
# a generation call (it would bias the copy and bloat every request).
CLI_CWD = os.path.join(tempfile.gettempdir(), "sentrada_cli_cwd")
os.makedirs(CLI_CWD, exist_ok=True)

# Map CLI aliases to full API model IDs, for the optional SDK vision fallback.
ALIAS_TO_ID = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-4-6",
               "haiku": "claude-haiku-4-5"}

# Only these fields may go into the engine's data.json. Everything else (fact
# check list, prose) stays out of the engine input.
ENGINE_FIELDS = [
    "masthead_name", "edition_line", "date", "headline", "byline", "lead_article",
    "pull_quote_text", "pull_quote_attribution", "stat_number", "stat_descriptor",
    "stat_source", "kicker_text",
    "sidebar_1_headline", "sidebar_1_byline", "sidebar_1_body",
    "sidebar_2_headline", "sidebar_2_byline", "sidebar_2_body",
    "sidebar_3_headline", "sidebar_3_byline", "sidebar_3_body",
]


# --- Small helpers ----------------------------------------------------------

def die(msg):
    print(f"\n[halt] {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(path):
    if not os.path.exists(path):
        die(f"config not found at {path}. Copy config.example.json to config.json "
            f"and fill in your sender profile.")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_template(name):
    with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as fh:
        text = fh.read()
    # Shared house rules are a template include, expanded before fill() so the
    # cross-cutting copy rules are edited once (house_rules.md), not per template.
    if "{{house_rules}}" in text and name != "house_rules.md":
        with open(os.path.join(TEMPLATE_DIR, "house_rules.md"), "r", encoding="utf-8") as fh:
            rules = fh.read()
        rules = re.sub(r"<!--.*?-->\s*", "", rules, count=1, flags=re.DOTALL)
        text = text.replace("{{house_rules}}", rules.strip())
    return text


def fill(template, values):
    """Replace every {{key}} with values[key]. Missing keys become empty so an
    unused placeholder never leaks {{...}} into the prompt."""
    out = template
    for key, val in values.items():
        out = out.replace("{{" + key + "}}", str(val))
    out = re.sub(r"\{\{[a-z0-9_]+\}\}", "", out)  # clear any leftover placeholders
    return out


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "x"


def read_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_file(path, text):
    # Atomic: committed ledgers (outcomes.json, capture.json) go through here,
    # and a truncate-write killed mid-flight leaves a corrupt file that can be
    # committed unnoticed.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def extract_last_json(text):
    """Return the last fenced ```json ... ``` block parsed, or None."""
    blocks = re.findall(r"```json\s*(.*?)```", text, re.DOTALL)
    if not blocks:
        blocks = re.findall(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1].strip())
    except json.JSONDecodeError:
        return None


def split_factcheck(text):
    """Split a copy reply into (copy_part, factcheck_part)."""
    marker = "FACT CHECK LIST:"
    idx = text.find(marker)
    if idx == -1:
        return text, ""
    return text[:idx].rstrip(), text[idx + len(marker):].strip()


# --- Model calls via `claude -p` (headless CLI) -----------------------------

def model_for(config, key):
    """Per-prompt model from config['models'][key], falling back to DEFAULT_MODELS."""
    return config.get("models", {}).get(key, DEFAULT_MODELS[key])


# Per-run credit accounting. `claude -p` reports total_cost_usd per call; we sum it
# so each piece prints roughly how much of the subscription pool it consumed.
_USAGE = {"calls": 0, "cost_usd": 0.0}


def _usage_reset():
    _USAGE["calls"] = 0
    _USAGE["cost_usd"] = 0.0


def _print_usage(label="this run"):
    print(f"\n[credit] {label}: {_USAGE['calls']} model call(s), approx "
          f"${_USAGE['cost_usd']:.2f} subscription credit (USD-equivalent).")


def _cli_invoke(prompt, model, image_path=None, timeout=900):
    """One `claude -p` call. Returns the model's text. Raises on transport error
    or an error envelope (so the retry wrappers can re-try)."""
    cmd = ["claude", "-p", "--output-format", "json", "--model", model,
           "--append-system-prompt", APPEND_SYSTEM]
    if image_path:
        cmd += ["--allowed-tools", "Read"]
        prompt = (prompt + "\n\nThe image to review is at this path. Read it with "
                  "the Read tool, then complete the task above:\n@"
                  + os.path.abspath(image_path))
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              cwd=CLI_CWD, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude -p timed out after {timeout}s (model {model})")
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: "
                           + (proc.stderr or proc.stdout)[:500])
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("claude -p did not return a JSON envelope: "
                           + proc.stdout[:300])
    _USAGE["calls"] += 1
    _USAGE["cost_usd"] += float(env.get("total_cost_usd") or 0.0)
    if env.get("is_error") or env.get("subtype") != "success":
        raise RuntimeError("claude -p error: " + str(env.get("result", env))[:300])
    return (env.get("result") or "").strip()


def cli_text(prompt, model, image_path=None, retries=2):
    """Text (or vision) call with transient-error retries."""
    last = None
    for attempt in range(retries + 1):
        try:
            return _cli_invoke(prompt, model, image_path=image_path)
        except RuntimeError as exc:
            last = exc
            print(f"[retry] {model} call failed ({attempt + 1}/{retries + 1}): {exc}")
    raise SystemExit(f"\n[halt] model call failed after {retries + 1} attempts: {last}")


def cli_json(prompt, model, retries=3):
    """Call that must return a fenced ```json block. Retries with a reminder if the
    block is missing or unparseable. Returns (full_text, parsed_dict)."""
    last_text = ""
    for attempt in range(retries):
        p = prompt
        if attempt > 0:
            p = (prompt + "\n\nIMPORTANT: your previous reply did not end with a "
                 "valid fenced ```json block. Produce the full response and finish "
                 "with one valid ```json ... ``` block as the very last thing.")
        last_text = cli_text(p, model)
        data = extract_last_json(last_text)
        if data is not None:
            return last_text, data
        print(f"[retry] no valid JSON block from {model} "
              f"({attempt + 1}/{retries}); re-asking.")
    die("could not extract a valid JSON block after retries. Last reply began:\n"
        + last_text[:500])


def vision_call(config, prompt, model, image_path):
    """Vision call. Default backend is the CLI (subscription). Set
    config['vision_backend'] = 'sdk' to bill per-token via the Python SDK and
    ANTHROPIC_API_KEY instead."""
    if not os.path.exists(image_path):
        die(f"image not found: {image_path}")
    if config.get("vision_backend", "cli") == "sdk":
        return _sdk_vision(prompt, ALIAS_TO_ID.get(model, model), image_path)
    return cli_text(prompt, model, image_path=image_path)


def _sdk_vision(prompt, model_id, image_path):
    """Optional fallback: vision via the Anthropic Python SDK (needs an API key)."""
    try:
        import anthropic
    except ImportError:
        die("vision_backend is 'sdk' but the anthropic package is not installed.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        die("vision_backend is 'sdk' but ANTHROPIC_API_KEY is not set (this path "
            "bills per-token).")
    client = anthropic.Anthropic(max_retries=4)
    with open(image_path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("utf-8")
    media = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
        {"type": "text", "text": prompt},
    ]
    with client.messages.stream(model=model_id, max_tokens=16000,
                                messages=[{"role": "user", "content": content}]) as s:
        for _ in s.text_stream:
            pass
        msg = s.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# --- Newspaper gates and engine --------------------------------------------

def newspaper_violations(data):
    """The runner's gate, stricter than the engine: lead 600-640, exactly 3
    sidebars, each sidebar body 60-80 words."""
    v = []
    lead = len(str(data.get("lead_article", "")).split())
    if not (600 <= lead <= 640):
        v.append(f"lead_article is {lead} words; it must be 600-640 (target 620).")
    present = [n for n in (1, 2, 3, 4) if data.get(f"sidebar_{n}_headline")]
    if present != [1, 2, 3]:
        v.append(f"there must be exactly 3 sidebar stories (sidebar_1/2/3); found {present}.")
    for n in (1, 2, 3):
        wc = len(str(data.get(f"sidebar_{n}_body", "")).split())
        if not (60 <= wc <= 80):
            v.append(f"sidebar_{n}_body is {wc} words; it must be 60-80.")
    return v


def engine_data_only(data):
    """Keep only the engine schema fields, dropping anything else."""
    return {k: data[k] for k in ENGINE_FIELDS if k in data and data[k] not in (None, "")}


def run_engine(engine_path, template_path, data_path, output_path=None, check=False):
    cmd = [sys.executable, engine_path, "--template", template_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", "360"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if output_path and result.returncode == 0:
        _stamp_engine(output_path, engine_path)
    return result.returncode, result.stdout + result.stderr


# --- Crossword gates and engine --------------------------------------------
# Parallel to the newspaper helpers above; nothing here touches the newspaper
# path. crossword.py mirrors newspaper.py's CLI (--check validates and exits
# nonzero on FAIL without writing output; --output renders at 300 DPI), so the
# crossword path runs the same check-then-render pattern.

def crossword_violations(data):
    """The runner's structural gate on the P4 crossword candidates: 25-30 of
    them, every answer a single ALL-CAPS word, letters only, 3-15 characters,
    each with a clue, and no duplicate answers. Mirrors newspaper_violations."""
    v = []
    cands = data.get("candidates") or []
    n = len(cands)
    if not (25 <= n <= 30):
        v.append(f"there must be 25-30 candidates; found {n}.")
    seen = {}
    for i, c in enumerate(cands, 1):
        a = str(c.get("answer", ""))
        if not a:
            v.append(f"candidate {i} has no answer.")
        else:
            if not a.isalpha():
                v.append(f"candidate {i} answer '{a}' must be a single word, letters "
                         f"only (no spaces, digits, or punctuation).")
            elif a != a.upper():
                v.append(f"candidate {i} answer '{a}' must be ALL CAPS.")
            if not (3 <= len(a) <= 15):
                v.append(f"candidate {i} answer '{a}' is {len(a)} characters; must be 3-15.")
            seen[a.upper()] = seen.get(a.upper(), 0) + 1
        if not str(c.get("clue", "")).strip():
            v.append(f"candidate {i} ('{a}') has no clue.")
    dupes = sorted(k for k, count in seen.items() if count > 1)
    if dupes:
        v.append("duplicate answers (each answer must be unique): " + ", ".join(dupes))
    anchors = sum(1 for c in cands if c.get("anchor"))
    if anchors > 3:
        v.append(f"too many anchor candidates ({anchors}); mark at most 2-3 (the "
                 "brief's central concept) or the grid cannot place them all.")
    open_loops = [c for c in cands if c.get("open_loop")]
    if len(open_loops) > 1:
        v.append(f"{len(open_loops)} candidates are marked open_loop; exactly one "
                 "candidate may carry the open-loop flag.")
    for c in open_loops:
        if not c.get("anchor"):
            v.append(f"open-loop candidate '{c.get('answer', '')}' must also be "
                     "marked \"anchor\": true (the grid must try hardest to place it).")
    return v


def crossword_copy_text(data):
    """The clue text fed to the factual-grounding gate: title, subtitle, and
    every answer/clue pair, so P4b can verify each clue against the research."""
    parts = ["TITLE: " + str(data.get("title", "")),
             "SUBTITLE: " + str(data.get("subtitle", ""))]
    for c in (data.get("candidates") or []):
        parts.append(f"{c.get('answer', '')}: {c.get('clue', '')}")
    return "\n".join(parts)


def crossword_engine_data(data, company, config):
    """Map the P4 crossword output (title/subtitle/candidates) onto the
    crossword engine's --data schema. The engine composes the rendered title
    from company_name and places the best min..max candidates using seed."""
    return {
        "company_name": company,
        # title is optional: the engine falls back to "THE {company} CROSSWORD"
        # when it is empty, so passing P4's title through is safe either way.
        "title": str(data.get("title", "")).strip(),
        "subtitle": str(data.get("subtitle", "")),
        "min_words": config.get("crossword_min_words", 15),
        "max_words": config.get("crossword_max_words", 20),
        "seed": config.get("crossword_seed", 42),
        "candidates": [
            {
                "answer": str(c.get("answer", "")).upper(),
                "clue": str(c.get("clue", "")),
                # Pass the optional anchor flag through so the engine places the
                # brief's central concept first. Omitted when falsy (backward
                # compatible: no anchors -> identical behaviour).
                **({"anchor": True} if c.get("anchor") else {}),
                # The open-loop marker rides along for the runner's own seed-retry
                # and record-keeping; the grid generator ignores unknown keys.
                **({"open_loop": True} if c.get("open_loop") else {}),
            }
            for c in (data.get("candidates") or [])
        ],
    }


def run_crossword_engine(engine_path, template_path, data_path, output_path=None, check=False):
    """Render or validate the crossword. crossword.py mirrors newspaper.py:
    --check validates (and exits nonzero on FAIL) without writing output;
    --output renders at 360 DPI (matching the newspaper output). Same exit-code
    contract as the newspaper engine."""
    cmd = [sys.executable, engine_path, "--template", template_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", "360"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if output_path and result.returncode == 0:
        _stamp_engine(output_path, engine_path)
    return result.returncode, result.stdout + result.stderr


# --- The Email gates and engine --------------------------------------------
# Parallel to the newspaper/crossword helpers; nothing here touches those paths.
# email.py renders procedural Gmail chrome (no --template); --check validates fit
# at print size and exits nonzero on FAIL, same contract as the other engines.

def email_copy_text(data):
    """The text fed to the factual-grounding gate: subject, every body block,
    and the sign-off, so P4b can verify each claim against the research."""
    parts = ["SUBJECT: " + str(data.get("subject", ""))]
    for b in (data.get("body") or []):
        if b.get("type") == "signature":
            lines = b.get("lines") or [x for x in (b.get("name"), b.get("detail")) if x]
            parts.append("SIGN-OFF: " + " / ".join(lines))
        else:
            parts.append(str(b.get("text", "")))
    # A custom recipient-specific postscript is rendered on the piece and must be
    # gated like the body (the house P.S. is engine-authored and exempt).
    if str(data.get("postscript", "")).strip():
        parts.append("POSTSCRIPT: " + str(data["postscript"]))
    return "\n".join(parts)


def email_violations(data):
    """Structural + house-style gate on the P4 email output: a subject, a body of
    p/bullet/signature blocks signed off, no em dashes, no exclamation marks, and
    a rough word budget before the engine's exact fit --check. Authored copy only;
    client copy is rendered verbatim. Mirrors newspaper/crossword_violations."""
    v = []
    subject = str(data.get("subject", "")).strip()
    if not subject:
        v.append("the email has no subject.")
    elif len(subject) > 90:
        v.append(f"subject is {len(subject)} characters; keep it under 90 so it does "
                 f"not wrap awkwardly in the header.")
    body = data.get("body") or []
    if not body:
        v.append("the email body is empty.")
    paras = [b for b in body if b.get("type") in (None, "p", "bullet", "li")]
    if len(paras) < 2:
        v.append("the body needs at least two paragraphs of real copy.")
    if not any(b.get("type") == "signature" for b in body):
        v.append("the email has no signature block (the sender must sign off).")
    text = email_copy_text(data)
    if "—" in text or "--" in text or " - " in text:
        v.append("remove em dashes and spaced hyphens (house rule: no em dashes).")
    if "!" in text:
        v.append("remove exclamation marks (house rule: no exclamation marks in "
                 "professional copy).")
    words = sum(len(str(b.get("text", "")).split()) for b in body)
    if words > 230:
        v.append(f"the email is ~{words} words; trim to about 200 so it fills the A2 "
                 f"without overflowing.")
    return v


def _engine_file_sha1(engine_path):
    with open(engine_path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def _stamp_qc(folder, qc_name, image_path):
    """Record WHICH render a QC verdict actually looked at (qc_stamp.json,
    keyed by QC filename). Ship-check prefers this hash over mtimes: mtime
    comparison can be fooled by copy2-preserved times or a `qc --image` run
    against a different file than the delivered render."""
    try:
        path = os.path.join(folder, "qc_stamp.json")
        stamps = {}
        if os.path.exists(path):
            try:
                s = json.loads(read_file(path))
                if isinstance(s, dict):
                    stamps = s
            except (ValueError, OSError):
                pass
        stamps[qc_name] = {"image_sha1": _engine_file_sha1(image_path)}
        write_file(path, json.dumps(stamps, indent=2))
    except OSError:
        pass  # stamping is best-effort; the mtime fallback still applies


def _stamp_engine(output_path, engine_path):
    """Record which engine build produced a render (engine_stamp.json beside it).
    ship-check compares this against the current engine file and warns when a
    render predates an engine change — the lesson of the MongoDB dateline, where a
    fixed engine bug shipped inside a stale render. Keyed by output filename:
    the piece and its card render into the same folder, and a single flat stamp
    let the card render clobber the artefact's."""
    try:
        folder = os.path.dirname(os.path.abspath(output_path))
        path = os.path.join(folder, "engine_stamp.json")
        stamps = {}
        if os.path.exists(path):
            try:
                existing = json.loads(read_file(path))
                if isinstance(existing, dict):
                    # Legacy flat stamp {"engine":..,"sha1":..}: no output name
                    # was recorded, so it can only be kept as-is or replaced.
                    stamps = {} if "engine" in existing else existing
            except (ValueError, OSError):
                pass
        stamps[os.path.basename(output_path)] = {
            "engine": os.path.relpath(engine_path, REPO_ROOT),
            "sha1": _engine_file_sha1(engine_path),
        }
        write_file(path, json.dumps(stamps, indent=2))
    except OSError:
        pass  # stamping is best-effort; never fail a render over it


def email_engine_data(data, args, sender, config):
    """Assemble the engine's --data from the email copy plus the sender identity
    (config) and recipient (args). The sender signs the piece, so name/email/
    avatar always come from config, never from the model."""
    sender_name = sender.get("sender_name") or sender.get("company", "Sentrada")
    body = []
    for b in (data.get("body") or []):
        t = b.get("type", "p")
        if t == "signature":
            if data.get("copy_source", "authored") == "client":
                # Client-supplied copy is rendered verbatim, sign-off included.
                lines = b.get("lines") or [x for x in (b.get("name"), b.get("detail")) if x]
            else:
                # House sign-off: the sender's first name alone, no "Best regards"
                # (normalised here so the rule holds whatever the model wrote).
                lines = [sender_name.split()[0] if sender_name.strip() else "Joe"]
            body.append({"type": "signature", "lines": lines})
        elif t in ("bullet", "li"):
            body.append({"type": "bullet", "text": str(b.get("text", ""))})
        else:
            body.append({"type": "p", "text": str(b.get("text", ""))})
    return {
        "copy_source": data.get("copy_source", "authored"),
        "account": {"unread_count": config.get("email_unread_count", 1284)},
        "sender": {
            "name": sender_name,
            "email": sender.get("sender_email", ""),
            "avatar_initial": sender_name.strip()[:1].upper() if sender_name else "S",
            "avatar_color": config.get("email_avatar_color", "#C05933"),
        },
        "recipient": {"name": args.name.split()[0] if args.name else "me"},
        "subject": str(data.get("subject", "")),
        "timestamp": data.get("timestamp", config.get("email_timestamp", "09:14")),
        "label": "Inbox",
        "body": body,
        # The ever-present P.S. is a house device of this format: the single
        # acknowledgement that the email has been printed at A2 and hand
        # delivered rather than left in an inbox. A tailored postscript in the
        # copy overrides it; otherwise the house line is always present.
        "postscript": data.get("postscript") or config.get(
            "email_postscript",
            "Yes, I printed an email at A2 and had it delivered to your desk."),
    }


def load_client_email(path):
    """Load a client-supplied email. A .json file is used as the copy (subject +
    body blocks). A plain-text file is split on blank lines into paragraphs, with
    an optional 'Subject:' first line. Either way copy_source is 'client'."""
    raw = read_file(path)
    if path.lower().endswith(".json"):
        d = json.loads(raw)
        d["copy_source"] = "client"
        return d
    lines = raw.strip().split("\n")
    subject = ""
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        lines = lines[1:]
    paras = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
    return {"copy_source": "client", "subject": subject,
            "body": [{"type": "p", "text": p} for p in paras]}


def run_email_engine(engine_path, data_path, output_path=None, check=False):
    """Render or validate The Email. email.py has no --template (procedural
    chrome). Same exit-code contract as the other engines: --check validates and
    exits nonzero on FAIL; --output renders at 360 DPI."""
    cmd = [sys.executable, engine_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", "360"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if output_path and result.returncode == 0:
        _stamp_engine(output_path, engine_path)
    return result.returncode, result.stdout + result.stderr


# --- Factual grounding gate (text, not vision) -----------------------------
# Catches fabricated/contradicted claims in the copy before a render exists.
# More reliable than the post-render vision review for verifying text claims.

def newspaper_copy_text(data):
    parts = [
        # The printed dateline anchors violation 6's elapsed-time arithmetic;
        # without it the gate assumes today's date and a true "nine months in
        # post" on a June piece gets flagged as ten in July (found by the
        # harness's first baseline run). Same blind-spot class as the edition
        # line below.
        "DATE: " + data.get("date", ""),
        # The edition line's city list reads as the company's real locations, so
        # the grounding gate must see it (a fabricated city once shipped inside
        # the "fictional furniture" blind spot). The masthead stays out: it is
        # furniture by design and P4b is told never to flag it.
        "EDITION LINE: " + data.get("edition_line", ""),
        "HEADLINE: " + data.get("headline", ""),
        "STAT: " + data.get("stat_number", "") + " " + data.get("stat_descriptor", "")
        # The printed source attribution is a factual claim about provenance;
        # it must be visible to the gate (same blind-spot class as the edition line).
        + ((" | " + data["stat_source"]) if data.get("stat_source") else ""),
        "LEAD ARTICLE: " + data.get("lead_article", ""),
        "PULL QUOTE: " + data.get("pull_quote_text", "")
        + " -- " + data.get("pull_quote_attribution", ""),
    ]
    for n in (1, 2, 3):
        parts.append(f"SIDEBAR {n}: {data.get(f'sidebar_{n}_headline', '')} | "
                     f"{data.get(f'sidebar_{n}_body', '')}")
    return "\n\n".join(parts)


def claymation_copy_text(clay):
    parts = ["SCENE: " + str(clay.get("scene_description", "")),
             "CAPTION: " + str(clay.get("caption", ""))]
    for label in ("visual_details", "in_scene_text"):
        val = clay.get(label, "")
        if isinstance(val, list):
            val = "; ".join(str(x) for x in val)
        parts.append(label.upper() + ": " + str(val))
    return "\n\n".join(parts)


def grounding_check(config, research, copy_text, sender_facts=""):
    """Return (grounded: bool, issues: list of {claim, issue}).

    sender_facts (optional) are facts the sender may legitimately assert about
    ITSELF: its proof points, named customers, results, and what it sells. The
    Email is written from the sender and cites its own track record, so a claim
    those facts support counts as supported. Left empty for newspaper/crossword,
    whose grounding stays strictly research-only."""
    prompt = fill(load_template("prompt4b_grounding.md"),
                  {"research": research, "copy_text": copy_text,
                   "sender_facts": sender_facts.strip()
                   or "(none provided - verify every claim against the research only)"})
    _, data = cli_json(prompt, model_for(config, "p4b"))
    issues = data.get("unsupported") or []
    if not isinstance(data.get("grounded"), bool):
        # Wrong-schema reply (empty block, copy JSON echoed back): fail CLOSED.
        # Ungated copy must never proceed because the gate mumbled; the attempt
        # loop retries and then halts.
        return False, issues or [{"claim": "(gate reply unusable)",
                                  "issue": "no boolean 'grounded' verdict in the "
                                           "gate's JSON; treating as not grounded"}]
    grounded = data["grounded"] and not issues
    return grounded, issues


# --- Checkpoint -------------------------------------------------------------

def print_brief_checkpoint(brief):
    print("\n" + "=" * 70)
    print("BRIEF CHECKPOINT — review before anything else runs")
    print("=" * 70)
    print(f"\nSnapshot:\n{brief.get('snapshot', '')}\n")
    fields = [
        ("Core problem", "core_problem"), ("Key metric", "key_metric"),
        ("Environment", "environment"), ("Moment", "moment"),
        ("Operational details", "operational_details"),
        ("Problem label", "problem_label"),
        ("Companion card hook", "companion_card_hook"),
    ]
    for label, key in fields:
        print(f"{label}: {brief.get(key, '')}")
    if brief.get("reserve_detail"):
        print(f"Reserve detail (held back for Touch 3): {brief['reserve_detail']}")
    if brief.get("absurdity"):
        print(f"Absurdity: {brief['absurdity']}")
    if brief.get("comedy_potential"):
        print(f"Comedy potential: {brief['comedy_potential']}")
    print("=" * 70)


# --- generate ---------------------------------------------------------------

def _build_base_values(args, sender, research, fmt):
    probs = sender.get("problems", [])
    return {
        "recipient_name": args.name, "recipient_title": args.title,
        "recipient_company": args.company,
        "problem_1": probs[0] if len(probs) > 0 else "",
        "problem_2": probs[1] if len(probs) > 1 else "",
        "problem_3": probs[2] if len(probs) > 2 else "",
        "research": research,
        "sender_company": sender.get("company", ""),
        "sender_what": sender.get("what_they_sell", ""),
        "sender_proof": sender.get("proof_points", ""),
        "booking_link": sender.get("booking_link", ""),
        "sender_name": sender.get("sender_name", ""),
        # Crossword open-loop tier judgement (P2). Optional; empty means P2
        # judges deliverability from what_they_sell and prefers Tier B.
        "sender_capabilities": sender.get("measurement_capabilities", ""),
        "format": {"newspaper": "Newspaper Front Page",
                   "crossword": "Crossword",
                   "email": "The Email"}.get(fmt, "Claymation Scene"),
    }


def _build_meta(args, fmt, brief, sender):
    meta = {
        "recipient_name": args.name, "recipient_title": args.title,
        "recipient_company": args.company, "format": fmt,
        "problem_label": brief.get("problem_label", ""),
        "companion_card_hook": brief.get("companion_card_hook", ""),
        "sender": sender,
    }
    # Sourcing handoff: when the manifest entry carries source_signals (see
    # sourcing/sourcing.py export), they ride into the piece record so the
    # outcome ledger can compute response rate per signal source later.
    raw = getattr(args, "source_signals", "") or ""
    if raw:
        try:
            meta["source_signals"] = json.loads(raw)
        except json.JSONDecodeError:
            print("[warn] --source-signals is not valid JSON; ignored")
    return meta


def _run_brief(config, folder, base_values, feedback_text=""):
    feedback_block = ""
    if feedback_text:
        feedback_block = ("FOUNDER FEEDBACK ON YOUR PREVIOUS DRAFT:\n" + feedback_text
                          + "\n\nRegenerate the brief incorporating this feedback.")
    prompt = fill(load_template("prompt2_brief.md"),
                  dict(base_values, feedback_block=feedback_block))
    print(f"\n[prompt 2] generating the brief ({model_for(config, 'p2')})...")
    brief_text, brief = cli_json(prompt, model_for(config, "p2"))
    write_file(os.path.join(folder, "brief.md"), brief_text)
    write_file(os.path.join(folder, "brief.json"), json.dumps(brief, indent=2))
    return brief


def _continue_after_brief(args, config, folder, base_values, brief, sender):
    fmt = fmt_of(args)
    meta = _build_meta(args, fmt, brief, sender)
    if fmt == "newspaper":
        _generate_newspaper(args, config, folder, base_values, brief, meta)
    elif fmt == "email":
        _generate_email(args, config, folder, base_values, brief, meta)
    elif fmt == "crossword":
        _generate_crossword(args, config, folder, base_values, brief, meta)
    else:
        die(f"unsupported format '{fmt}'. Accepted: newspaper, email, crossword.")


def fmt_of(args):
    return args.format.lower()


def research_gate(config, research, name, title, company, fmt):
    """Prompt 1b: gate pasted research for completeness before anything builds on
    it. Structural checks run in code (free); the model judges completeness and
    format sufficiency. Returns (verdict, gaps, fact_count_estimate). The gate
    never blocks by itself: callers persist the result and surface it at the human
    approval checkpoint, where thin research is a conscious decision, not a
    surprise three steps later."""
    structural = []
    if "DELIVERY_STATUS" not in research:
        structural.append("no structured DELIVERY block (DELIVERY_STATUS/ADDRESS/"
                          "NOTES) — birch-csv cannot ship this piece")
    if not re.search(r"^Recipient\s*:", research, re.MULTILINE):
        structural.append("no Piece setup header (Recipient/Title/Company/Format)")
    words = len(research.split())
    if words < 300:
        structural.append(f"research is only ~{words} words — too thin to build on")

    values = {"recipient_name": name, "recipient_title": title,
              "recipient_company": company, "format": fmt, "research": research}
    print(f"[prompt 1b] gating the research ({model_for(config, 'p1b')})...")
    reply, data = cli_json(fill(load_template("prompt1b_research_gate.md"), values),
                           model_for(config, "p1b"))
    verdict = str(data.get("verdict", "")).upper().replace(" ", "_")
    gaps = structural + [str(g) for g in (data.get("gaps") or [])]
    if not verdict:
        # No verdict in the gate's JSON: fail closed, never assume READY.
        gaps.append("research gate returned no verdict (wrong-schema reply) — "
                    "treated as NOT_READY; re-run or review manually")
        verdict = "NOT_READY"
    elif structural and verdict == "READY":
        verdict = "READY_WITH_GAPS"
    return verdict, gaps, data.get("fact_count_estimate")


def cmd_generate(args):
    _usage_reset()
    config = load_config(args.config)
    sender = config["sender"]
    fmt = args.format.lower()
    if fmt not in ("newspaper", "email", "crossword"):
        die("format must be 'newspaper', 'email', or 'crossword'.")

    slug = f"{slugify(args.name)}-{slugify(args.company)}"
    folder = os.path.join(PIECES_DIR, slug)

    # --- resume: skip Prompt 2 and the checkpoint, use the approved brief.json ---
    if args.resume:
        bp = os.path.join(folder, "brief.json")
        if not os.path.exists(bp):
            die(f"--resume needs an approved brief at {bp}. Run --brief-only first.")
        rp = os.path.join(folder, "research.md")
        # The brief is a snapshot of the research it was written from. If the
        # research has been edited since, resuming feeds P4 a brief that may
        # carry facts the research no longer supports, and the grounding gate
        # (which checks against the CURRENT research) then fails every attempt.
        # A live piece looped through five failed builds exactly this way.
        if os.path.exists(rp) and os.path.getmtime(rp) > os.path.getmtime(bp):
            die("research.md has been edited since brief.json was written. The "
                "brief may carry facts the research no longer supports, which "
                "makes the grounding gate unwinnable. Re-run --brief-only to "
                "regenerate the brief from the current research, then --resume.")
        research = read_file(rp)
        brief = json.loads(read_file(bp))
        base_values = _build_base_values(args, sender, research, fmt)
        print(f"[resume] {os.path.relpath(folder, REPO_ROOT)} (brief already approved)")
        _continue_after_brief(args, config, folder, base_values, brief, sender)
        return

    if not args.research:
        die("--research is required (path to your pasted research file).")
    research = read_file(args.research)
    os.makedirs(folder, exist_ok=True)
    write_file(os.path.join(folder, "research.md"), research)
    print(f"[folder] {os.path.relpath(folder, REPO_ROOT)}")

    # Prompt 1b: gate the research at ingestion. Persisted to gate.json and
    # surfaced at the approval checkpoint; skipped on feedback reruns (the
    # research was already gated the first time round).
    if not args.feedback:
        verdict, gaps, facts = research_gate(config, research, args.name,
                                             args.title, args.company, fmt)
        write_file(os.path.join(folder, "gate.json"), json.dumps(
            {"verdict": verdict, "gaps": gaps, "fact_count_estimate": facts},
            indent=2))
        if gaps:
            print(f"[gate] {verdict}:")
            for g in gaps:
                print(f"  - {g}")
        else:
            print(f"[gate] {verdict}")

    base_values = _build_base_values(args, sender, research, fmt)

    # --- brief-only: run the brief once, print the checkpoint, then stop ---
    if args.brief_only:
        brief = _run_brief(config, folder, base_values, args.feedback)
        if brief.get("fit") == "poor":
            die("the brief declares a POOR fit: "
                + brief.get("fit_reason", "no problem reached medium/high confidence"))
        print_brief_checkpoint(brief)
        rel = os.path.relpath(__file__, REPO_ROOT)
        print("\n[brief-only] Brief saved. When you approve it, continue with:")
        print(f'  python {rel} generate --name "{args.name}" --title "{args.title}" '
              f'--company "{args.company}" --format {fmt} --resume')
        print(f'  (or revise first: ... --format {fmt} --brief-only --feedback "your notes")')
        _print_usage("brief")
        return

    # --- default interactive run with the approval checkpoint ---
    feedback_text = ""
    while True:
        brief = _run_brief(config, folder, base_values, feedback_text)
        if brief.get("fit") == "poor":
            die("the brief declares a POOR fit: "
                + brief.get("fit_reason", "no problem reached medium/high confidence")
                + "\nNo piece generated. Review the target or add sender context.")
        print_brief_checkpoint(brief)
        ans = input("\nType 'approve' to proceed, or paste feedback to rerun the "
                    "brief: ").strip()
        if ans.lower() == "approve":
            break
        if not ans:
            print("(no input; nothing proceeds without approval)")
            continue
        feedback_text = ans

    _continue_after_brief(args, config, folder, base_values, brief, sender)


def _generate_newspaper(args, config, folder, base_values, brief, meta):
    template = load_template("prompt4_copy_newspaper.md")
    brief_values = dict(base_values, **{
        "core_problem": brief.get("core_problem", ""),
        "key_metric": brief.get("key_metric", ""),
        "environment": brief.get("environment", ""),
        "moment": brief.get("moment", ""),
        "problem_label": brief.get("problem_label", ""),
        "operational_details": brief.get("operational_details", ""),
    })

    model = model_for(config, "p4")
    research = base_values["research"]
    attempt, feedback_block = 0, ""
    while True:
        attempt += 1
        prompt = fill(template, dict(brief_values, feedback_block=feedback_block))
        print(f"\n[prompt 4] writing newspaper copy ({model}, attempt {attempt})...")
        reply, data = cli_json(prompt, model)
        copy_part, factcheck = split_factcheck(reply)

        problems = list(newspaper_violations(data))
        print(f"[prompt 4b] factual grounding check ({model_for(config, 'p4b')})...")
        grounded, issues = grounding_check(config, research, newspaper_copy_text(data))
        problems += [f"unsupported claim \"{i.get('claim', '')}\": {i.get('issue', '')}"
                     for i in issues]

        if not problems:
            break
        if attempt >= 3:
            die("Prompt 4 newspaper failed the gates after 3 attempts (word count "
                "and/or factual grounding):\n  - " + "\n  - ".join(problems))
        print("[gate] violations found, rerunning Prompt 4 once:")
        for x in problems:
            print(f"  - {x}")
        feedback_block = ("YOUR PREVIOUS DRAFT FAILED THESE HARD CHECKS. Fix every "
                          "one and keep everything else. Any 'unsupported claim' below "
                          "is a fact the research does not support: remove it or "
                          "replace it with a fact the research does support.\n- "
                          + "\n- ".join(problems))

    write_file(os.path.join(folder, "copy.md"), copy_part)
    write_file(os.path.join(folder, "factcheck.md"), factcheck or "(none returned)")

    engine_data = engine_data_only(data)
    data_path = os.path.join(folder, "data.json")
    write_file(data_path, json.dumps(engine_data, indent=2))

    engine_path = os.path.join(REPO_ROOT, config.get("engine", "newspaper/newspaper.py"))
    template_path = os.path.join(
        REPO_ROOT, config.get("newspaper_template",
                              "newspaper/Newspaper Template - Upscaled - 25mb.jpg"))

    print("\n[engine] running --check on data.json...")
    rc, out = run_engine(engine_path, template_path, data_path, check=True)
    print(out.strip())
    if rc != 0:
        die("the layout engine's --check FAILED. Nothing rendered. Fix the copy "
            "or template and re-run. See the engine output above.")

    output_path = os.path.join(folder, f"{slugify(args.name)}-{slugify(args.company)}.png")
    print("\n[engine] rendering the print-ready newspaper...")
    rc, out = run_engine(engine_path, template_path, data_path, output_path=output_path)
    print(out.strip())
    if rc != 0 or not os.path.exists(output_path):
        die("the render failed (see engine output above). A *.FAILED file may have "
            "been written for inspection.")

    meta["piece_reference"] = (
        f'the front page of "{engine_data.get("masthead_name", "")}", '
        f'headline "{engine_data.get("headline", "")}"')
    write_file(os.path.join(folder, "meta.json"), json.dumps(meta, indent=2))

    print(f"\n[rendered] {os.path.relpath(output_path, REPO_ROOT)}")
    print("[fact check] runner/.../factcheck.md holds the copy claims for your "
          "sign-off before print.")
    # Newspaper has a real rendered image, so the QC + follow-up chain runs now.
    delivery_date = getattr(args, "delivery_date", "") or "to be confirmed on delivery"
    run_chain_after_render(config, folder, output_path, delivery_date)


# --- Step 8b: companion card (card/card.py) ---------------------------------
# After Prompt 7 writes the card copy, render the A6 companion card that ships in
# the box beside the artefact. Same house contract as the email engine. The card
# text is parsed out of the P7 output; the contact block is always the sender
# (from config), never the recipient. Output lands at <slug>-card.png next to the
# artefact PNG so deliverables staging picks it up.

def _card_copy_from_p7(reply, first_name, sender):
    """Pull the companion-card body paragraphs from the Prompt 7 output. Returns a
    list of paragraph strings, or None when the card is suppressed (WOULD BIN) or
    the sender provides their own copy. The salutation comes from the recipient
    name and the sign-off lines are dropped (the rendered contact block is the
    sender)."""
    if "sender will provide custom companion card" in reply.lower():
        return None
    m = re.search(r"COMPANION CARD[^\n]*?\*\*(.*?)(?:\n[ \t]*\*\*[A-Z]|\Z)", reply, re.DOTALL)
    block = (m.group(1) if m else reply).strip()
    if not block or block.startswith("["):            # unfilled stub
        return None
    paras = [p.strip() for p in re.split(r"\n[ \t]*\n", block) if p.strip()]
    paras = [p for p in paras if p.strip("-*_ ")]      # drop horizontal rules
    if paras and paras[0].rstrip(",").strip().lower() == (first_name or "").lower():
        paras = paras[1:]                              # drop the salutation line
    sign = {s.lower() for s in (sender.get("sender_name", ""), sender.get("company", "")) if s}
    while paras:                                       # drop trailing sign-off
        last = [l.strip().lower() for l in paras[-1].split("\n") if l.strip()]
        if last and all(l in sign for l in last):
            paras.pop()
        else:
            break
    return paras or None


def card_engine_data(body_paras, first_name, sender):
    """Assemble card.py's --data: salutation from the recipient first name, body
    from the Prompt 7 paragraphs, contact block always the sender (config)."""
    return {
        "salutation": first_name,
        "body": body_paras,
        "contact": {
            "name": sender.get("sender_name", "Joe Chapman"),
            "company": sender.get("company", "Sentrada"),
            "email": sender.get("sender_email", ""),
            "phone": sender.get("card_phone", ""),
        },
    }


def run_card_engine(config, engine_path, data_path, output_path=None, check=False):
    """Render or validate the companion card (procedural, no --template). Same
    exit-code contract as the others: --check validates and exits nonzero on
    overflow; --output renders at 360 DPI. Runs under the same interpreter as the
    other Pango engines (sys.executable); override with config 'card_python'.
    Bleed/crop stay off to match what newspaper/crossword/email send to Birch."""
    interp = config.get("card_python") or sys.executable
    cmd = [interp, engine_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", "360"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if output_path and result.returncode == 0:
        _stamp_engine(output_path, engine_path)
    return result.returncode, result.stdout + result.stderr


def _generate_card(config, folder, reply):
    """Step 8b: render the A6 companion card from the Prompt 7 card copy and stage
    it as <slug>-card.png beside the artefact. Skipped when the sender provides
    custom copy or the card was suppressed. Overflow is non-fatal: the engine
    refuses to squeeze, so the card is withheld and flagged for a 150-word trim
    while the artefact and follow-up stand."""
    engine = os.path.join(REPO_ROOT, config.get("card_engine", "card/card.py"))
    if not os.path.exists(engine):
        print(f"[card] step 8b skipped: engine not found at "
              f"{os.path.relpath(engine, REPO_ROOT)}.")
        return
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    sender = config.get("sender", {})
    first = (meta.get("recipient_name", "").split() or [""])[0]
    body = _card_copy_from_p7(reply, first, sender)
    if not body:
        print("[card] step 8b skipped: no auto card (sender-provided or suppressed).")
        return None
    slug = os.path.basename(os.path.normpath(folder))
    data_path = os.path.join(folder, f"{slug}-card.json")
    card_png = os.path.join(folder, f"{slug}-card.png")
    write_file(data_path, json.dumps(card_engine_data(body, first, sender), indent=2))
    print(f"\n[card] step 8b: rendering the A6 companion card ({len(body)} paragraphs)...")
    rc, out = run_card_engine(config, engine, data_path, check=True)
    if rc != 0:
        print("[card] WITHHELD: copy overflows A6 even at the compact tier. Trim the "
              "card under 150 words and re-run. Engine output:\n" + out.strip())
        return None
    rc, out = run_card_engine(config, engine, data_path, card_png)
    if rc != 0:
        print("[card] render failed:\n" + out.strip())
        return None
    print(f"[card] rendered: {os.path.relpath(card_png, REPO_ROOT)}")
    return card_png


def run_chain_after_render(config, folder, image_path, delivery_date):
    """After a newspaper renders: P6 craft review -> (stop on FAIL) -> P6B
    recipient sim -> (stop on WOULD BIN) -> P7 follow-up -> consolidated package
    for the founder's single print/no-print decision."""
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    brief = json.loads(read_file(os.path.join(folder, "brief.json")))
    research = read_file(os.path.join(folder, "research.md"))

    review, v6 = _p6(config, folder, image_path, meta, brief, research)
    if v6 == "FAIL":
        print("\n" + "=" * 70)
        print("CHAIN STOPPED — Prompt 6 returned FAIL. 6B and follow-up not run.")
        print("=" * 70)
        print(f"Rendered (withhold from print): {os.path.relpath(image_path, REPO_ROOT)}")
        print("\nReason and regeneration instructions (full review: qc_review.md):\n")
        print(review)
        _print_usage("this piece")
        return

    sixb, v6b = _p6b(config, folder, image_path, meta, research)
    if v6b == "WOULD BIN":
        print("\n" + "=" * 70)
        print("CHAIN STOPPED — Prompt 6B returned WOULD BIN. Follow-up suppressed "
              "(P7 Step 0 verdict gate).")
        print("=" * 70)
        print(f"Rendered: {os.path.relpath(image_path, REPO_ROOT)}")
        print("The simulation predicts this recipient will not respond regardless of "
              "conversion copy quality. Card and follow-up suppressed. Recommend "
              "reviewing the target or the artefact angle before sending.")
        print("\n6B simulation: qc_recipient.md")
        _print_usage("this piece")
        return

    reply = _p7(config, folder, delivery_date)
    card_png = _generate_card(config, folder, reply)   # step 8b: companion card
    if card_png:
        # Step 8c: the package pass. The piece-stage 6B above judged the artefact
        # alone (the verdict P7 built the card from); now the card exists, judge
        # the full send. Custom-card senders run this later via `package-qc`.
        _, vpkg = _p6b(config, folder, image_path, meta, research, package=True)
        print(f"[6B package] verdict: {vpkg or '(see qc_package.md)'}")
    _print_package(image_path, review, v6, sixb, v6b, reply)


def _print_package(image_path, review, v6, sixb, v6b, followup_text):
    print("\n" + "=" * 70)
    print("PIECE COMPLETE — final checkpoint: review and decide whether to print")
    print("=" * 70)
    print(f"\nRendered piece: {os.path.relpath(image_path, REPO_ROOT)}")

    print(f"\n--- Prompt 6 (craft): {v6 or 'see qc_review.md'} ---")
    flags = [ln.strip() for ln in review.splitlines()
             if ("flag" in ln.lower() or "nuance" in ln.lower()) and ln.strip()]
    if v6 == "BORDERLINE":
        print("BORDERLINE — proceeding, but review the craft notes before print.")
    print("Flags: " + ("; ".join(flags) if flags else "none") + "  (full review: qc_review.md)")

    print(f"\n--- Prompt 6B (recipient): {v6b or 'see qc_recipient.md'} ---")
    print(_extract_6b_leverage(sixb))
    print("(full simulation: qc_recipient.md)")

    print("\n--- Companion card + 3-touch follow-up (followup.md) ---\n")
    print(followup_text)
    _print_usage("this piece")


def _extract_6b_leverage(sixb):
    low = sixb.lower()
    i = low.find("highest-leverage")
    if i == -1:
        return "Highest-leverage change: (see qc_recipient.md)"
    after = sixb[i:]
    nl = after.find("\n")                       # drop the header line itself
    body = (after[nl + 1:] if nl != -1 else after).split("\n\n")[0].strip()
    body = re.sub(r"\*+", "", body)             # strip markdown bold
    return "Highest-leverage change: " + (body[:600] if body else "(see qc_recipient.md)")


def _crossword_placement(engine_path, engine_data, answer):
    """Where did `answer` land in the rendered grid? Recomputes the engine's own
    deterministic grid (same candidates, seed and attempts via read-only import
    of the engine modules; no engine code changes) and returns
    {number, direction} or None. Numbering matches the render exactly because
    the engine derives its grid from the same data.json."""
    eng_dir = os.path.dirname(os.path.abspath(engine_path))
    if eng_dir not in sys.path:
        sys.path.insert(0, eng_dir)
    import crossword as _cw          # for CONFIG["grid_attempts"], mirroring build()
    import grid_generator as _gg
    grid = _gg.generate_grid(
        engine_data["candidates"],
        min_words=engine_data.get("min_words", _cw.CONFIG["min_words"]),
        max_words=engine_data.get("max_words", _cw.CONFIG["max_words"]),
        seed=engine_data.get("seed"),
        attempts=_cw.CONFIG["grid_attempts"],
        anchors=engine_data.get("anchors"))
    for e in grid["placed"]:
        if e["answer"] == str(answer).upper():
            return {"number": e["number"], "direction": e["direction"]}
    return None


def _open_loop_anchor_fails(out, answer):
    """True when the engine --check output's ONLY [FAIL] lines are the
    designated-anchor placement failure for `answer` (the open-loop word).
    Any other failure (clue fit, solvability, the hero anchor) returns False
    so it takes the existing feedback/hard-fail path."""
    fails = [ln for ln in out.splitlines() if "[FAIL]" in ln]
    return bool(fails) and all(
        "anchor" in ln and f"'{str(answer).upper()}'" in ln for ln in fails)


def _generate_crossword(args, config, folder, base_values, brief, meta):
    """Crossword path. Mirrors _generate_newspaper: P4 (crossword branch) ->
    structural + factual-grounding gates -> render via the crossword engine ->
    the shared P6 -> P6B -> P7 chain. Nothing here touches the newspaper path."""
    template = load_template("prompt4_copy_crossword.md")
    ol_brief = brief.get("open_loop")
    ol_brief = ol_brief if isinstance(ol_brief, dict) else None
    brief_values = dict(base_values, **{
        "core_problem": brief.get("core_problem", ""),
        "key_metric": brief.get("key_metric", ""),
        "environment": brief.get("environment", ""),
        "moment": brief.get("moment", ""),
        "problem_label": brief.get("problem_label", ""),
        "operational_details": brief.get("operational_details", ""),
        "open_loop_brief": (json.dumps(ol_brief, ensure_ascii=False)
                            if ol_brief else "none"),
        "hero_fact": str(brief.get("hero_fact", "") or ""),
    })

    model = model_for(config, "p4")
    research = base_values["research"]
    data_path = os.path.join(folder, "data.json")
    engine_path = os.path.join(REPO_ROOT, config.get("crossword_engine", "crossword/crossword.py"))
    template_path = os.path.join(
        REPO_ROOT, config.get("crossword_template",
                              "crossword/Blank crossword template 25mb - upscaled.jpg"))
    if not os.path.exists(engine_path):
        die(f"crossword engine not found at {engine_path}. The engine ships separately; "
            f"place crossword/crossword.py in the repo, or set 'crossword_engine' in "
            f"config.json.")
    if not os.path.exists(template_path):
        die(f"crossword template not found at {template_path}. Place the upscaled "
            f"crossword template in crossword/ or set 'crossword_template' "
            f"in config.json.")

    # Up to three attempts: P4 copy -> structural + factual gates -> engine
    # --check (grid placement + clue fit). Unlike newspaper (where a --check FAIL
    # just halts), a layout FAIL here feeds back into P4 so it can shorten or cut
    # clues, because clue fit cannot be predicted from word counts alone.
    attempt, feedback_block, engine_data = 0, "", None
    while True:
        attempt += 1
        prompt = fill(template, dict(brief_values, feedback_block=feedback_block))
        print(f"\n[prompt 4] writing crossword copy ({model}, attempt {attempt})...")
        reply, data = cli_json(prompt, model)
        copy_part, factcheck = split_factcheck(reply)

        problems = list(crossword_violations(data))
        print(f"[prompt 4b] factual grounding check ({model_for(config, 'p4b')})...")
        grounded, issues = grounding_check(config, research, crossword_copy_text(data))
        problems += [f"unsupported clue \"{i.get('claim', '')}\": {i.get('issue', '')}"
                     for i in issues]

        # Only run the engine layout check once the copy is structurally sound
        # and factually grounded (no point validating layout on copy we'll redo).
        if not problems:
            engine_data = crossword_engine_data(data, args.company, config)
            ol_answer = next((c["answer"] for c in engine_data["candidates"]
                              if c.get("open_loop")), None)
            base_seed = engine_data["seed"]
            # The open-loop word must anchor the grid, but a placement miss is a
            # seed problem before it is a copy problem: retry seeds, then fall
            # back to running it as an ordinary candidate (spec: retry-then-
            # fallback, never hard-fail). The hero anchor keeps the existing
            # behaviour: its failure falls through to P4 feedback and, after 3
            # attempts, halts the piece.
            retries = max(1, int(config.get("crossword_open_loop_seed_retries", 5)))
            seeds = [base_seed + i for i in range(retries)] if ol_answer else [base_seed]
            open_loop_fallback = False
            rc, out = 1, ""
            for s in seeds:
                engine_data["seed"] = s
                write_file(data_path, json.dumps(engine_data, indent=2))
                print(f"[engine] running --check on data.json (grid placement + "
                      f"clue fit, seed {s})...")
                rc, out = run_crossword_engine(engine_path, template_path, data_path,
                                               check=True)
                if rc == 0 or not (ol_answer and _open_loop_anchor_fails(out, ol_answer)):
                    break
                print(f"[open-loop] seed {s}: anchor '{ol_answer}' would not place; "
                      "retrying with the next seed...")
            if rc != 0 and ol_answer and _open_loop_anchor_fails(out, ol_answer):
                # Fallback: strip the anchor flag from the open-loop candidate
                # ONLY. The word stays in the pool as an ordinary candidate; the
                # mechanic is dropped for this piece and P7 gets no record.
                print(f"[open-loop] FALLBACK: '{ol_answer}' would not place as an "
                      f"anchor across {len(seeds)} seed(s). Running it as an "
                      "ordinary candidate; no open-loop record will reach P7.")
                for c in engine_data["candidates"]:
                    if c.get("open_loop"):
                        c.pop("anchor", None)
                engine_data["seed"] = base_seed
                open_loop_fallback = True
                write_file(data_path, json.dumps(engine_data, indent=2))
                print("[engine] re-running --check without the open-loop anchor...")
                rc, out = run_crossword_engine(engine_path, template_path, data_path,
                                               check=True)
            print(out.strip())
            if rc == 0:
                break
            fails = [ln.strip().lstrip("[FAIL]").strip()
                     for ln in out.splitlines() if "[FAIL]" in ln] or ["layout check failed"]
            problems = ["the layout --check failed: " + f for f in fails]
            if any("clue" in f.lower() for f in fails):
                problems.append("Make the clues materially shorter (aim 4-8 words each, "
                                "never more than 10) and favour shorter answers. Keep "
                                "25-30 candidates; the grid uses the best 15-20.")
            if any("anchor" in f.lower() for f in fails):
                problems.append("An anchor answer could not be placed. KEEP the hero "
                                "designation (and the open-loop designation if the brief "
                                "supplied one), but choose shorter, more letter-friendly "
                                "answers for the designated candidates (common letters "
                                "E S T A R N I O intersect best).")

        if attempt >= 3:
            die("Prompt 4 crossword failed the gates after 3 attempts (candidate "
                "structure, factual grounding, and/or engine layout fit):\n  - "
                + "\n  - ".join(problems))
        print("[gate] issues found, rerunning Prompt 4:")
        for x in problems:
            print(f"  - {x}")
        feedback_block = ("YOUR PREVIOUS DRAFT FAILED THESE HARD CHECKS. Fix every one and "
                          "keep everything else. Any 'unsupported clue' is a fact the "
                          "research does not support: drop that candidate or replace it "
                          "with one the research supports.\n- " + "\n- ".join(problems))

    write_file(os.path.join(folder, "copy.md"), copy_part)
    write_file(os.path.join(folder, "factcheck.md"), factcheck or "(none returned)")

    output_path = os.path.join(folder, f"{slugify(args.name)}-{slugify(args.company)}.png")
    print("\n[engine] rendering the print-ready crossword "
          f"(placing {engine_data['min_words']}-{engine_data['max_words']} of "
          f"{len(engine_data['candidates'])} candidates)...")
    rc, out = run_crossword_engine(engine_path, template_path, data_path, output_path=output_path)
    print(out.strip())
    if rc != 0 or not os.path.exists(output_path):
        die("the crossword render failed (see engine output above).")

    meta["piece_reference"] = (
        f'the "{args.company}" crossword, subtitle "{engine_data.get("subtitle", "")}"')

    # Open-loop record for P7: look up the clue number the grid assigned to the
    # open-loop answer. Only written when the mechanic ran to completion; on
    # fallback the piece record logs why instead, and P7 sees no record.
    if ol_answer and not open_loop_fallback and ol_brief:
        placement = _crossword_placement(engine_path, engine_data, ol_answer)
        if placement:
            meta["open_loop"] = {
                "answer": str(ol_answer).upper(),
                "clue": next((c.get("clue", "") for c in engine_data["candidates"]
                              if c.get("open_loop")), ""),
                "clue_number": placement["number"],
                "direction": placement["direction"],
                "metric": str(ol_brief.get("metric", "")),
                "question": str(ol_brief.get("question", "")),
                "tier": str(ol_brief.get("tier", "")).upper(),
                "tier_A_number": str(ol_brief.get("tier_A_number", "")),
            }
            print(f"[open-loop] '{ol_answer}' placed at {placement['number']} "
                  f"{placement['direction'].title()} (tier {meta['open_loop']['tier']}); "
                  "recorded for P7.")
        else:
            meta["open_loop_fallback"] = ("open-loop answer passed --check as an anchor "
                                          "but was not found in the recomputed grid; no "
                                          "record passed to P7")
            print("[open-loop] WARNING: placement lookup did not find the answer; "
                  "no open-loop record passed to P7.")
    elif ol_answer and open_loop_fallback:
        meta["open_loop_fallback"] = (f"anchor '{ol_answer}' unplaceable across "
                                      f"{len(seeds)} seed(s); ran as ordinary candidate; "
                                      "no open-loop record passed to P7")
    write_file(os.path.join(folder, "meta.json"), json.dumps(meta, indent=2))

    print(f"\n[rendered] {os.path.relpath(output_path, REPO_ROOT)}")
    print("[fact check] runner/.../factcheck.md holds the clue claims for your "
          "sign-off before print.")
    # A real rendered image exists, so the shared QC + follow-up chain runs now,
    # exactly as it does for newspaper.
    delivery_date = getattr(args, "delivery_date", "") or "to be confirmed on delivery"
    run_chain_after_render(config, folder, output_path, delivery_date)


def _generate_email(args, config, folder, base_values, brief, meta):
    """The Email path. Two sources. Authored: P4 writes the cold email from the
    brief, then the structural/house-style gate, the factual-grounding gate and
    the engine fit --check run (up to three attempts, feeding failures back like
    crossword). Client-supplied (--email-copy): rendered verbatim, fit-checked
    only, no house-rule lint and no grounding. Both feed the same engine and the
    shared P6 -> P6B -> P7 chain. Nothing here touches the other format paths."""
    research = base_values["research"]
    sender = config["sender"]
    data_path = os.path.join(folder, "data.json")
    engine_path = os.path.join(REPO_ROOT, config.get("email_engine", "email/email.py"))
    if not os.path.exists(engine_path):
        die(f"email engine not found at {engine_path}. Place email/email.py in the "
            f"repo or set 'email_engine' in config.json.")

    client_copy = getattr(args, "email_copy", "") or ""
    if client_copy:
        if not os.path.exists(client_copy):
            die(f"--email-copy file not found: {client_copy}")
        data = load_client_email(client_copy)
        if not str(data.get("subject", "")).strip() or not data.get("body"):
            die("the client email needs a subject and a non-empty body. For a plain "
                "text file, put 'Subject: ...' on the first line and separate "
                "paragraphs with blank lines, or supply a JSON file.")
        engine_data = email_engine_data(data, args, sender, config)
        write_file(data_path, json.dumps(engine_data, indent=2))
        write_file(os.path.join(folder, "copy.md"),
                   "Client-supplied email, rendered verbatim.")
        write_file(os.path.join(folder, "factcheck.md"),
                   "Client-supplied copy: not fact-checked by the runner. The client "
                   "owns its claims.")
        print("[engine] running --check on data.json (fit at print size)...")
        rc, out = run_email_engine(engine_path, data_path, check=True)
        print(out.strip())
        if rc != 0:
            die("the client email overflows the A2 at print size. Ask the client to "
                "shorten it; the engine never squeezes copy.")
    else:
        template = load_template("prompt4_copy_email.md")
        brief_values = dict(base_values, **{
            "core_problem": brief.get("core_problem", ""),
            "key_metric": brief.get("key_metric", ""),
            "environment": brief.get("environment", ""),
            "moment": brief.get("moment", ""),
            "problem_label": brief.get("problem_label", ""),
            "operational_details": brief.get("operational_details", ""),
        })
        model = model_for(config, "p4")
        # The Email is written from the sender and cites the sender's own track
        # record, so give the grounding gate the sender's stated facts alongside
        # the recipient research; otherwise true proof points read as fabricated.
        sender_facts = "\n".join(
            f"- {x}" for x in (sender.get("company", ""), sender.get("what_they_sell", ""),
                               sender.get("proof_points", "")) if str(x).strip())
        attempt, feedback_block = 0, ""
        while True:
            attempt += 1
            prompt = fill(template, dict(brief_values, feedback_block=feedback_block))
            print(f"\n[prompt 4] writing the cold email ({model}, attempt {attempt})...")
            reply, data = cli_json(prompt, model)
            copy_part, factcheck = split_factcheck(reply)

            problems = list(email_violations(data))
            print(f"[prompt 4b] factual grounding check ({model_for(config, 'p4b')})...")
            grounded, issues = grounding_check(config, research, email_copy_text(data),
                                               sender_facts=sender_facts)
            problems += [f"unsupported claim \"{i.get('claim', '')}\": {i.get('issue', '')}"
                         for i in issues]

            if not problems:
                engine_data = email_engine_data(data, args, sender, config)
                write_file(data_path, json.dumps(engine_data, indent=2))
                print("[engine] running --check on data.json (fit at print size)...")
                rc, out = run_email_engine(engine_path, data_path, check=True)
                print(out.strip())
                if rc == 0:
                    break
                fails = [ln.strip() for ln in out.splitlines() if "[FAIL]" in ln] \
                    or ["fit check failed"]
                problems = ["the layout --check failed: " + f for f in fails]
                problems.append("Shorten the body so it fits the A2 at print size.")

            if attempt >= 3:
                die("Prompt 4 email failed the gates after 3 attempts (structure, "
                    "house style, factual grounding, and/or fit):\n  - "
                    + "\n  - ".join(problems))
            print("[gate] issues found, rerunning Prompt 4:")
            for x in problems:
                print(f"  - {x}")
            feedback_block = ("YOUR PREVIOUS DRAFT FAILED THESE HARD CHECKS. Fix every one "
                              "and keep everything else. Any 'unsupported claim' is a fact "
                              "the research does not support: cut it or replace it with one "
                              "the research supports.\n- " + "\n- ".join(problems))

        write_file(os.path.join(folder, "copy.md"), copy_part)
        write_file(os.path.join(folder, "factcheck.md"), factcheck or "(none returned)")

    output_path = os.path.join(folder, f"{slugify(args.name)}-{slugify(args.company)}.png")
    print("\n[engine] rendering the print-ready email...")
    rc, out = run_email_engine(engine_path, data_path, output_path=output_path)
    print(out.strip())
    if rc != 0 or not os.path.exists(output_path):
        die("the email render failed (see engine output above).")

    final = json.loads(read_file(data_path))
    meta["piece_reference"] = (f'a cold email printed at A2, subject '
                               f'"{final.get("subject", "")}"')
    write_file(os.path.join(folder, "meta.json"), json.dumps(meta, indent=2))

    print(f"\n[rendered] {os.path.relpath(output_path, REPO_ROOT)}")
    print("[fact check] runner/.../factcheck.md holds the copy claims for your "
          "sign-off before print.")
    delivery_date = getattr(args, "delivery_date", "") or "to be confirmed on delivery"
    run_chain_after_render(config, folder, output_path, delivery_date)


def _generate_claymation(args, config, folder, base_values, brief, meta):
    template = load_template("prompt4_copy_claymation.md")
    values = dict(base_values, **{
        "core_problem": brief.get("core_problem", ""),
        "key_metric": brief.get("key_metric", ""),
        "environment": brief.get("environment", ""),
        "moment": brief.get("moment", ""),
        "problem_label": brief.get("problem_label", ""),
        "operational_details": brief.get("operational_details", ""),
        "absurdity": brief.get("absurdity", ""),
    })
    model = model_for(config, "p4")
    research = base_values["research"]
    attempt, feedback_block = 0, ""
    while True:
        attempt += 1
        print(f"\n[prompt 4] writing claymation copy ({model}, attempt {attempt})...")
        reply, clay = cli_json(fill(template, dict(values, feedback_block=feedback_block)), model)
        print(f"[prompt 4b] factual grounding check ({model_for(config, 'p4b')})...")
        grounded, issues = grounding_check(config, research, claymation_copy_text(clay))
        if grounded:
            break
        problems = [f"unsupported claim \"{i.get('claim', '')}\": {i.get('issue', '')}"
                    for i in issues]
        if attempt >= 2:
            die("Prompt 4 claymation failed the factual grounding gate twice:\n  - "
                + "\n  - ".join(problems))
        print("[gate] unsupported claims, rerunning Prompt 4 once:")
        for x in problems:
            print(f"  - {x}")
        feedback_block = ("YOUR PREVIOUS DRAFT CONTAINED CLAIMS THE RESEARCH DOES NOT "
                          "SUPPORT. Remove or replace each, keep everything else.\n- "
                          + "\n- ".join(problems))
    copy_part, factcheck = split_factcheck(reply)
    write_file(os.path.join(folder, "copy.md"), copy_part)
    write_file(os.path.join(folder, "claymation_copy.json"), json.dumps(clay, indent=2))
    write_file(os.path.join(folder, "factcheck.md"), factcheck or "(none returned)")

    # --- Prompt 5: assemble the paste-ready image prompt ---
    # P5 is the verbatim general Assembly Agent. It receives Layer A (with its
    # [LAYER B]/[LAYER C]/[CAPTION] slots), the Layer B brief fields, the literal
    # [AUTO-ASSIGNED] Layer C tags (resolved from context), and the copy content
    # (scene description, visual details, caption), then assembles the prompt.
    p5 = load_template("prompt5_assembly_claymation.md")
    vd = clay.get("visual_details", "")
    if isinstance(vd, list):
        vd = "; ".join(str(x) for x in vd)
    p5_values = dict(values, **{
        "layer_a_claymation": load_template("layer_a_claymation.md"),
        "scene_description": clay.get("scene_description", ""),
        "visual_details": vd,
        "caption": clay.get("caption", ""),
    })
    print(f"[prompt 5] assembling the paste-ready image prompt ({model_for(config, 'p5')})...")
    image_prompt = cli_text(fill(p5, p5_values), model_for(config, "p5"))
    write_file(os.path.join(folder, "image_prompt.txt"), image_prompt)

    # Companion card hook comes from the brief (Prompt 2), not Prompt 4 (Notion).
    meta["piece_reference"] = f'the claymation scene captioned "{clay.get("caption", "")}"'
    write_file(os.path.join(folder, "meta.json"), json.dumps(meta, indent=2))

    print("\n" + "=" * 70)
    print("CLAYMATION COPY AND PASTE-READY IMAGE PROMPT WRITTEN")
    print("=" * 70)
    print(f"\nFolder: {os.path.relpath(folder, REPO_ROOT)}")
    print("  image_prompt.txt   <- paste this into ChatGPT image generation, then upscale")
    print("  copy.md            <- scene, caption, in-scene text")
    print("  factcheck.md       <- review against the rendered image before print")
    print("\nClaymation has no rendered image at this stage, so the QC + follow-up "
          "chain does not run automatically. Generate and upscale the image from "
          "image_prompt.txt, then run `qc` and `followup` against it manually.")
    print("\nFACT CHECK LIST:\n" + (factcheck or "(none returned)"))
    _print_usage("this piece")


# --- QC (Prompts 6 + 6B) and follow-up (Prompt 7): shared steps -------------
# These run both standalone (cmd_qc / cmd_followup) and chained from generate.

_6B_PHRASES = ("WOULD TAKE THE MEETING", "WOULD ENGAGE IF FOLLOWED UP WELL",
               "WOULD ADMIRE AND IGNORE", "WOULD BIN")


def extract_6b_verdict(text):
    """The 6B verdict, robust to prose that DISCUSSES other verdict options.
    Never scan in fixed phrase-priority order: comparative prose ('narrowly
    avoids WOULD ADMIRE AND IGNORE territory... VERDICT: WOULD BIN') would let
    a mention of a better verdict mask the real one, and this parser drives the
    chain stop, ship-check's WOULD BIN hold and calibration. Preference:
    the structured JSON tail (authoritative), then the earliest phrase after
    the LAST 'VERDICT' marker, then the phrase mentioned last in the text."""
    s = extract_6b_structured(text)
    if s:
        v = str(s.get("verdict", "")).upper()
        for phrase in _6B_PHRASES:
            if phrase in v:
                return phrase
    up = text.upper()
    i = up.rfind("VERDICT")
    if i != -1:
        seg = up[i:]
        best = None
        for phrase in _6B_PHRASES:
            p = seg.find(phrase)
            if p != -1 and (best is None or p < best[0]):
                best = (p, phrase)
        if best:
            return best[1]
    best = None
    for phrase in _6B_PHRASES:
        p = up.rfind(phrase)
        if p != -1 and (best is None or p > best[0]):
            best = (p, phrase)
    return best[1] if best else None


def extract_6b_structured(text):
    """Parse the machine-readable JSON tail 6B now emits. Returns the dict or
    None (older qc_recipient.md files predate the tail; callers fall back to
    the prose). The verdict inside the JSON also satisfies extract_6b_verdict's
    phrase scan, so the two stay consistent."""
    blocks = re.findall(r"```json\s*(.*?)```", text, re.DOTALL)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("verdict") else None


def extract_p6_verdict(text):
    """Read the P6 verdict robustly. The model may wrap it in markdown
    (e.g. '**VERDICT: BORDERLINE**'), so find the first 'VERDICT' anywhere and
    take the earliest verdict keyword on that line. Returns FAIL / BORDERLINE /
    PASS / None. Correctness-critical: the chain's FAIL stop-gate depends on it."""
    up = text.upper()
    i = up.find("VERDICT")
    if i == -1:
        return None
    line_end = up.find("\n", i)
    seg = up[i: line_end if line_end != -1 else i + 60]
    best = None
    for v in ("PASS", "FAIL", "BORDERLINE"):
        p = seg.find(v)
        if p != -1 and (best is None or p < best[0]):
            best = (p, v)
    return best[1] if best else None


def _p6(config, folder, image_path, meta, brief, research):
    legibility = "Rendered by the layout engine; all text is guaranteed legible."
    lp = os.path.join(folder, "image_prompt.txt")
    if os.path.exists(lp):
        txt = read_file(lp)
        idx = txt.find("LEGIBILITY CHECK:")
        legibility = txt[idx + len("LEGIBILITY CHECK:"):].strip() if idx != -1 else txt
    fmt_label = {"newspaper": "Newspaper Front Page",
                 "crossword": "Crossword",
                 "email": "The Email"}.get(meta["format"], "Claymation Scene")
    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "format": fmt_label,
        "core_problem": brief.get("core_problem", ""), "key_metric": brief.get("key_metric", ""),
        "problem_label": brief.get("problem_label", ""),
        "operational_details": brief.get("operational_details", ""),
        "research": research, "legibility_checklist": legibility,
    }
    print(f"[prompt 6] reviewing the image ({model_for(config, 'p6')}, vision)...")
    review = vision_call(config, fill(load_template("prompt6_review.md"), values),
                         model_for(config, "p6"), image_path)
    write_file(os.path.join(folder, "qc_review.md"), review)
    _stamp_qc(folder, "qc_review.md", image_path)
    return review, extract_p6_verdict(review)


_P6B_STAGE_PIECE = (
    "You are judging the artefact alone. A companion card carrying the sender's "
    "message and a three-touch follow-up ship with every piece but are not shown "
    "here: the piece earns attention, the card and follow-up convert it. Judge the "
    "artefact on the jobs only it can do (survive the first five seconds, earn the "
    "desk, get shown to a colleague) and do not name the absent sender or absent "
    "ask as the weakest element: they arrive with the card. Aim the highest-leverage "
    "change at what the companion card and the first follow-up touch must supply to "
    "convert the attention this piece earns.")

_P6B_STAGE_PACKAGE = (
    "You have seen the full package exactly as it lands: the artefact plus the "
    "companion card read first. Judge the send as a whole: does the card's message "
    "convert the attention the piece earns, does the ask land for this specific "
    "person, and what would still stop them responding before the first follow-up "
    "touch arrives.")


def _p6b(config, folder, image_path, meta, research, package=False):
    """Recipient simulation, two stages. Piece stage (default): the box holds the
    artefact alone, judged on what the artefact alone must do; this is the verdict
    P7 and the card copy build on, written to qc_recipient.md. Package stage: the
    box holds artefact + companion card (read from <slug>-card.json, generated or
    imported), judged as the full send; runs once the card exists and writes
    qc_package.md. Ship-check and calibration prefer the package verdict."""
    slug = os.path.basename(os.path.normpath(folder))
    card_context = ""
    if package:
        card_path = os.path.join(folder, slug + "-card.json")
        if not os.path.exists(card_path):
            die(f"package QC needs the card copy at {slug}-card.json — render the "
                f"card (step 8b) or import custom copy with `package-qc --card-file`.")
        card = json.loads(read_file(card_path))
        lines = [card.get("salutation", "").rstrip(",") + ","] + list(card.get("body", []))
        # The engine prints a contact block at the foot of every generated card;
        # omit it and the simulation wrongly judges the send as unsigned (two
        # "anonymous sender" verdicts shipped through exactly that gap).
        contact = card.get("contact") or {}
        foot = ", ".join(str(v) for v in (contact.get("name"), contact.get("company"),
                                          contact.get("email"), contact.get("phone")) if v)
        if foot:
            lines.append("[printed at the foot of the card] " + foot)
        card_text = "\n".join(x for x in lines if x.strip())
        if not card_text.strip():
            die(f"{slug}-card.json has no card text to show the simulation.")
        card_context = ("\nOn top of the piece sat an A6 companion card, which you "
                        "read first. It says:\n\n" + card_text + "\n")
    sender = (config.get("sender") or meta.get("sender") or {})
    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "research": research,
        "card_context": card_context,
        "stage_note": _P6B_STAGE_PACKAGE if package else _P6B_STAGE_PIECE,
        "sender_company": sender.get("company", "unknown"),
        "sender_what": sender.get("what_they_sell", ""),
    }
    label = "prompt 6B package" if package else "prompt 6B"
    print(f"[{label}] simulating the recipient ({model_for(config, 'p6b')}, vision)...")
    sixb = vision_call(config, fill(load_template("prompt6b_recipient.md"), values),
                       model_for(config, "p6b"), image_path)
    qc_name = "qc_package.md" if package else "qc_recipient.md"
    write_file(os.path.join(folder, qc_name), sixb)
    _stamp_qc(folder, qc_name, image_path)
    return sixb, extract_6b_verdict(sixb)


def _piece_reference(folder, meta):
    """Describe the piece from data.json (the rendered copy) at call time. The
    reference frozen into meta.json at build time goes stale the moment copy is
    revised post-build (a live failure: a follow-up referenced a subtitle that
    had been cut), so derive it from what actually shipped and fall back to the
    snapshot only when data.json is missing."""
    fmt = meta.get("format")
    data_path = os.path.join(folder, "data.json")
    if os.path.exists(data_path):
        try:
            d = json.loads(read_file(data_path))
            if fmt == "newspaper":
                return (f'the front page of "{d.get("masthead_name", "")}", '
                        f'headline "{d.get("headline", "")}"')
            if fmt == "crossword":
                return (f'the "{d.get("company_name", "")}" crossword, '
                        f'subtitle "{d.get("subtitle", "")}"')
            if fmt == "email":
                return f'a cold email printed at A2, subject "{d.get("subject", "")}"'
        except (ValueError, OSError):
            pass
    return meta.get("piece_reference", "")


def _p7(config, folder, delivery_date):
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    brief = json.loads(read_file(os.path.join(folder, "brief.json")))
    research = read_file(os.path.join(folder, "research.md"))
    # Live config, not the meta.json snapshot: proof points evolve between build
    # and follow-up, and the snapshot quoted stale proof in a live batch.
    sender = config.get("sender") or meta["sender"]

    sixb_path = os.path.join(folder, "qc_recipient.md")
    if os.path.exists(sixb_path):
        sixb = read_file(sixb_path)
        verdict = extract_6b_verdict(sixb) or "WOULD ENGAGE IF FOLLOWED UP WELL"
        parsed = extract_6b_structured(sixb)
        if parsed:
            verdict = parsed.get("verdict", verdict)
            leverage = (parsed.get("highest_leverage_change", "")
                        + "\n\nFull 6B recipient simulation for context:\n" + sixb)
            failure = parsed.get("failure_mode", "See the full 6B analysis above.")
            stopped = parsed.get("what_stopped_them", "See the full 6B analysis above.")
        else:
            leverage = "Full 6B recipient simulation:\n" + sixb
            failure = "See the full 6B analysis in the highest-leverage field above."
            stopped = "See the full 6B analysis in the highest-leverage field above."
    else:
        print("[note] no qc_recipient.md found. Run `qc` first for a 6B-grounded "
              "sequence. Proceeding without the simulation.")
        verdict = "Not available (6B not run). Treat as WOULD ENGAGE IF FOLLOWED UP WELL."
        leverage = failure = stopped = "Not available (run qc first)."

    fmt_label = {"newspaper": "Newspaper Front Page",
                 "crossword": "Crossword",
                 "email": "The Email"}.get(meta["format"], "Claymation Scene")

    # Crossword holdback: hand P7 the open-loop record when the build wrote one.
    # "none" keeps the closing-the-loop section inert (backward compatible).
    ol = meta.get("open_loop") if isinstance(meta.get("open_loop"), dict) else None
    if ol and meta.get("format") == "crossword":
        # The stored clue number is a build-time snapshot; a re-render after a
        # copy fix renumbers the grid (the piece_reference failure class). Verify
        # against the current data.json and correct or drop the record.
        data_path = os.path.join(folder, "data.json")
        if os.path.exists(data_path):
            try:
                engine_path = os.path.join(REPO_ROOT,
                                           config.get("crossword_engine",
                                                      "crossword/crossword.py"))
                live = _crossword_placement(engine_path,
                                            json.loads(read_file(data_path)),
                                            ol.get("answer", ""))
            except Exception as e:                       # lookup is best-effort
                print(f"[open-loop] placement re-check failed ({e}); "
                      "using the stored record.")
                live = {"number": ol.get("clue_number"),
                        "direction": ol.get("direction")}
            if live is None:
                print(f"[open-loop] '{ol.get('answer', '')}' is no longer in the "
                      "current grid (re-rendered since the record was written); "
                      "dropping the open-loop record for this follow-up.")
                ol = None
            elif (live["number"] != ol.get("clue_number")
                  or live["direction"] != ol.get("direction")):
                print(f"[open-loop] grid renumbered since the record was written: "
                      f"{ol.get('clue_number')} {ol.get('direction')} -> "
                      f"{live['number']} {live['direction']}. Using the live number.")
                ol = dict(ol, clue_number=live["number"], direction=live["direction"])
    if ol and ol.get("clue_number") and ol.get("metric"):
        open_loop_block = (
            f"the grid answer {ol.get('answer', '')} at "
            f"{ol['clue_number']} {str(ol.get('direction', '')).title()}, printed clue "
            f"\"{ol.get('clue', '')}\". The metric behind it (which the recipient does "
            f"not have): {ol['metric']}. The question it answers for them: "
            f"{ol.get('question', '')}. Tier {str(ol.get('tier', '')).upper() or 'B'}"
            + (f"; the sender has computed the actual number: {ol['tier_A_number']}"
               if str(ol.get("tier_A_number", "")).strip() else
               " (the sender offers to measure it)"))
    else:
        open_loop_block = "none"

    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "format": fmt_label,
        "open_loop_block": open_loop_block,
        "piece_reference": _piece_reference(folder, meta),
        "problem_label": brief.get("problem_label", ""), "core_problem": brief.get("core_problem", ""),
        "key_metric": brief.get("key_metric", ""), "operational_details": brief.get("operational_details", ""),
        "companion_card_hook": meta.get("companion_card_hook", "") or brief.get("companion_card_hook", ""),
        # Reserve detail comes from the brief (Prompt 2). "none" means the research
        # offered no second verifiable detail; pass "" so Prompt 7's missing-reserve
        # rule fires legitimately rather than treating the literal word as a fact.
        "reserve_detail": "" if str(brief.get("reserve_detail", "")).strip().lower() in ("", "none") else brief["reserve_detail"],
        "research": research,
        "verdict_6b": verdict, "failure_mode_6b": failure, "leverage_6b": leverage, "stopped_6b": stopped,
        "sender_name": sender.get("sender_name", ""), "sender_company": sender.get("company", ""),
        "sender_what": sender.get("what_they_sell", ""), "sender_proof": sender.get("proof_points", ""),
        "booking_link": sender.get("booking_link", ""), "delivery_date": delivery_date,
        "custom_card": "yes" if sender.get("custom_card") else "no",
    }
    base_prompt = fill(load_template("prompt7_followup.md"), values)
    # The follow-up goes out under the sender's name carrying factual and proof
    # claims, so it passes the same P4b grounding gate as printed copy. A shipped
    # card once over-attributed the sender's record; this catches that class.
    sender_facts = "\n".join(
        f"- {x}" for x in (sender.get("company", ""), sender.get("what_they_sell", ""),
                           sender.get("proof_points", "")) if str(x).strip())
    if open_loop_block != "none":
        # The open-loop metric (and any Tier A number) is a sender-supplied fact
        # that exists in neither the research nor the profile; without this the
        # grounding gate would flag the reveal as unsupported and strip the
        # mechanic's payoff.
        sender_facts += ("\n- Open-loop record for this piece (sender-supplied; the "
                         "metric and any revealed number are the sender's own "
                         "assertions, legitimate for the follow-up to state): "
                         + open_loop_block)
    reply, gate = None, {"grounded": None, "attempts": 0, "issues": []}
    for attempt in range(1, 4):
        prompt = base_prompt
        if gate["issues"]:
            prompt += ("\n\n---\n\n**A fact-checker reviewed your previous attempt and "
                       "flagged these claims as unsupported by the research or the sender "
                       "profile. Rewrite the sequence fixing every one (drop or correct "
                       "the claim; never argue with the checker):**\n"
                       + "\n".join(f"- \"{i.get('claim', '')}\": {i.get('issue', '')}"
                                   for i in gate["issues"]))
        print(f"[prompt 7] writing the companion card and 3-touch follow-up "
              f"({model_for(config, 'p7')}, attempt {attempt})...")
        reply = cli_text(prompt, model_for(config, "p7"))
        print(f"[prompt 4b] grounding the follow-up copy ({model_for(config, 'p4b')})...")
        grounded, issues = grounding_check(config, research, reply, sender_facts=sender_facts)
        gate = {"grounded": grounded, "attempts": attempt, "issues": issues}
        if grounded:
            break
        print(f"[prompt 4b] {len(issues)} unsupported claim(s) in the follow-up"
              + (" — regenerating with feedback." if attempt < 3 else "."))
    write_file(os.path.join(folder, "followup.md"), reply)
    write_file(os.path.join(folder, "followup_gate.json"),
               json.dumps(gate, indent=2, ensure_ascii=False))
    if not gate["grounded"]:
        print("\n" + "!" * 70)
        print("FOLLOW-UP GROUNDING FAILED after 3 attempts. followup.md is written but"
              "\ncarries unsupported claims (see followup_gate.json). Fix by hand or"
              "\nre-run `followup` before sending any touch.")
        print("!" * 70)
    return reply


# --- standalone qc / followup commands -------------------------------------

def cmd_qc(args):
    _usage_reset()
    config = load_config(args.config)
    folder = args.folder
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    brief = json.loads(read_file(os.path.join(folder, "brief.json")))
    research = read_file(os.path.join(folder, "research.md"))

    review, v6 = _p6(config, folder, args.image, meta, brief, research)
    if v6 == "FAIL":
        print("\n" + "=" * 70)
        print("PROMPT 6: FAIL — 6B not run.")
        print("=" * 70)
        print("Reason and regeneration instructions (full review: qc_review.md):\n")
        print(review)
        _print_usage("qc")
        return
    sixb, v6b = _p6b(config, folder, args.image, meta, research)
    # Refresh the package pass alongside: a re-QC that left qc_package.md built
    # against the previous render would trip ship-check's staleness hold.
    slug = os.path.basename(os.path.normpath(folder))
    vpkg = None
    if os.path.exists(os.path.join(folder, slug + "-card.json")):
        _, vpkg = _p6b(config, folder, args.image, meta, research, package=True)
    print("\n" + "=" * 70)
    print("QC COMPLETE")
    print("=" * 70)
    print("  qc_review.md     <- Prompt 6 craft review")
    print("  qc_recipient.md  <- Prompt 6B recipient simulation (piece alone)")
    if vpkg:
        print("  qc_package.md    <- Prompt 6B package pass (piece + companion card)")
    print(f"\nPrompt 6 verdict:  {v6 or '(see qc_review.md)'}")
    print(f"Prompt 6B verdict: {v6b or '(see qc_recipient.md)'}")
    if vpkg:
        print(f"6B package verdict: {vpkg}")
    _print_usage("qc")


def _cold_review_one(config, folder):
    """The automated two-chat review for one piece: a genuinely blind read of
    the render (fresh context, no research - the one look the gated chain never
    takes), a strategist pass that reveals the target only after the blind read
    is captured, and a render fact-check against the research (the pass a human
    would do by pasting the image back into the research chat). Advisory: writes
    qc_cold.md and qc_render_factcheck.md, gates nothing."""
    slug = os.path.basename(os.path.normpath(folder))
    image = os.path.join(folder, slug + ".png")
    if not os.path.exists(image):
        print(f"[cold-review] {slug}: no render, skipped.")
        return None
    # Resume-friendly: a piece whose reviews are both fresher than its render is
    # done; a session-limit halt mid-batch can be re-run without redoing them.
    cold_path = os.path.join(folder, "qc_cold.md")
    facts_path = os.path.join(folder, "qc_render_factcheck.md")
    if all(os.path.exists(p) and os.path.getmtime(p) > os.path.getmtime(image)
           for p in (cold_path, facts_path)):
        print(f"[cold-review] {slug}: reviews already fresh, skipped.")
        strat = read_file(cold_path)
        m = re.search(r"COLD REVIEW:\s*(PRINT AS IS|PRINT AFTER TWEAKS|DO NOT PRINT)", strat)
        fc = extract_6b_structured(read_file(facts_path)) or {}
        return {"slug": slug, "cold": m.group(1) if m else None,
                "facts": fc.get("verdict"), "fc_detail": fc}
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    research = read_file(os.path.join(folder, "research.md"))
    model = model_for(config, "p6b")
    values = {"recipient_name": meta["recipient_name"],
              "recipient_title": meta["recipient_title"],
              "recipient_company": meta["recipient_company"]}

    print(f"[cold review] {slug}: blind read ({model}, vision)...")
    cold = vision_call(config, load_template("prompt6c_cold_eyes.md"), model, image)
    print(f"[cold review] {slug}: strategist pass ({model}, vision)...")
    strat = vision_call(config, fill(load_template("prompt6c_strategist.md"),
                                     dict(values, cold_read=cold)), model, image)
    write_file(os.path.join(folder, "qc_cold.md"),
               "# Cold-eyes read (no context)\n\n" + cold +
               "\n\n# Strategist pass (target revealed)\n\n" + strat)
    m = re.search(r"COLD REVIEW:\s*(PRINT AS IS|PRINT AFTER TWEAKS|DO NOT PRINT)", strat)
    cold_verdict = m.group(1) if m else None

    print(f"[cold review] {slug}: render fact-check ({model}, vision)...")
    facts = vision_call(config, fill(load_template("prompt6d_render_factcheck.md"),
                                     dict(values, research=research)), model, image)
    write_file(os.path.join(folder, "qc_render_factcheck.md"), facts)
    fc = extract_6b_structured(facts) or {}
    return {"slug": slug, "cold": cold_verdict,
            "facts": fc.get("verdict"), "fc_detail": fc}


def cmd_cold_review(args):
    _usage_reset()
    config = load_config(args.config)
    if args.manifest:
        entries, _ = _load_manifest(args.manifest)
        folders = [os.path.join(PIECES_DIR, _piece_slug(e)) for e in entries]
    else:
        folders = [args.folder]
    results = [r for f in folders for r in [_cold_review_one(config, f)] if r]
    print("\n" + "=" * 70)
    print("COLD REVIEW — advisory, does not gate")
    print("=" * 70)
    for r in results:
        fc = r["fc_detail"]
        counts = (f"({fc.get('verified', '?')} verified, {fc.get('stretched', '?')} "
                  f"stretched, {fc.get('unsupported', '?')} unsupported)") if fc else ""
        print(f"  {r['slug']}")
        print(f"      cold eyes:  {r['cold'] or '(see qc_cold.md)'}")
        print(f"      fact-check: {r['facts'] or '(see qc_render_factcheck.md)'} {counts}")
    print("\nFull reads: qc_cold.md and qc_render_factcheck.md in each piece folder.")
    _print_usage("cold-review")


def cmd_package_qc(args):
    """The package pass on demand: judge piece + companion card as the full send.
    For generated cards the chain runs this automatically after step 8b; this
    command serves custom-card senders (import the card copy with --card-file)
    and re-runs after a card revision."""
    _usage_reset()
    config = load_config(args.config)
    folder = args.folder
    slug = os.path.basename(os.path.normpath(folder))
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    research = read_file(os.path.join(folder, "research.md"))
    card_path = os.path.join(folder, slug + "-card.json")

    if args.card_file:
        text = read_file(args.card_file).strip()
        if not text:
            die(f"{args.card_file} is empty.")
        paras = [re.sub(r"\s*\n\s*", " ", p).strip()
                 for p in re.split(r"\n\s*\n", text) if p.strip()]
        first = (meta.get("recipient_name", "").split() or [""])[0]
        salutation = first
        # A short first paragraph naming the recipient is the salutation line.
        if len(paras) > 1 and len(paras[0].split()) <= 3 and first.lower() in paras[0].lower():
            salutation = paras.pop(0).rstrip(",")
        if os.path.exists(card_path):
            print(f"[package-qc] replacing existing {slug}-card.json with the imported copy.")
        write_file(card_path, json.dumps(
            {"custom": True, "salutation": salutation, "body": paras}, indent=2))
        print(f"[package-qc] imported card copy ({len(paras)} paragraph(s)) "
              f"-> {os.path.relpath(card_path, REPO_ROOT)}")

    image = args.image or os.path.join(folder, slug + ".png")
    if not os.path.exists(image):
        die(f"no render found at {image} — pass --image explicitly.")
    _, vpkg = _p6b(config, folder, image, meta, research, package=True)
    print("\n" + "=" * 70)
    print("PACKAGE QC COMPLETE")
    print("=" * 70)
    print("  qc_package.md  <- Prompt 6B package pass (piece + companion card)")
    print(f"\n6B package verdict: {vpkg or '(see qc_package.md)'}")
    if vpkg == "WOULD BIN":
        print("The simulation predicts the full send fails this recipient. "
              "Ship-check will hold the piece; revise the card copy and re-run.")
    _print_usage("package-qc")


# --- ship-check: print-readiness gate --------------------------------------
# The vision QC (P6 + P6B) runs immediately after the engine render in the
# normal flow, so a clean build is always QC'd against its delivered file. The
# gap is MANUAL re-renders done outside the runner — a subtitle fix, a DPI
# re-render — which leave the delivered PNG newer than its qc_*.md, i.e. never
# seen by the vision model in the form that ships. ship-check holds those (and
# missing QC, P6 FAIL, 6B WOULD BIN) so a re-rendered piece cannot reach the
# print supplier unverified.

_BANNED_FOLLOWUP_LINES = (
    "something arrived on your desk",   # stock opener, banned by batch collision rule
    "know it when you see it",          # stock reception nudge, same rule
    "quick follow up", "following up briefly",
    "best regards", "kind regards",
)
_LITIGATION_WORDS = re.compile(
    r"\b(litigation|lawsuit|high court|tribunal|legal dispute|redundanc\w*)\b", re.I)
_GRID_REF = re.compile(r"\b\d+[\s-]?(across|down)\b", re.I)


def _strings_in(obj):
    """Yield every string value in a JSON structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings_in(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings_in(v)


def _followup_body(text):
    """The lintable part of followup.md: everything before the FACT CHECK LIST
    (sources legitimately contain URLs and case names), minus Flag: lines (the
    model's own refusal notes may name what they refused)."""
    idx = text.upper().find("FACT CHECK LIST")
    body = text[:idx] if idx != -1 else text
    return "\n".join(l for l in body.splitlines() if not l.strip().startswith("Flag:"))


def crossword_subtitle_words(folder):
    """Normalised subtitle word set for the batch near-duplicate check, minus
    the company's own tokens (the company name legitimately recurs). Returns
    None for non-crossword pieces (only crossword data.json has candidates)."""
    path = os.path.join(folder, "data.json")
    if not os.path.exists(path):
        return None
    try:
        data = json.loads(read_file(path))
    except Exception:
        return None
    if "candidates" not in data:
        return None
    sub = str(data.get("subtitle", "") or "")
    if not sub:
        return None
    company = re.sub(r"[^a-z0-9 ]", "", str(data.get("company_name", "")).lower()).split()
    skip = set(company) | {c + "s" for c in company}   # covers possessives
    words = set(re.sub(r"[^a-z0-9 ]", "", sub.lower()).split()) - skip
    return words or None


def touch2_opener(folder):
    """First line of Touch 2, normalised, for the batch duplicate check."""
    path = os.path.join(folder, "followup.md")
    if not os.path.exists(path):
        return None
    text = read_file(path)
    m = re.search(r"TOUCH 2 LINKEDIN[^\n]*\n+(.+)", text)
    if not m:
        return None
    line = re.sub(r"[^a-z0-9 ]", "", m.group(1).strip().strip("*").lower())
    return line[:60] or None


_CARD_FIXTURES = ("my email and number are below",
                  "if easier ill follow up by email this week")


def card_ask(folder):
    """The card's closing ask, normalised, minus the sanctioned fixture
    sentences (the contact-block pointer and the backup-email signal), for the
    batch duplicate check. An example ask copied verbatim from the template
    shipped on 3 cards in one batch."""
    slug = os.path.basename(os.path.normpath(folder))
    path = os.path.join(folder, slug + "-card.json")
    if not os.path.exists(path):
        return None
    try:
        body = json.loads(read_file(path)).get("body") or []
    except (ValueError, OSError):
        return None
    if not body:
        return None
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", str(body[-1])) if s.strip()]
    kept = [s for s in sentences
            if re.sub(r"[^a-z0-9 ]", "", s.lower()).strip() not in _CARD_FIXTURES]
    return re.sub(r"[^a-z0-9 ]", "", " ".join(kept).lower()).strip() or None


def touch1_subject(folder):
    """Touch 1 subject line, normalised, for the batch duplicate check (the
    object-first subject rule pulls same-format pieces toward identical
    subjects; three shipped that way in one batch)."""
    path = os.path.join(folder, "followup.md")
    if not os.path.exists(path):
        return None
    m = re.search(r"^Subject:\s*(.+)$", read_file(path), re.M)
    if not m:
        return None
    return re.sub(r"[^a-z0-9 ]", "", m.group(1).strip().lower()) or None


# Gate field-of-vision coverage: the copy-text builders define what P4b sees,
# so any data.json text zone they omit is ungated by construction (the class
# that shipped a fabricated edition-line city). This check makes the rule
# "add new rendered fields to the builder in the same commit" mechanical:
# every string leaf in data.json must either appear in the builder's output
# or be an explicitly exempted piece of furniture below. A new engine field
# therefore fails ship-check until someone consciously routes or exempts it.

_COPY_TEXT_EXEMPT = {
    # Fictional furniture by design (P4b is told never to flag the masthead
    # and bylines) plus the date line.
    "newspaper": {"masthead_name", "byline", "date",
                  "sidebar_1_byline", "sidebar_2_byline", "sidebar_3_byline"},
    "crossword": set(),
    # Gmail chrome and runner-injected sender identity (human-owned config,
    # never P4 output). body[].type is a schema tag, not rendered text.
    "email": {"copy_source", "sender", "recipient", "account",
              "timestamp", "label", "type"},
}


def ungated_copy_fields(data, fmt):
    """Key paths of data.json string values invisible to the grounding gate:
    present in the piece data but absent from the copy text P4b reads and not
    exempted as furniture. Empty list means full gate coverage."""
    builders = {"newspaper": newspaper_copy_text, "crossword": crossword_copy_text,
                "email": email_copy_text}
    builder = builders.get(fmt)
    if not builder:
        return []
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    text = norm(builder(data))
    exempt = _COPY_TEXT_EXEMPT.get(fmt, set())
    missing = []

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in exempt:
                    continue
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            s = norm(obj)
            if len(s) >= 3 and s not in text:   # <3 chars: too short to gate
                missing.append(path)

    walk(data, "")
    return missing


def _copy_lint(folder, fmt):
    """Deterministic checks for the text rules the prompts promise but nothing
    enforced. Cheap, no model calls, run on every ship-check. Returns
    (holds, warns)."""
    holds, warns = [], []

    data_path = os.path.join(folder, "data.json")
    if os.path.exists(data_path):
        try:
            data = json.loads(read_file(data_path))
        except (ValueError, OSError):
            data = None
            holds.append("lint: data.json exists but cannot be parsed — every "
                         "data lint is blind until it is fixed")
        if data:
            strings = list(_strings_in(data))
            if any("—" in s for s in strings):
                holds.append("lint: em dash in data.json copy (house rule: none)")
            cite = next((m.group(0) for s in strings
                         for m in [re.search(r"\[[^\]\[]{3,60}\]", s)] if m), None)
            if cite:
                holds.append(f"lint: bracket citation printed in copy ({cite}) — "
                             "citations live in the fact-check list; the piece "
                             "attributes in prose")
            if any("!" in s for s in strings):
                holds.append("lint: exclamation mark in data.json copy (house rule: none)")
            for path in ungated_copy_fields(data, fmt):
                holds.append(f"lint: data.json field \"{path}\" is invisible to the "
                             "grounding gate (absent from the copy-text builder) — "
                             "recipient-visible text must be routed through "
                             f"{fmt}_copy_text or exempted as furniture")
            if fmt == "crossword":
                fields = [data.get("title", ""), data.get("subtitle", "")]
                fields += [c.get("clue", "") for c in data.get("candidates", [])]
                bad = [f for f in fields if _GRID_REF.search(f or "")]
                if bad:
                    holds.append(f"lint: grid-number reference in crossword copy "
                                 f"(\"{bad[0].strip()[:50]}\") — numbering changes on "
                                 "regeneration, reference will dangle")

    fu_path = os.path.join(folder, "followup.md")
    if os.path.exists(fu_path):
        text = read_file(fu_path)
        if "WOULD BIN" in text and "suppressed" in text:
            return holds, warns          # suppression notice, nothing to lint
        body = _followup_body(text)
        low = body.lower()
        for phrase in _BANNED_FOLLOWUP_LINES:
            if phrase in low:
                holds.append(f"lint: banned line \"{phrase}\" in followup.md")
        if "—" in body:
            holds.append("lint: em dash in followup.md (house rule: none)")
        if "http://" in body or "https://" in body:
            warns.append("lint: URL in follow-up touch body — permitted only as a "
                         "final CTA line, check it")
        m = _LITIGATION_WORDS.search(body)
        if m:
            warns.append(f"lint: possible litigation/misfortune leverage in follow-up "
                         f"(\"{m.group(0)}\") — banned as leverage, review the line")
        if "TOUCH 1 EMAIL" in body and not re.search(r"^Subject:", body, re.M):
            holds.append("lint: Touch 1 has no subject line — it is the recipient's "
                         "first-ever email from the sender (re-run followup)")
        # Within-sequence repetition: the same closing ask restated across
        # touches reads as nagging (a live failure: one sequence closed on the
        # same routing ask 3 times). Compare each core touch's closing line;
        # a shared 4-word run or heavy word overlap holds the sequence. The
        # connection-note and nudge variants are alternatives, not extra sends,
        # so they are not compared.
        closes = {}
        for label, pat in (("Touch 1", r"TOUCH 1 EMAIL"),
                           ("Touch 2", r"TOUCH 2 LINKEDIN"),
                           ("Touch 3", r"TOUCH 3 BUMP")):
            sect = re.search(pat + r"[^\n]*\n(.*?)(?=\n\*\*|\Z)", body, re.DOTALL)
            if sect:
                lines = [l.strip() for l in sect.group(1).splitlines()
                         if l.strip() and not re.fullmatch(r"[-*_]{3,}", l.strip())]
                if lines:
                    words = re.sub(r"[^a-z0-9 ]", "", lines[-1].lower()).split()
                    closes[label] = (set(words),
                                     {tuple(words[i:i + 4]) for i in range(len(words) - 3)})
        labels = list(closes)
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                (wa, sa), (wb, sb) = closes[labels[i]], closes[labels[j]]
                if not (wa and wb):
                    continue
                overlap = len(wa & wb) / min(len(wa), len(wb))
                if (sa & sb) or overlap >= 0.6:
                    holds.append(f"lint: {labels[i]} and {labels[j]} close on "
                                 "near-identical asks — each touch must advance "
                                 "the angle, not restate it (re-run followup)")

        if "CONNECTION NOTE VARIANT" not in body:
            warns.append("lint: no Touch 2 connection-note variant — follow-up predates "
                         "the current P7 template (re-run followup)")
        else:
            nm = re.search(r"CONNECTION NOTE VARIANT[^\n]*\**\n+(.*?)\n\s*\n", body, re.DOTALL)
            if nm:
                note = nm.group(1).strip().strip('"')
                note = re.sub(r"\s*\(\d+ characters?\)\s*$", "", note)
                if len(note) > 280:
                    holds.append(f"lint: connection-note variant is {len(note)} chars "
                                 "(LinkedIn truncates ~300; cap is 280)")
    return holds, warns


def _ship_status(folder):
    """Assess one piece for print-readiness. Core guard: the delivered render
    must not be newer than either vision QC file. Returns a dict with the verdict
    (shippable True/False, or None for prompt-only formats) and reasons."""
    slug = os.path.basename(os.path.normpath(folder))
    render = os.path.join(folder, slug + ".png")
    review = os.path.join(folder, "qc_review.md")
    recipient = os.path.join(folder, "qc_recipient.md")

    fmt = None
    meta_path = os.path.join(folder, "meta.json")
    if os.path.exists(meta_path):
        try:
            fmt = json.loads(read_file(meta_path)).get("format")
        except (ValueError, OSError):
            fmt = None

    # Claymation is text-only (no render, no vision QC); nothing to print-check.
    if fmt == "claymation":
        return {"slug": slug, "format": fmt, "shippable": None, "p6": None, "p6b": None,
                "pkg": None, "holds": [],
                "warns": ["claymation is prompt-only; nothing to print-check"]}

    holds, warns, p6, p6b = [], [], None, None
    if not os.path.exists(folder):
        return {"slug": slug, "format": fmt, "shippable": False, "p6": None, "p6b": None,
                "pkg": None, "holds": ["piece folder not found (build it first)"], "warns": []}
    if not os.path.exists(render):
        return {"slug": slug, "format": fmt, "shippable": False, "p6": None, "p6b": None,
                "pkg": None, "holds": [f"no render found ({slug}.png)"], "warns": []}

    png_m = os.path.getmtime(render)

    # QC freshness: prefer the qc_stamp.json hash (which render the verdict
    # actually saw) over mtime ordering; fall back to mtimes for QC files that
    # predate stamping.
    qc_stamps = {}
    qsp = os.path.join(folder, "qc_stamp.json")
    if os.path.exists(qsp):
        try:
            s = json.loads(read_file(qsp))
            if isinstance(s, dict):
                qc_stamps = s
        except (ValueError, OSError):
            pass
    render_sha = _engine_file_sha1(render) if qc_stamps else None

    def qc_stale(qc_path, qc_name):
        st = qc_stamps.get(qc_name) or {}
        if st.get("image_sha1"):
            return st["image_sha1"] != render_sha
        return os.path.getmtime(qc_path) <= png_m

    # Copy staleness: data.json newer than the render means the printed file no
    # longer matches the copy (post-QC edit, forgotten re-render) AND the edited
    # copy never re-passed the grounding gate (P4b runs inside generate only).
    dj = os.path.join(folder, "data.json")
    if os.path.exists(dj) and os.path.getmtime(dj) >= png_m:
        holds.append("data.json is NEWER than the render — the copy changed after "
                     "rendering; re-render and re-QC (edited copy has not "
                     "re-passed the grounding gate)")

    if not os.path.exists(review):
        holds.append("no qc_review.md — P6 craft QC never ran")
    else:
        p6 = extract_p6_verdict(read_file(review))
        if qc_stale(review, "qc_review.md"):
            holds.append("qc_review.md did not see the delivered render — "
                         "re-QC required")
        if p6 is None:
            holds.append("qc_review.md exists but no P6 verdict is readable — "
                         "an unreadable verdict must not pass; re-run `qc`")
        elif p6 == "FAIL":
            holds.append("P6 verdict is FAIL")
        elif p6 == "BORDERLINE":
            warns.append("P6 verdict is BORDERLINE — review craft notes before print")

    if not os.path.exists(recipient):
        holds.append("no qc_recipient.md — P6B recipient sim never ran")
    else:
        p6b = extract_6b_verdict(read_file(recipient))
        if qc_stale(recipient, "qc_recipient.md"):
            holds.append("qc_recipient.md did not see the delivered render — "
                         "re-QC required")
        if p6b is None:
            holds.append("qc_recipient.md exists but no 6B verdict is readable — "
                         "an unreadable verdict must not pass; re-run `qc`")
        elif p6b == "WOULD BIN":
            holds.append("P6B verdict is WOULD BIN — do not print")

    # Package pass (piece + companion card judged together). Optional: pieces
    # without one ship on the piece verdict alone. When it exists it is the
    # closer prediction of the send, so it must be fresh against both the render
    # and the card copy, and its WOULD BIN holds like the piece verdict's.
    pkg = None
    package = os.path.join(folder, "qc_package.md")
    card_json = os.path.join(folder, slug + "-card.json")
    if os.path.exists(package):
        pkg = extract_6b_verdict(read_file(package))
        if qc_stale(package, "qc_package.md"):
            holds.append("qc_package.md did not see the delivered render — "
                         "re-run `package-qc`")
        if os.path.exists(card_json) and \
                os.path.getmtime(card_json) >= os.path.getmtime(package):
            holds.append("card copy changed after the package pass — re-run `package-qc`")
        if pkg is None:
            holds.append("qc_package.md exists but no 6B verdict is readable — "
                         "an unreadable verdict must not pass; re-run `package-qc`")
        elif pkg == "WOULD BIN":
            holds.append("6B package verdict is WOULD BIN — the full send fails; "
                         "revise the card copy and re-run `package-qc`")
    elif os.path.exists(card_json):
        warns.append("card copy exists but no qc_package.md — run `package-qc` "
                     "to judge the full send")

    # Follow-up freshness: the sequence is built from the rendered copy and the
    # 6B verdict, so it goes stale the moment either changes (a live failure:
    # touches referenced a clue that had been cut post-P7). Held here because
    # ship-check runs right before staging, which is also when touches go live.
    fu = os.path.join(folder, "followup.md")
    if os.path.exists(fu):
        fu_m = os.path.getmtime(fu)
        data_path = os.path.join(folder, "data.json")
        if os.path.exists(data_path) and os.path.getmtime(data_path) > fu_m:
            holds.append("followup.md predates current data.json — the touches may "
                         "reference cut copy; re-run `followup`")
        if os.path.exists(recipient) and os.path.getmtime(recipient) > fu_m:
            holds.append("followup.md predates current qc_recipient.md — the sequence "
                         "was built on a superseded 6B verdict; re-run `followup`")
    else:
        warns.append("no followup.md — P7 never ran (run `followup` before the piece lands)")

    # A follow-up that failed its grounding gate three times still writes
    # followup.md; the failure must hold here, not scroll past in a build log.
    fg = os.path.join(folder, "followup_gate.json")
    if os.path.exists(fg):
        try:
            if json.loads(read_file(fg)).get("grounded") is not True:
                holds.append("follow-up failed its grounding gate "
                             "(followup_gate.json) — revise and re-run `followup`")
        except (ValueError, OSError):
            holds.append("followup_gate.json unreadable — re-run `followup`")

    lint_holds, lint_warns = _copy_lint(folder, fmt)
    holds += lint_holds
    warns += lint_warns

    # Engine staleness: warn when the engine file has changed since this render
    # (a fixed engine bug may be shipping inside a stale render). Pieces rendered
    # before stamping existed have no stamp and are not warned about.
    stamp_path = os.path.join(folder, "engine_stamp.json")
    if os.path.exists(stamp_path):
        try:
            stamp = json.loads(read_file(stamp_path))
            # Legacy flat stamp {"engine":..,"sha1":..} vs the keyed form
            # {"<output>.png": {...}}: the card render used to clobber the
            # artefact's flat stamp, which neutralised this check entirely.
            entries = {slug + ".png": stamp} if "engine" in stamp else stamp
            for out_name, s in entries.items():
                if not isinstance(s, dict):
                    continue
                ep = os.path.join(REPO_ROOT, s.get("engine", ""))
                if os.path.exists(ep) and _engine_file_sha1(ep) != s.get("sha1"):
                    warns.append(f"engine has changed since {out_name} was rendered "
                                 "— re-render recommended (delivered file may "
                                 "predate an engine fix)")
        except (ValueError, OSError):
            pass

    return {"slug": slug, "format": fmt, "shippable": not holds,
            "holds": holds, "warns": warns, "p6": p6, "p6b": p6b, "pkg": pkg}


def cmd_ship_check(args):
    if args.all:
        folders = (sorted(os.path.join(PIECES_DIR, d) for d in os.listdir(PIECES_DIR)
                          if os.path.isdir(os.path.join(PIECES_DIR, d)) and not d.startswith("_"))
                   if os.path.isdir(PIECES_DIR) else [])
    elif args.manifest:
        entries, _ = _load_manifest(args.manifest)
        folders = [os.path.join(PIECES_DIR, _piece_slug(e)) for e in entries]
    else:
        folders = [args.folder]
    if not folders:
        die("ship-check found no piece folders to check.")

    statuses = [_ship_status(f) for f in folders]

    # Batch collision check: recipients in one batch often share a professional
    # community, so identical Touch 2 openers across pieces quietly kill the
    # one-of-one claim if they ever compare notes. Only checkable across pieces.
    if len(folders) > 1:
        openers = {}
        for f, s in zip(folders, statuses):
            op = touch2_opener(f)
            if op:
                openers.setdefault(op, []).append(s)
        for op, group in openers.items():
            if len(group) > 1:
                names = ", ".join(g["slug"] for g in group)
                for g in group:
                    g["holds"].append(f"lint: Touch 2 opener duplicated across batch "
                                      f"({names}) — each needs its own opening line")
                    g["shippable"] = False

        subjects = {}
        for f, s in zip(folders, statuses):
            subj = touch1_subject(f)
            if subj:
                subjects.setdefault(subj, []).append(s)
        # Batch similarity in subjects and card asks is a WARN, not a hold:
        # the sender's call (10 Jul) is that some similar lines are fine in
        # small batches. Identical Touch 2 openers remain a hold (older rule).
        for subj, group in subjects.items():
            if len(group) > 1:
                names = ", ".join(g["slug"] for g in group)
                for g in group:
                    g["warns"].append(f"lint: Touch 1 subject duplicated across batch "
                                      f"({names}) — consider differentiating")

        asks = {}
        for f, s in zip(folders, statuses):
            a = card_ask(f)
            if a:
                asks.setdefault(a, []).append(s)
        for a, group in asks.items():
            if len(group) > 1:
                names = ", ".join(g["slug"] for g in group)
                for g in group:
                    g["warns"].append(f"lint: card ask duplicated across batch ({names}) "
                                      "— consider a piece-specific ask")

        # Same failure mode on the piece itself: two crossword subtitles built
        # from the same construction ("Built entirely from X's own vocabulary")
        # read as a template the moment two recipients compare pieces.
        subs = [(s, w) for f, s in zip(folders, statuses)
                for w in [crossword_subtitle_words(f)] if w]
        for i in range(len(subs)):
            for j in range(i + 1, len(subs)):
                a, wa = subs[i]
                b, wb = subs[j]
                if len(wa & wb) / min(len(wa), len(wb)) >= 0.5:
                    for g in (a, b):
                        g["holds"].append(
                            f"lint: crossword subtitle near-duplicate across batch "
                            f"({a['slug']}, {b['slug']}) — same construction on two "
                            f"pieces kills the one-of-one claim")
                        g["shippable"] = False

    print("\n" + "=" * 70)
    print("SHIP CHECK — print-readiness gate")
    print("=" * 70)
    held = 0
    for s in statuses:
        mark = "n/a " if s["shippable"] is None else ("SHIP" if s["shippable"] else "HOLD")
        if s["shippable"] is False:
            held += 1
        verds = [v for v in (f"P6 {s['p6']}" if s["p6"] else "",
                             f"6B {s['p6b']}" if s["p6b"] else "",
                             f"pkg {s['pkg']}" if s.get("pkg") else "") if v]
        tail = ("  [" + ", ".join(verds) + "]") if verds else ""
        print(f"  {mark}  {s['slug']}{tail}")
        for h in s["holds"]:
            print(f"        HOLD: {h}")
        for w in s["warns"]:
            print(f"        warn: {w}")
    print("-" * 70)
    print(f"{sum(1 for s in statuses if s['shippable'] is True)} ship, {held} held, "
          f"{sum(1 for s in statuses if s['shippable'] is None)} n/a, {len(statuses)} checked.")
    if held:
        print("\nHeld pieces are NOT print-ready. Re-run `qc` against the delivered PNG "
              "(and fix any FAIL/BIN) before staging them for the print supplier.")
        sys.exit(1)
    print("\nAll checked pieces are print-ready.")


# --- birch-csv: shipping manifest for the print supplier ---------------------
# Emits the CSV Birch ships from: one row per piece with a sequential code,
# recipient, company, the confirmed delivery address, optional notes, and a
# file_stem naming the print file. Columns match the format Birch already uses:
#   code,recipient,company,delivery_address,notes,file_stem
#
# PRIVACY: this file contains delivery addresses. It is written next to the
# manifest (research/ is gitignored) and must NEVER be committed or pushed to the
# deliverables branch. PNGs go to GitHub; addresses go straight to Birch.
#
# Addresses come from the structured DELIVERY block the research agent (Prompt 1)
# emits at the end of each research.md — DELIVERY_STATUS / DELIVERY_ADDRESS /
# DELIVERY_NOTES — never scraped from prose. Status drives behaviour: CONFIRMED
# uses the address; CONFIRM_FIRST keeps it but flags "confirm before shipping";
# BLOCKED leaves it blank. A manifest entry may carry a "delivery" block to
# OVERRIDE the research (a human correction). Any of status/address/notes may
# be overridden independently; omitted fields fall back to the research. The
# manifest is committed, so put addresses here only when correcting one (the
# usual place for addresses is the gitignored research file):
#   {"name": "...", "company": "...", "format": "...",
#    "delivery": {"status": "CONFIRMED", "address": "", "notes": ""}}

_HONOURS = {"CBE", "OBE", "MBE", "KBE", "DBE", "PHD", "QC", "KC", "FRSA",
            "JR", "SR", "II", "III"}


def _surname(full_name):
    """Last name token for the file stem, dropping trailing honours/suffixes."""
    toks = [t for t in re.split(r"\s+", full_name.strip()) if t]
    while len(toks) > 1 and toks[-1].strip(".,").upper() in _HONOURS:
        toks.pop()
    last = toks[-1] if toks else full_name
    return re.sub(r"[^A-Za-z0-9]", "", last)


def _company_condensed(company):
    """Company with spaces and punctuation removed, original casing kept."""
    return re.sub(r"[^A-Za-z0-9]", "", company)


def _delivery_from_research(slug):
    """Read the structured DELIVERY block from a piece's research.md. Returns
    {status, address, notes} (empty strings when absent)."""
    path = os.path.join(PIECES_DIR, slug, "research.md")
    if not os.path.exists(path):
        return {"status": "", "address": "", "notes": ""}
    text = read_file(path)

    def grab(key):
        # [ \t] (not \s) after the colon so an empty value does not let the match
        # run onto the next DELIVERY_ line.
        m = re.search(rf"^[ \t>*-]*{key}[ \t]*:[ \t]*(.*)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    address = grab("DELIVERY_ADDRESS")
    if address.startswith("<"):          # unfilled template placeholder
        address = ""
    return {"status": grab("DELIVERY_STATUS").upper(),
            "address": address, "notes": grab("DELIVERY_NOTES")}


def cmd_birch_csv(args):
    entries, mdir = _load_manifest(args.manifest)
    rows, missing, flags = [], [], []
    for i, entry in enumerate(entries):
        slug = _piece_slug(entry)
        meta_path = os.path.join(PIECES_DIR, slug, "meta.json")
        if os.path.exists(meta_path):
            meta = json.loads(read_file(meta_path))
            recipient = meta.get("recipient_name", entry["name"])
            company = meta.get("recipient_company", entry["company"])
        else:
            recipient, company = entry["name"], entry["company"]
        code = f"{args.prefix}-{i + args.start:02d}"

        # Source of truth: the DELIVERY block in research.md. A manifest "delivery"
        # block overrides it (a human correction).
        res = _delivery_from_research(slug)
        override = entry.get("delivery") or {}
        # Status may also be overridden (e.g. a switchboard call upgrades
        # CONFIRM_FIRST to CONFIRMED, or a remote worker downgrades to BLOCKED).
        # The manifest is committed, so overrides carry status/notes only;
        # addresses stay in the gitignored research files unless corrected.
        # Presence-checked so an override can deliberately BLANK a field, not
        # just replace it (`{"address": ""}` must clear, not fall through).
        status = str(override["status"] if "status" in override
                     else res.get("status", "") or "").upper()
        address = str(override["address"] if "address" in override
                      else res.get("address", "") or "").strip()
        notes = str(override["notes"] if "notes" in override
                    else res.get("notes", "") or "").strip()
        flagged = False
        if status == "BLOCKED":
            address = ""
            notes = "BLOCKED" + (f" — {notes}" if notes else " — no shippable address")
            flags.append(f"{slug}: BLOCKED")
            flagged = True
        elif status == "CONFIRM_FIRST":
            notes = ("CONFIRM ADDRESS BEFORE SHIPPING" + (f" — {notes}" if notes else "")).strip()
            flags.append(f"{slug}: CONFIRM_FIRST")
            flagged = True
        elif status != "CONFIRMED" and address:
            # Unknown or missing status with an address present must never ship
            # blind: the fail-safe only bit on the two exact spellings before.
            label = "missing" if not status else f"unrecognised: {status}"
            notes = (f"CONFIRM ADDRESS BEFORE SHIPPING — DELIVERY_STATUS {label}"
                     + (f" — {notes}" if notes else ""))
            flags.append(f"{slug}: status {label} — treated as CONFIRM_FIRST")
            flagged = True
        if not address and not flagged:
            missing.append(slug)
        file_stem = f"{code}_{_surname(recipient)}_{_company_condensed(company)}"
        rows.append({"code": code, "recipient": recipient, "company": company,
                     "delivery_address": address, "notes": notes,
                     "file_stem": file_stem, "slug": slug})

    out = args.output or os.path.join(
        mdir, os.path.splitext(os.path.basename(args.manifest))[0] + "-birch.csv")
    def cell(v):
        # Excel executes a leading =+-@ as a formula; a recipient-submitted
        # address must never run code in the supplier's spreadsheet.
        return "'" + v if str(v)[:1] in ("=", "+", "-", "@") else v

    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "recipient", "company", "delivery_address", "notes", "file_stem"])
        for r in rows:
            w.writerow([r["code"], cell(r["recipient"]), cell(r["company"]),
                        cell(r["delivery_address"]), cell(r["notes"]), r["file_stem"]])

    staged = cards_staged = 0
    if args.stage_pngs:
        outdir = os.path.join(os.path.dirname(out) or ".",
                              "birch-" + os.path.splitext(os.path.basename(out))[0])
        os.makedirs(outdir, exist_ok=True)
        for r in rows:
            src = os.path.join(PIECES_DIR, r["slug"], r["slug"] + ".png")
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(outdir, r["file_stem"] + ".png"))
                staged += 1
            csrc = os.path.join(PIECES_DIR, r["slug"], r["slug"] + "-card.png")
            if os.path.exists(csrc):
                shutil.copy2(csrc, os.path.join(outdir, r["file_stem"] + "-card.png"))
                cards_staged += 1

    print("\n" + "=" * 70)
    print("BIRCH SHIPPING CSV")
    print("=" * 70)
    print(f"  {os.path.relpath(out, REPO_ROOT)}  ({len(rows)} rows)")
    if args.stage_pngs:
        print(f"  staged {staged} artefact + {cards_staged} card PNG(s) renamed to "
              "<file_stem>.png / <file_stem>-card.png beside it")
    if flags:
        print("\n  STATUS FLAGS from research (do not ship blind): " + "; ".join(flags))
    if missing:
        print(f"\n  {len(missing)} piece(s) with NO delivery address (left blank): "
              + ", ".join(missing))
        print("  Their research.md has no usable DELIVERY block. Add DELIVERY_STATUS/"
              "DELIVERY_ADDRESS/DELIVERY_NOTES to the research, or a manifest \"delivery\" override.")
    print("\n  PRIVACY: this CSV holds delivery addresses. Do NOT commit it or push it "
          "to the deliverables branch. Send it to Birch directly.")


# --- Durability: snapshot renders to the deliverables branch -----------------
# runner/pieces/ is gitignored (it holds personal data) and therefore does NOT
# survive a container restart: a fresh clone restores tracked files only. The
# expensive, hard-to-regenerate outputs are the render + companion-card PNGs,
# and the `deliverables` branch already exists as their durable, address-free
# home. So the instant a piece is built we push its PNGs there; `restore` pulls
# them back after a restart. This makes the loss that hit batch-2026-07-09
# structurally impossible: built renders live in git within seconds, not only
# in an ephemeral folder.

def _batch_label(manifest_path):
    """Derive a stable batch label (YYYY-MM-DD when present) from a manifest
    filename, e.g. research/batch-2026-07-09.json -> 2026-07-09."""
    base = os.path.splitext(os.path.basename(manifest_path))[0]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", base)
    return m.group(1) if m else base


def _piece_pngs(slug):
    """The render + companion-card PNGs for a slug that exist on disk, as
    (src_abspath, dest_filename) pairs."""
    items = []
    for suffix in ("", "-card"):
        p = os.path.join(PIECES_DIR, slug, slug + suffix + ".png")
        if os.path.exists(p):
            items.append((p, slug + suffix + ".png"))
    return items


def _git(a, cwd, check=True, binary=False):
    r = subprocess.run(["git"] + a, cwd=cwd,
                       capture_output=True, text=(not binary))
    if check and r.returncode != 0:
        raise RuntimeError("git " + " ".join(a) + ": "
                           + ((r.stderr or r.stdout) if not binary else "binary"))
    return r


def _deliverables_push(cwd, wt_branch, attempts=4):
    """Push the worktree branch to origin/deliverables, rebasing onto any
    concurrent snapshot and retrying network errors with backoff."""
    delay = 2
    for i in range(attempts):
        r = subprocess.run(["git", "push", "origin", wt_branch + ":deliverables"],
                           cwd=cwd, capture_output=True, text=True)
        if r.returncode == 0:
            return True
        err = (r.stderr or "") + (r.stdout or "")
        if "non-fast-forward" in err or "fetch first" in err or "rejected" in err:
            subprocess.run(["git", "fetch", "origin", "deliverables"], cwd=cwd,
                           capture_output=True, text=True)
            subprocess.run(["git", "rebase", "origin/deliverables"], cwd=cwd,
                           capture_output=True, text=True)
            continue
        if i < attempts - 1:
            time.sleep(delay)
            delay *= 2
    return False


def _deliverables_snapshot(items, batch_label, push=True):
    """Stage (src, dest_filename) PNGs under batch-<label>/ on the durable
    `deliverables` branch through a temporary worktree. Idempotent: a byte
    identical file already on the branch is skipped. Pushes file by file so a
    large batch never builds a >100MB pack (the git proxy rejects those with
    413), matching the documented manual process."""
    items = [(s, d) for (s, d) in items if os.path.exists(s)]
    if not items:
        return {"staged": 0, "skipped": 0, "pushed": 0, "failed": 0, "empty": True}
    _git(["fetch", "origin", "deliverables"], REPO_ROOT, check=False)
    have_remote = _git(["rev-parse", "--verify", "origin/deliverables"],
                       REPO_ROOT, check=False).returncode == 0
    wt = tempfile.mkdtemp(prefix="sentrada-deliv-")
    br = "deliverables-snapshot-wt"
    _git(["worktree", "prune"], REPO_ROOT, check=False)
    _git(["worktree", "remove", "-f", wt], REPO_ROOT, check=False)
    _git(["branch", "-D", br], REPO_ROOT, check=False)
    staged = skipped = pushed = failed = 0
    try:
        if have_remote:
            _git(["worktree", "add", "-f", "-B", br, wt, "origin/deliverables"], REPO_ROOT)
        else:
            _git(["worktree", "add", "-f", "--orphan", "-b", br, wt], REPO_ROOT)
        _git(["config", "user.email", "noreply@anthropic.com"], wt, check=False)
        _git(["config", "user.name", "Claude"], wt, check=False)
        for src, dest in items:
            rel = os.path.join("batch-" + batch_label, dest)
            dst = os.path.join(wt, rel)
            if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            _git(["add", rel], wt)
            _git(["commit", "-m", "snapshot " + rel], wt)
            staged += 1
            if push:
                if _deliverables_push(wt, br):
                    pushed += 1
                else:
                    failed += 1
        return {"staged": staged, "skipped": skipped, "pushed": pushed,
                "failed": failed}
    finally:
        _git(["worktree", "remove", "-f", wt], REPO_ROOT, check=False)
        _git(["branch", "-D", br], REPO_ROOT, check=False)


def _auto_snapshot(slugs, label):
    """Best-effort durability hook wired into batch-build. Warns on failure,
    never raises: a snapshot problem must not fail a build that otherwise
    succeeded, but the operator must know the renders are not yet durable."""
    items = []
    for slug in slugs:
        items += _piece_pngs(slug)
    if not items:
        return
    try:
        res = _deliverables_snapshot(items, label, push=True)
        if res.get("failed"):
            print(f"\n[durable] WARNING: {res['failed']} PNG(s) committed but NOT "
                  f"pushed to deliverables (network?). Re-run `snapshot --manifest "
                  "<manifest>` before relying on them.")
        else:
            print(f"\n[durable] {res['staged']} PNG(s) pushed to the deliverables "
                  f"branch under batch-{label}/ ({res['skipped']} already current). "
                  "A restart can no longer lose these renders.")
    except Exception as e:
        print(f"\n[durable] WARNING: snapshot to deliverables failed ({e}). Renders "
              "are NOT yet durable; run `python runner/sentrada_runner.py snapshot "
              f"--manifest <manifest>` manually.")


def cmd_snapshot(args):
    if args.folder:
        slugs = [os.path.basename(os.path.normpath(args.folder))]
        label = args.batch_label
        if not label:
            die("snapshot --folder needs --batch-label (e.g. 2026-07-09)")
    else:
        entries, _ = _load_manifest(args.manifest)
        slugs = [_piece_slug(e) for e in entries]
        label = args.batch_label or _batch_label(args.manifest)
    items = []
    for slug in slugs:
        items += _piece_pngs(slug)
    if not items:
        die("no render/card PNGs found on disk for these pieces (nothing to snapshot)")
    res = _deliverables_snapshot(items, label, push=not args.no_push)
    print("\n" + "=" * 70)
    print("DELIVERABLES SNAPSHOT")
    print("=" * 70)
    print(f"  batch-{label}/  on the deliverables branch")
    print(f"  {res['staged']} pushed, {res['skipped']} already current"
          + (f", {res['failed']} FAILED to push" if res.get("failed") else ""))
    if res.get("failed"):
        die("some pushes failed; renders are not fully durable")


def cmd_restore(args):
    _git(["fetch", "origin", "deliverables"], REPO_ROOT, check=False)
    if _git(["rev-parse", "--verify", "origin/deliverables"],
            REPO_ROOT, check=False).returncode != 0:
        if getattr(args, "all", False):
            return  # nothing to restore yet; a clean first session is fine
        die("no deliverables branch on origin; nothing to restore")
    if getattr(args, "all", False):
        listing = _git(["ls-tree", "-r", "--name-only", "origin/deliverables"],
                       REPO_ROOT, check=False)
        prefixes = sorted({f.split("/", 1)[0] for f in listing.stdout.splitlines()
                           if f.startswith("batch-") and "/" in f})
        if not prefixes:
            return
        total = sum(_restore_batch(p[len("batch-"):]) for p in prefixes)
        if total:
            print(f"[restore] pulled {total} render/card PNG(s) across "
                  f"{len(prefixes)} batch(es) back into runner/pieces/.")
        return
    label = args.batch_label or (_batch_label(args.manifest) if args.manifest else None)
    if not label:
        die("restore needs --batch-label (e.g. 2026-07-09), --manifest, or --all")
    n = _restore_batch(label)
    if not n:
        die(f"no snapshot found on deliverables for batch-{label}")
    print(f"[restore] pulled {n} PNG(s) for batch-{label} back into "
          "runner/pieces/. NOTE: this restores the renders + cards (the "
          "durable, expensive outputs) only. data.json, research and the QC "
          "artefacts are rebuilt from research if you need to re-run the chain.")


def _restore_batch(label):
    """Copy every render/card PNG under batch-<label>/ on origin/deliverables
    back into runner/pieces/<slug>/. Returns the count restored."""
    listing = _git(["ls-tree", "-r", "--name-only", "origin/deliverables",
                    "batch-" + label + "/"], REPO_ROOT, check=False)
    files = [f for f in listing.stdout.splitlines() if f.endswith(".png")]
    restored = 0
    for f in files:
        name = os.path.basename(f)
        slug = name[:-9] if name.endswith("-card.png") else name[:-4]
        dest_dir = os.path.join(PIECES_DIR, slug)
        os.makedirs(dest_dir, exist_ok=True)
        blob = _git(["show", "origin/deliverables:" + f], REPO_ROOT,
                    check=False, binary=True)
        if blob.returncode != 0:
            continue
        with open(os.path.join(dest_dir, name), "wb") as fh:
            fh.write(blob.stdout)
        restored += 1
    return restored


def cmd_followup(args):
    _usage_reset()
    config = load_config(args.config)
    folder = args.folder
    reply = _p7(config, folder, args.delivery_date)
    # Mirror the chain's conversion tail: render the card (skipped for
    # custom-card senders) and, when one exists, refresh the package pass so
    # the regenerated card copy never ships judged only in its previous form.
    # --no-card protects ALREADY-SHIPPED pieces: their card is physically in
    # the box, so regenerating touches must not overwrite the record of it.
    card_png = None if getattr(args, "no_card", False) \
        else _generate_card(config, folder, reply)
    vpkg = None
    if card_png:
        slug = os.path.basename(os.path.normpath(folder))
        image = os.path.join(folder, slug + ".png")
        if os.path.exists(image):
            meta = json.loads(read_file(os.path.join(folder, "meta.json")))
            research = read_file(os.path.join(folder, "research.md"))
            _, vpkg = _p6b(config, folder, image, meta, research, package=True)
    print("\n" + "=" * 70)
    print("FOLLOW-UP WRITTEN")
    print("=" * 70)
    print("  followup.md  <- companion card + Touch 1-3 + reception nudge + fact check")
    if vpkg:
        print(f"  qc_package.md <- 6B package pass; verdict: {vpkg}")
    print("\n" + reply)
    _print_usage("followup")


# --- Outcomes and calibration -----------------------------------------------
# The chain predicts (6B) and then forgets. These two commands close the loop:
# `outcome` records what actually happened to a sent piece; `calibration`
# tabulates 6B's predictions against reality. Once ~30 outcomes exist, the misses
# become calibration examples for the 6B prompt.
#
# Outcomes live in a COMMITTED ledger (runner/outcomes.json), not in the
# gitignored pieces folders: remote containers are ephemeral, and outcomes
# accumulate over weeks. The 6B verdict and piece context are captured into the
# ledger at record time, so calibration works in a fresh container with no
# pieces folders at all. Commit the ledger after recording.

OUTCOME_CHOICES = ("replied", "meeting", "opportunity", "declined", "no_response",
                   "bounced")  # declined = a reply, but a "no thank you"
OUTCOMES_PATH = os.path.join(RUNNER_DIR, "outcomes.json")

# Doctrine-cut fields on every ledger record. reply_language uses P7b's
# classification vocabulary (gift / problem / mixed / none). angle_type and
# source_signal are free strings, but the monthly review cuts by exact value,
# so keep to the starter vocabularies unless a new value earns its place:
#   angle_type:    problem-tension | achievement-gap | open-loop | other
#   source_signal: live-need-signal | job-change | funding-or-announcement |
#                  referral-or-community | cold-list | other
REPLY_LANGUAGES = ("gift", "problem", "mixed", "none")


def cmd_outcome(args):
    ledger = json.loads(read_file(OUTCOMES_PATH)) if os.path.exists(OUTCOMES_PATH) else {}
    rec = ledger.get(args.slug, {})
    # Capture piece context while the folder still exists.
    folder = os.path.join(PIECES_DIR, args.slug)
    meta_path = os.path.join(folder, "meta.json")
    if os.path.exists(meta_path):
        meta = json.loads(read_file(meta_path))
        rec.setdefault("recipient", meta.get("recipient_name", ""))
        rec.setdefault("company", meta.get("recipient_company", ""))
        rec.setdefault("format", meta.get("format", ""))
        # The P7b reply handler stamps these into meta.json when the first
        # reply is processed; harvest rather than re-asking.
        for key in ("first_reply_date", "reply_language"):
            if meta.get(key):
                rec.setdefault(key, meta[key])
        # The sourcing pipeline stamps the signals that surfaced this
        # recipient into meta.json; harvest them so response rate per signal
        # source is computable from the ledger alone.
        if meta.get("source_signals"):
            rec.setdefault("source_signals", meta["source_signals"])
            # Tags (champion-moved, successor-seat...) fold into the type
            # string so monthly-review's source_signal cut separates them
            # from plain in-seat signals automatically.
            types = set()
            for s in meta["source_signals"]:
                if not s.get("type"):
                    continue
                t = s["type"]
                if s.get("tags"):
                    t += "+" + "+".join(sorted(s["tags"]))
                types.add(t)
            if types:
                rec.setdefault("source_signal", ", ".join(sorted(types)))
    sixb_path = os.path.join(folder, "qc_recipient.md")
    if os.path.exists(sixb_path):
        v = extract_6b_verdict(read_file(sixb_path))
        if v:
            rec["verdict_6b"] = v
    # The package pass saw the piece plus the card, i.e. what was actually sent,
    # so calibration prefers it over the piece-only verdict when both exist.
    pkg_path = os.path.join(folder, "qc_package.md")
    if os.path.exists(pkg_path):
        v = extract_6b_verdict(read_file(pkg_path))
        if v:
            rec["verdict_package"] = v
    if args.result:
        rec["result"] = args.result
    elif not rec.get("result"):
        rec["result"] = "no_response"  # the default until a reply changes it
    if args.date:
        rec["date"] = args.date
    if args.company:
        rec["company"] = args.company
    if args.notes:
        rec["notes"] = args.notes
    for key, val in (("format", args.format), ("angle_type", args.angle),
                     ("source_signal", args.signal),
                     ("delivered_date", args.delivered),
                     ("first_reply_date", args.first_reply),
                     ("reply_language", args.reply_language)):
        if val:
            rec[key] = val
    ledger[args.slug] = rec
    write_file(OUTCOMES_PATH, json.dumps(ledger, indent=2))
    predicted = rec.get("verdict_package") or rec.get("verdict_6b")
    print(f"[outcome] {args.slug}: {rec['result']}"
          + (f"  (6B predicted: {predicted})" if predicted else ""))
    missing = [k for k in ("format", "angle_type", "source_signal",
                           "delivered_date") if not rec.get(k)]
    if missing:
        print("[outcome] doctrine fields still unset: " + ", ".join(missing)
              + " (set them with --format/--angle/--signal/--delivered)")
    print(f"[outcome] ledger updated: {os.path.relpath(OUTCOMES_PATH, REPO_ROOT)} "
          "— commit and push it so the record survives this container")


# --- monthly-review: the ledger becomes doctrine proposals -------------------

REVIEWS_DIR = os.path.join(RUNNER_DIR, "reviews")
REVIEW_CUTS = (("format", "format"), ("angle type", "angle_type"),
               ("source signal", "source_signal"),
               ("reply language", "reply_language"))
DIRECTIONAL_N = 20  # below this, a cut's numbers are directional, not doctrine


def _review_cut_table(ledger, key):
    positive = {"replied", "meeting", "opportunity"}
    cut = {}
    for rec in ledger.values():
        t = cut.setdefault(str(rec.get(key) or "(unset)"),
                           {"sent": 0, "responded": 0, "positive": 0})
        t["sent"] += 1
        if rec.get("result") not in ("no_response", "bounced", None):
            t["responded"] += 1
        if rec.get("result") in positive:
            t["positive"] += 1
    lines = ["| value | sent | responded | positive | basis |",
             "| --- | --- | --- | --- | --- |"]
    for val, t in sorted(cut.items()):
        basis = "DIRECTIONAL" if t["sent"] < DIRECTIONAL_N else "doctrine-grade"
        lines.append(f"| {val} | {t['sent']} | {t['responded']} "
                     f"({100 * t['responded'] // t['sent']}%) | {t['positive']} "
                     f"({100 * t['positive'] // t['sent']}%) | {basis} |")
    return "\n".join(lines)


def cmd_monthly_review(args):
    _usage_reset()
    config = load_config(args.config)
    ledger = json.loads(read_file(OUTCOMES_PATH)) if os.path.exists(OUTCOMES_PATH) else {}
    if not ledger:
        die("no outcomes recorded yet (runner/outcomes.json is empty). Record "
            "some with the `outcome` command first.")
    month = args.month or time.strftime("%Y-%m")

    doc = [f"# Sentrada monthly review — {month}", "",
           f"All {len(ledger)} piece records in the ledger, compiled "
           f"{time.strftime('%Y-%m-%d')}. Cuts with fewer than {DIRECTIONAL_N} "
           "pieces are labelled DIRECTIONAL: read them as hints, never as "
           "doctrine.", ""]
    for label, key in REVIEW_CUTS:
        doc += [f"## By {label}", "", _review_cut_table(ledger, key), ""]
    doc += ["## Every piece", ""]
    for slug, rec in sorted(ledger.items()):
        bits = [rec.get("format") or "?", rec.get("angle_type") or "angle unset",
                rec.get("source_signal") or "signal unset",
                "delivered " + (rec.get("delivered_date") or "?"),
                "first reply " + (rec.get("first_reply_date") or "-"),
                "language " + (rec.get("reply_language") or "-"),
                rec.get("result", "?")]
        doc.append(f"- **{slug}** — " + " | ".join(bits))
        if rec.get("notes"):
            doc.append(f"  - {rec['notes']}")
    compiled = "\n".join(doc)

    print(f"[monthly-review] compiling {len(ledger)} piece record(s); asking "
          f"for doctrine observations ({model_for(config, 'p2')})...")
    observations = cli_text(fill(load_template("monthly_review.md"),
                                 {"month": month, "compiled": compiled,
                                  "directional_n": DIRECTIONAL_N}),
                            model_for(config, "p2"))
    compiled += ("\n\n## Doctrine observations (proposals only — nothing "
                 "changes automatically)\n\n" + observations + "\n")

    os.makedirs(REVIEWS_DIR, exist_ok=True)
    out_path = os.path.join(REVIEWS_DIR, f"{month}.md")
    write_file(out_path, compiled)
    print(f"\n[monthly-review] written to {os.path.relpath(out_path, REPO_ROOT)}")
    print("Nothing has been changed anywhere: every observation is a proposal "
          "for you to act on or ignore.")
    _print_usage("monthly-review")


def cmd_calibration(args):
    positive = {"replied", "meeting", "opportunity"}
    ledger = json.loads(read_file(OUTCOMES_PATH)) if os.path.exists(OUTCOMES_PATH) else {}
    # Prefer the package verdict (piece + card, what was actually sent) over the
    # piece-only one wherever both exist; older records carry only verdict_6b.
    rows = [(slug, rec.get("verdict_package") or rec.get("verdict_6b", "?"),
             rec.get("result", "?"))
            for slug, rec in sorted(ledger.items()) if rec.get("result")]
    pending = []
    if os.path.isdir(PIECES_DIR):
        for d in sorted(os.listdir(PIECES_DIR)):
            if d.startswith("_") or d in ledger:
                continue
            pkg_path = os.path.join(PIECES_DIR, d, "qc_package.md")
            sixb_path = os.path.join(PIECES_DIR, d, "qc_recipient.md")
            if os.path.exists(pkg_path):
                pending.append((d, extract_6b_verdict(read_file(pkg_path)) or "?"))
            elif os.path.exists(sixb_path):
                pending.append((d, extract_6b_verdict(read_file(sixb_path)) or "?"))

    print("\n" + "=" * 70)
    print("6B CALIBRATION — prediction vs outcome")
    print("=" * 70)
    if not rows:
        print("No outcomes recorded yet. Record one with:")
        print("  python runner/sentrada_runner.py outcome --slug <slug> "
              "--result replied|meeting|opportunity|no_response|bounced")
    for d, v, res in rows:
        if "WOULD" not in v:      # no surviving prediction: outcome-only record
            hit = "·"
        else:
            hit = "✓" if ((res in positive) == ("BIN" not in v and "IGNORE" not in v)) else "✗"
        print(f"  {hit}  {d:38} 6B: {v:38} actual: {res}")
    if rows:
        got = sum(1 for _, _, res in rows if res in positive)
        responded = sum(1 for _, _, res in rows if res not in ("no_response", "bounced"))
        print("-" * 70)
        print(f"  {len(rows)} outcome(s) recorded: {responded} responded "
              f"({100 * responded // len(rows)}% response rate), {got} positive "
              f"({100 * got // len(rows)}%)")
        # Running response rate per format. Same convention as the campaign's
        # own reporting: denominator is everything sent (bounces included),
        # responded is any reply including declines, positive excludes them.
        by_fmt = {}
        for slug, rec in sorted(ledger.items()):
            if not rec.get("result"):
                continue
            t = by_fmt.setdefault(rec.get("format") or "(unknown)",
                                  {"sent": 0, "responded": 0, "positive": 0})
            t["sent"] += 1
            if rec["result"] not in ("no_response", "bounced"):
                t["responded"] += 1
            if rec["result"] in positive:
                t["positive"] += 1
        print("\n  Response rate by format:")
        for f, t in sorted(by_fmt.items()):
            print(f"     {f:12} {t['responded']}/{t['sent']} responded "
                  f"({100 * t['responded'] // t['sent']}%), "
                  f"{t['positive']} positive ({100 * t['positive'] // t['sent']}%)")
        print("  When ~30 outcomes exist, feed the misses back into prompt6b as "
              "calibration examples.")
    if pending:
        print(f"\n  {len(pending)} piece(s) with a 6B prediction and no outcome yet:")
        for d, v in pending:
            print(f"     {d:38} 6B: {v}")


# --- Address capture (sentrada.io/for/<token>) ------------------------------
# The tease flow for remote-likely targets: a piece is printed, the recipient
# gets a one-off unguessable link, the page collects a delivery address, the
# runner pulls it when staging the shipment and deletes it on delivery.
#
# Split of responsibilities, chosen so the page's privacy promise ("Used once,
# for this delivery. Deleted once it's signed for.") is genuinely enforceable:
#   - The address lives ONLY in the capture store (the site's backend) between
#     submission and delivery. `address` prints it to the terminal for the
#     Birch CSV / manifest override; it is never written to a committed file.
#   - This ledger (runner/capture.json, COMMITTED like outcomes.json so it
#     survives ephemeral containers) holds everything EXCEPT the address and
#     the raw token: a sha256 of the token, dates, channel, variant, statuses.
#   - `delivered` purges the store record entirely; the ledger keeps only the
#     address type and the fact one was provided.
#
# Statuses: printed -> tease-sent -> address-received; swapped-after-silence
# when the sender decides to swap the recipient. `capture` computes the two
# advisory flags (never acted on automatically): NUDGE-DUE 5 days after the
# tease with nothing back, SWAP-RECOMMENDED 7 days after the nudge.
#
# Auth to the backend is a bearer secret in SENTRADA_RUNNER_SECRET (env or
# .env; never committed). The API base is config "capture_api", overridable
# with SENTRADA_CAPTURE_API (used by the local validation harness).

# SENTRADA_CAPTURE_LEDGER override exists for capture-probe, which must never
# touch the real committed ledger.
CAPTURE_PATH = (os.environ.get("SENTRADA_CAPTURE_LEDGER")
                or os.path.join(RUNNER_DIR, "capture.json"))
CAPTURE_TTL_DAYS = 30
NUDGE_DUE_DAYS = 5
SWAP_RECOMMEND_DAYS = 7
TEASE_CHANNELS = ("linkedin", "email")
TEASE_VARIANTS = ("mystery", "teaser")


def _capture_ledger():
    return json.loads(read_file(CAPTURE_PATH)) if os.path.exists(CAPTURE_PATH) else {}


def _capture_save(ledger):
    write_file(CAPTURE_PATH, json.dumps(ledger, indent=2))
    print(f"[capture] ledger updated: {os.path.relpath(CAPTURE_PATH, REPO_ROOT)} "
          "— commit and push it so the record survives this container")


def _capture_api_base(config):
    base = os.environ.get("SENTRADA_CAPTURE_API") or config.get("capture_api", "")
    if not base:
        die("no capture API configured: set \"capture_api\" in config.json "
            "(the deployed site base, e.g. https://sentrada.io)")
    return base.rstrip("/")


def _capture_secret(required=True):
    # Environment first, then a plain SENTRADA_RUNNER_SECRET=... line in .env
    # (gitignored). The secret must match RUNNER_SECRET on the deployment.
    val = os.environ.get("SENTRADA_RUNNER_SECRET", "")
    env_path = os.path.join(REPO_ROOT, ".env")
    if not val and os.path.exists(env_path):
        for line in read_file(env_path).splitlines():
            if line.strip().startswith("SENTRADA_RUNNER_SECRET="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not val and required:
        die("SENTRADA_RUNNER_SECRET is not set (env or .env). It must match "
            "the RUNNER_SECRET configured on the site deployment.")
    return val


def _capture_call(config, payload):
    req = urllib.request.Request(
        _capture_api_base(config) + "/api/runner",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_capture_secret()}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            die(f"capture API {payload.get('action')} returned a non-JSON reply "
                f"(is the base URL a Vercel deployment, not a proxy/login page?): "
                f"{raw[:200]}")
    except urllib.error.HTTPError as e:
        die(f"capture API {payload.get('action')} failed ({e.code}): "
            f"{e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        die(f"capture API unreachable at {_capture_api_base(config)}: {e.reason}")


def _capture_today():
    return datetime.date.today().isoformat()


def _days_since(iso_date):
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(iso_date[:10])).days
    except ValueError:
        return None


def _capture_entry(ledger, slug):
    if slug not in ledger:
        die(f"{slug} is not in the capture ledger. Mark it printed first:\n"
            f"  python runner/sentrada_runner.py printed --piece {slug}")
    return ledger[slug]


def cmd_printed(args):
    ledger = _capture_ledger()
    entry = ledger.get(args.piece, {})
    name = args.name
    meta_path = os.path.join(PIECES_DIR, args.piece, "meta.json")
    if not name and os.path.exists(meta_path):
        name = json.loads(read_file(meta_path)).get("recipient_name", "")
    if not name:
        name = entry.get("recipient", "") or entry.get("first_name", "")
    if not name:
        die("no recipient name found (no piece folder in this container): "
            "pass --name \"First Last\"")
    # Ledger contract: first name only. The committed file's documented contents
    # are enumerated (token hash, first name, dates, statuses); the full name
    # lives in meta.json/research, not here.
    entry.pop("recipient", None)
    entry.update({"first_name": name.split()[0], "printed": _capture_today()})
    entry.setdefault("status", "printed")
    ledger[args.piece] = entry
    _capture_save(ledger)
    print(f"[capture] {args.piece}: marked printed ({name}). "
          f"Tease it with:\n  python runner/sentrada_runner.py tease --piece {args.piece} "
          f"--channel linkedin --variant mystery")


def cmd_tease(args):
    config = load_config(args.config)
    ledger = _capture_ledger()
    entry = _capture_entry(ledger, args.piece)
    # The tease claims one copy exists on a desk-ready board. That must be true
    # before anything sends, so an unprinted piece refuses here.
    if not entry.get("printed"):
        die(f"{args.piece} is not marked printed. The tease says one copy exists; "
            f"print it first, then:\n"
            f"  python runner/sentrada_runner.py printed --piece {args.piece}")
    if entry.get("status") == "address-received":
        die(f"{args.piece} already has an address on file. Pull it (`address`) "
            f"and mark it `delivered` (which deletes it) before re-teasing; "
            f"a new token would orphan the submitted address.")
    if entry.get("status") == "tease-sent" and not args.again:
        die(f"{args.piece} already has a live tease ({entry.get('tease_date')}, "
            f"{entry.get('tease_channel')}). Re-run with --again to rotate the "
            f"token (the old link stops working).")
    token = secrets.token_urlsafe(24)
    result = _capture_call(config, {
        "action": "register", "token": token, "piece_id": args.piece,
        "first_name": entry["first_name"], "ttl_days": CAPTURE_TTL_DAYS})
    link = f"{_capture_api_base(config)}/for/{token}"
    entry.update({
        "status": "tease-sent",
        "tease_date": _capture_today(),
        "tease_channel": args.channel,
        "tease_variant": args.variant,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "token_expires": (datetime.date.today()
                          + datetime.timedelta(days=CAPTURE_TTL_DAYS)).isoformat(),
    })
    entry.pop("nudge_date", None)
    _capture_save(ledger)
    print(f"\n[capture] {args.piece}: tease registered "
          f"({args.channel}, {args.variant} variant"
          + (", previous token invalidated" if result.get("replaced") else "") + ").")
    print(f"[capture] link expires {entry['token_expires']}. Drop this into your message:\n")
    print(f"  {link}\n")
    print("[capture] the raw token is shown once and not stored here; "
          "the ledger keeps only its hash.")


def cmd_teaser(args):
    try:
        from PIL import Image
    except ImportError:
        die("the teaser crop needs Pillow: pip install Pillow")
    image = args.image or os.path.join(PIECES_DIR, args.piece, f"{args.piece}.png")
    if not os.path.exists(image):
        die(f"no render at {image}. Pass --image with the piece's render "
            f"(pieces folders are gitignored and containers are ephemeral).")
    out = args.out or os.path.join(os.path.dirname(image), f"{args.piece}-teaser.jpg")
    img = Image.open(image)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    # A quarter of the page from the chosen corner. On the newspaper and email
    # renders the masthead / name row is centred at the top, so a top corner
    # slices it half visible, which is the point: enough to intrigue, not
    # enough to read.
    cw, ch = w // 2, h // 2
    boxes = {"tl": (0, 0, cw, ch), "tr": (w - cw, 0, w, ch),
             "bl": (0, h - ch, cw, h), "br": (w - cw, h - ch, w, h)}
    crop = img.crop(boxes[args.corner])
    # Small enough to sit inline in an email body or a LinkedIn DM.
    crop.thumbnail((args.width, args.width), Image.LANCZOS)
    crop.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    kb = os.path.getsize(out) // 1024
    print(f"[capture] teaser crop ({args.corner}) written: {out} "
          f"({crop.size[0]}x{crop.size[1]}, {kb} KB)")
    print("[capture] embed it inline at the top of the body, never as an attachment. "
          "If the crop shows too much or too little, try --corner tr/bl/br.")


def cmd_nudge(args):
    ledger = _capture_ledger()
    entry = _capture_entry(ledger, args.piece)
    if entry.get("status") != "tease-sent":
        die(f"{args.piece} is '{entry.get('status')}', not tease-sent; "
            f"the day-5 nudge only applies to a live tease.")
    entry["nudge_date"] = _capture_today()
    _capture_save(ledger)
    reminder = ("include the corner image inline with the nudge"
                if entry.get("tease_variant") == "mystery"
                else "the tease already showed the corner, so the nudge stays text only")
    print(f"[capture] {args.piece}: nudge recorded. Escalation rule: {reminder}.")


def cmd_swap(args):
    config = load_config(args.config)
    ledger = _capture_ledger()
    entry = _capture_entry(ledger, args.piece)
    # Kill the live token BEFORE recording the swap: the token stays valid in
    # the store for up to 30 days, and a post-swap submission would land in a
    # record that nothing ever pulls or purges — an address silently waiting
    # out the 90-day TTL. If the purge call fails, the swap is not recorded.
    result = _capture_call(config, {"action": "purge", "piece_id": args.piece})
    entry["status"] = "swapped-after-silence"
    entry["swap_date"] = _capture_today()
    _capture_save(ledger)
    tok = ("the tease link is dead" if result.get("purged")
           else "no live store record existed")
    print(f"[capture] {args.piece}: marked swapped-after-silence ({tok}). "
          f"The piece is free to re-target; run tease again once re-marked printed.")


def cmd_address(args):
    config = load_config(args.config)
    result = _capture_call(config, {"action": "pull", "piece_id": args.piece})
    record = result.get("record", {})
    if record.get("state") != "submitted":
        print(f"[capture] {args.piece}: no address yet "
              f"(token state: {record.get('state', 'unknown')}).")
        return
    addr = record.get("address", {})
    label = "My office" if record.get("address_type") == "office" else "Somewhere else"
    print(f"\n[capture] {args.piece} — {record.get('first_name')} chose: {label}")
    print(f"[capture] submitted {record.get('submitted_at', '')[:10]}\n")
    for field in ("line1", "line2", "city", "postcode", "country"):
        if addr.get(field):
            print(f"  {addr[field]}")
    print("\n[capture] this address goes into the Birch CSV or a manifest "
          "delivery override ONLY (both gitignored). Never commit it. It is "
          "deleted everywhere by:  delivered --piece " + args.piece)
    print("[capture] PRIVACY: this printed the address to your terminal. If this "
          "session is a logged/cloud transcript, the address now lives in that "
          "log, which `delivered` cannot reach. Run `address` in a local, "
          "unlogged terminal, or pipe it straight into the Birch CSV.")


def cmd_delivered(args):
    config = load_config(args.config)
    ledger = _capture_ledger()
    entry = _capture_entry(ledger, args.piece)
    result = _capture_call(config, {"action": "purge", "piece_id": args.piece})
    entry["delivered"] = _capture_today()
    entry["address_provided"] = bool(result.get("had_address") or
                                     entry.get("status") == "address-received")
    _capture_save(ledger)
    if result.get("purged"):
        print(f"[capture] {args.piece}: delivered. The capture store record is "
              f"deleted (address included); the ledger keeps only the address "
              f"type and that one was provided.")
    else:
        print(f"[capture] {args.piece}: delivered recorded. No store record "
              f"existed (already purged or never teased).")
    print("[capture] if the address was pasted into a Birch CSV or staging "
          "folder, delete those local copies now; they are gitignored but real.")


def cmd_capture(args):
    config = load_config(args.config)
    ledger = _capture_ledger()

    # Sync submissions from the store first, so a missed notification email can
    # never hide an address that arrived. Metadata only; addresses stay remote.
    records = []
    if args.offline:
        pass
    elif not _capture_secret(required=False):
        print("[capture] SENTRADA_RUNNER_SECRET not set; showing the ledger "
              "only (no store poll, submissions may be missing).")
    else:
        records = _capture_call(config, {"action": "list"}).get("records", [])
    if records:
        changed = False
        for rec in records:
            slug = rec.get("piece_id", "")
            entry = ledger.get(slug)
            submitted = rec.get("state") == "submitted"
            # Never skip a submitted address silently: the sync is the fallback
            # for missed notifications, and a dropped one waits out the 90-day
            # TTL unshipped and unpurged.
            if not entry:
                if submitted:
                    print(f"[capture] WARNING: the store holds a SUBMITTED address "
                          f"for '{slug}', which is not in this ledger (teased from "
                          f"another container, or ledger not pulled?). Ship it, or "
                          f"run `delivered --piece {slug}` to purge it.")
                continue
            if entry.get("status") == "swapped-after-silence":
                if submitted:
                    print(f"[capture] WARNING: {slug} was swapped but the store "
                          f"holds a submitted address (token pre-dates the swap "
                          f"purge). Decide whether to ship, then run `delivered "
                          f"--piece {slug}` to purge it.")
                continue
            if submitted and entry.get("status") != "address-received":
                entry["status"] = "address-received"
                entry["submission_date"] = str(rec.get("submitted_at", ""))[:10]
                entry["address_type"] = rec.get("address_type", "")
                changed = True
                print(f"[capture] {slug}: address received "
                      f"({entry['submission_date']}, {entry['address_type']}).")
        if changed:
            _capture_save(ledger)

    if not ledger:
        print("[capture] no pieces in the capture ledger yet. Start with:\n"
              "  python runner/sentrada_runner.py printed --piece <slug>")
        return

    print("\n" + "=" * 78)
    print("ADDRESS CAPTURE — status")
    print("=" * 78)
    flags = []
    for slug, e in sorted(ledger.items()):
        status = e.get("status", "?")
        bits = []
        if e.get("tease_date"):
            bits.append(f"teased {e['tease_date']} ({e.get('tease_channel')}, "
                        f"{e.get('tease_variant')})")
        if e.get("nudge_date"):
            bits.append(f"nudged {e['nudge_date']}")
        if e.get("submission_date"):
            bits.append(f"address {e['submission_date']} ({e.get('address_type')})")
        if e.get("delivered"):
            bits.append(f"delivered {e['delivered']}")
        flag = ""
        if status == "tease-sent":
            since_tease = _days_since(e.get("tease_date", ""))
            since_nudge = _days_since(e.get("nudge_date", "")) if e.get("nudge_date") else None
            if e.get("nudge_date"):
                if since_nudge is not None and since_nudge >= SWAP_RECOMMEND_DAYS:
                    flag = "SWAP-RECOMMENDED"
            elif since_tease is not None and since_tease >= NUDGE_DUE_DAYS:
                flag = "NUDGE-DUE"
            expires = e.get("token_expires", "")
            if expires and expires < _capture_today():
                flag = (flag + ", " if flag else "") + "token expired"
        if flag:
            flags.append((slug, flag))
        print(f"  {slug:38} {status:22} {'; '.join(bits)}"
              + (f"\n  {'':38} >> {flag}" if flag else ""))

    if flags:
        print("-" * 78)
        print("  Flags (advisory; the swap decision is always yours):")
        for slug, flag in flags:
            if "NUDGE-DUE" in flag:
                print(f"    {slug}: day-5 nudge is due. Send it, then record it:\n"
                      f"      python runner/sentrada_runner.py nudge --piece {slug}")
            if "SWAP-RECOMMENDED" in flag:
                print(f"    {slug}: 7 days past the nudge with nothing back. "
                      f"If you decide to swap:\n"
                      f"      python runner/sentrada_runner.py swap --piece {slug}")

    # Share rate, split by variant: the number this pilot exists to learn.
    teased = [e for e in ledger.values() if e.get("tease_date")]
    received = [e for e in teased if e.get("submission_date")]
    print("-" * 78)
    if teased:
        print(f"  Share rate: {len(received)}/{len(teased)} "
              f"({100 * len(received) // len(teased)}%) teases converted to an address")
        for variant in TEASE_VARIANTS:
            vt = [e for e in teased if e.get("tease_variant") == variant]
            vr = [e for e in vt if e.get("submission_date")]
            if vt:
                print(f"    {variant:8} {len(vr)}/{len(vt)} "
                      f"({100 * len(vr) // len(vt)}%)")
    else:
        print("  Share rate: no teases sent yet.")


def cmd_capture_probe(args):
    """Regression-test the address-capture flow end to end, gate-probe style.

    Spawns tools/capture-harness.js (the REAL api/ functions over a mock store
    and a mock Resend), then drives the real runner commands against it and
    asserts the privacy-critical properties: the notification never contains
    the address, a re-tease can never orphan a submitted address, `delivered`
    leaves the store empty, unknown tokens read as expired, the TTL safety net
    is armed, and the rate limit bites. Uses a throwaway ledger; never touches
    runner/capture.json. Run it after any edit to api/ or the capture commands.
    """
    import contextlib
    import io
    import time
    import urllib.parse

    global CAPTURE_PATH
    if not shutil.which("node"):
        die("capture-probe needs node (the harness runs the real api/ functions)")

    app_port, redis_port, resend_port = 18788, 18790, 18791
    base = f"http://127.0.0.1:{app_port}"
    secret = secrets.token_urlsafe(16)
    slug = "probe-fake-recipient"

    harness = subprocess.Popen(
        ["node", os.path.join(REPO_ROOT, "tools", "capture-harness.js")],
        env=dict(os.environ, APP_PORT=str(app_port), REDIS_PORT=str(redis_port),
                 RESEND_PORT=str(resend_port), CAPTURE_HARNESS_SECRET=secret),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def http_json(url, payload=None, headers=None, method=None):
        req = urllib.request.Request(
            url, data=(json.dumps(payload).encode() if payload is not None else None),
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method or ("POST" if payload is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}"), dict(e.headers)

    def run_cmd(fn, expect_halt=False, **kw):
        """Run a runner command in-process, capturing output; returns
        (halted, output)."""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                fn(argparse.Namespace(**kw))
            halted = False
        except SystemExit:
            halted = True
        if halted != expect_halt:
            print(out.getvalue())
        return halted, out.getvalue()

    results = []

    def check(label, ok):
        results.append((label, ok))
        print(("  PASS  " if ok else "  FAIL  ") + label)

    ledger_fd, ledger_path = tempfile.mkstemp(suffix=".json", prefix="capture-probe-")
    os.close(ledger_fd)
    os.unlink(ledger_path)
    saved_path = CAPTURE_PATH
    saved_env = {k: os.environ.get(k) for k in
                 ("SENTRADA_CAPTURE_API", "SENTRADA_RUNNER_SECRET")}
    CAPTURE_PATH = ledger_path
    os.environ["SENTRADA_CAPTURE_API"] = base
    os.environ["SENTRADA_RUNNER_SECRET"] = secret

    print("\n" + "=" * 70)
    print("CAPTURE PROBE — the real api/ functions over a mock store")
    print("=" * 70)
    try:
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            try:
                status, _, _ = http_json(f"{base}/api/token?t=x")
                ready = status == 200
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.3)
        if not ready:
            die("harness did not come up on " + base)

        cfg_path = args.config if os.path.exists(args.config) else None
        cfg = {"capture_api": base}
        if cfg_path:
            cfg = dict(load_config(cfg_path))
        probe_cfg_fd, probe_cfg = tempfile.mkstemp(suffix=".json", prefix="capture-probe-cfg-")
        with os.fdopen(probe_cfg_fd, "w") as fh:
            json.dump(cfg, fh)

        # 1. Auth: a wrong secret is rejected before anything else.
        status, _, _ = http_json(f"{base}/api/runner", {"action": "list"},
                                 {"Authorization": "Bearer wrong-" + secret})
        check("runner API rejects a wrong secret (401)", status == 401)

        # 2. tease refuses before printed.
        halted, _ = run_cmd(cmd_tease, expect_halt=True, piece=slug,
                            channel="linkedin", variant="mystery", again=False,
                            config=probe_cfg)
        check("tease refuses an unprinted piece", halted)

        # 3. printed, then tease issues a link and the ledger holds only a hash.
        run_cmd(cmd_printed, piece=slug, name="Proba Fakerson")
        halted, out = run_cmd(cmd_tease, piece=slug, channel="linkedin",
                              variant="mystery", again=False, config=probe_cfg)
        m = re.search(r"/for/([A-Za-z0-9_-]{16,64})", out)
        token = m.group(1) if m else ""
        check("tease issues a link for a printed piece", not halted and bool(token))
        ledger = json.loads(read_file(CAPTURE_PATH))
        check("ledger stores a token hash, never the raw token",
              token not in json.dumps(ledger)
              and ledger[slug]["token_sha256"] == hashlib.sha256(token.encode()).hexdigest())

        # 4. The page and its states.
        status, data, headers = http_json(f"{base}/api/token?t={token}")
        check("token lookup: active, greets by first name",
              status == 200 and data == {"state": "active", "first_name": "Proba"})
        status, data, headers = http_json(f"{base}/api/token?t=AAAAAAAAAAAAAAAAAAAAAA")
        check("unknown token reads as expired", data.get("state") == "expired")
        req = urllib.request.Request(f"{base}/for/{token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            page = resp.read().decode()
            final_url = resp.geturl()
            check("/for/<token> serves the page with a noindex header",
                  "noindex" in resp.headers.get("X-Robots-Tag", "") and "One copy" in page)
            # The deploy-blocker class: a rewrite destination that still carries
            # .html would 308 away under cleanUrls and STRIP the token. Assert
            # the token survives to the served URL (the page reads it from the
            # path). The harness now reproduces cleanUrls, so this is a real
            # guard against a vercel.json rewrite regression.
            check("/for/<token> keeps the token (no cleanUrls redirect strips it)",
                  final_url.rstrip("/").endswith(token))
            # Privacy: the token page must not reference any third-party origin
            # (no Google Fonts, no analytics). Locks the self-hosted-font fix.
            page_l = page.lower()
            check("token page references no third-party origin",
                  not any(h in page_l for h in
                          ("googleapis.com", "gstatic.com", "fonts.google",
                           "plausible.io", "//cdn", "http://")))
        # cleanUrls is actually active: /for.html redirects to the clean path.
        with urllib.request.urlopen(urllib.request.Request(f"{base}/for.html"), timeout=10) as resp:
            check("cleanUrls active: /for.html resolves to the clean /for path",
                  resp.geturl().rstrip("/").endswith("/for"))

        # 5. Submission: validation, storage, and an address-free notification.
        status, data, _ = http_json(f"{base}/api/submit",
                                    {"token": token, "address_type": "office",
                                     "line1": "1 Probe Street"})
        check("submit rejects missing fields (400)", status == 400)
        address = {"token": token, "address_type": "office",
                   "line1": "1 Probe Street", "line2": "Floor 2",
                   "city": "Lisbon", "postcode": "1100-001", "country": "Portugal"}
        status, data, _ = http_json(f"{base}/api/submit", address)
        check("submit stores a complete address", status == 200 and data.get("ok"))
        _, mails, _ = http_json(f"http://127.0.0.1:{resend_port}/_emails")
        mail_text = json.dumps(mails)
        check("notification sent, carries the piece id",
              len(mails) == 1 and slug in mail_text)
        check("notification contains no fragment of the address",
              not any(v in mail_text for v in
                      ("1 Probe Street", "Floor 2", "Lisbon", "1100-001", "Portugal")))

        # 6. A submitted address can never be orphaned by a re-tease.
        halted, out = run_cmd(cmd_tease, expect_halt=True, piece=slug,
                              channel="email", variant="teaser", again=True,
                              config=probe_cfg)
        check("re-tease over a submitted address is refused by the API (409)",
              halted and "address is on file" in out)

        # 7. capture syncs the submission; the client-side guard now refuses too.
        halted, out = run_cmd(cmd_capture, piece=None, offline=False, config=probe_cfg)
        ledger = json.loads(read_file(CAPTURE_PATH))
        check("capture syncs the submission to address-received",
              not halted and ledger[slug]["status"] == "address-received"
              and "1/1" in out)
        halted, out = run_cmd(cmd_tease, expect_halt=True, piece=slug,
                              channel="email", variant="teaser", again=False,
                              config=probe_cfg)
        check("tease refuses locally once address-received",
              halted and "address on file" in out.replace("an address", "address"))

        # 8. address prints it; delivered purges everything, verified raw.
        halted, out = run_cmd(cmd_address, piece=slug, config=probe_cfg)
        check("address prints the submitted address",
              not halted and "1 Probe Street" in out and "Lisbon" in out)
        _, dump, _ = http_json(f"http://127.0.0.1:{redis_port}/_dump")
        check("TTL safety net armed on the store keys",
              any(k.startswith("tok:") for k in dump.get("ttl_keys", [])))
        halted, _ = run_cmd(cmd_delivered, piece=slug, config=probe_cfg)
        _, dump, _ = http_json(f"http://127.0.0.1:{redis_port}/_dump")
        leftovers = [k for k in dump.get("keys", [])
                     if k.startswith("tok:") or k.startswith("piece:")]
        check("delivered leaves no token or address key in the store",
              not halted and not leftovers)
        halted, _ = run_cmd(cmd_address, expect_halt=True, piece=slug, config=probe_cfg)
        check("pull after purge finds nothing (404)", halted)

        # 9. An expired record reads as expired (injected with a past date).
        expired_rec = json.dumps({"piece_id": "old", "first_name": "Olda",
                                  "state": "active",
                                  "created_at": "2026-01-01T00:00:00Z",
                                  "expires_at": "2026-02-01T00:00:00Z"})
        http_json(f"http://127.0.0.1:{redis_port}",
                  ["SET", "tok:ExpiredExpiredExpired1", expired_rec])
        status, data, _ = http_json(f"{base}/api/token?t=ExpiredExpiredExpired1")
        check("expired token reads as expired, no name leaked",
              data == {"state": "expired"})

        # 10. The rate limit bites, AND a spoofed X-Forwarded-For does not
        # bypass it. The harness injects x-real-ip like Vercel does, so a fresh
        # forged X-Forwarded-For per request must still land in the same bucket.
        limited = False
        for i in range(140):
            status, _, _ = http_json(
                f"{base}/api/token?t={'B' * 22}",
                headers={"X-Forwarded-For": f"203.0.113.{i % 256}, 10.0.0.{i % 256}"})
            if status == 429:
                limited = True
                break
        check("rate limit holds against a spoofed X-Forwarded-For", limited)

        os.unlink(probe_cfg)
    finally:
        CAPTURE_PATH = saved_path
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        harness.terminate()
        try:
            harness.wait(timeout=5)
        except subprocess.TimeoutExpired:
            harness.kill()
        if os.path.exists(ledger_path):
            os.unlink(ledger_path)

    failed = [label for label, ok in results if not ok]
    print("-" * 70)
    print(f"{len(results) - len(failed)}/{len(results)} probes passed.")
    if failed:
        die("capture-probe failures: " + "; ".join(failed))


# --- Batch processing -------------------------------------------------------
# Two phases over a JSON manifest of recipients, each shelling out to the
# single-piece `generate` path as a subprocess so one piece failing never kills
# the batch (full isolation, reuses the tested per-piece flow verbatim).
#
#   batch-brief  -> runs Prompt 2 for every recipient, writes a review sheet and
#                   an editable approvals file (APPROVE/SKIP per piece).
#   batch-build  -> for every APPROVEd piece, runs copy -> render -> QC ->
#                   follow-up, then writes a summary sheet.

def _piece_slug(entry):
    return f"{slugify(entry['name'])}-{slugify(entry['company'])}"


def _batch_path(manifest_path, suffix):
    base = os.path.splitext(os.path.abspath(manifest_path))[0]
    return f"{base}.{suffix}"


def _parse_credit(text):
    m = re.findall(r"approx \$([0-9.]+)", text)
    return float(m[-1]) if m else 0.0


def _last_halt_reason(out, err):
    for src in (err, out):
        for line in reversed(src.splitlines()):
            s = line.strip()
            if s.startswith("[halt]"):
                return s[len("[halt]"):].strip()
    for src in (err, out):
        lines = [l.strip() for l in src.splitlines() if l.strip()]
        if lines:
            return lines[-1][:200]
    return "unknown error"


def _load_manifest(path):
    entries = json.loads(read_file(path))
    if not isinstance(entries, list) or not entries:
        die("manifest must be a non-empty JSON array of recipient objects.")
    mdir = os.path.dirname(os.path.abspath(path))
    for e in entries:
        for k in ("name", "title", "company", "format"):
            if not e.get(k):
                die(f"manifest entry missing '{k}': {e}")
        if e["format"].lower() not in ("newspaper", "email", "crossword"):
            die(f"manifest entry has bad format '{e['format']}': {e}")
    return entries, mdir


def _run_piece(entry, mode, config_path, research_abs=None):
    cmd = [sys.executable, os.path.abspath(__file__), "generate",
           "--name", entry["name"], "--title", entry["title"],
           "--company", entry["company"], "--format", entry["format"],
           "--config", config_path]
    if entry.get("source_signals"):
        cmd += ["--source-signals", json.dumps(entry["source_signals"])]
    if mode == "brief":
        cmd += ["--brief-only", "--research", research_abs]
    else:
        cmd += ["--resume"]
        if entry.get("delivery_date"):
            cmd += ["--delivery-date", entry["delivery_date"]]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def cmd_batch_brief(args):
    entries, mdir = _load_manifest(args.manifest)
    rows = []
    for entry in entries:
        slug = _piece_slug(entry)
        research = entry.get("research", "")
        if research and not os.path.isabs(research):
            research = os.path.join(mdir, research)
        print(f"[batch-brief] {slug} ({entry['format']}) ...")
        if not research or not os.path.exists(research):
            rows.append({"slug": slug, "entry": entry, "status": "error",
                         "reason": f"research file not found: {research}", "credit": 0.0})
            print("  ERROR: research file not found")
            continue
        rc, out, err = _run_piece(entry, "brief", args.config, research_abs=research)
        credit = _parse_credit(out)
        bp = os.path.join(PIECES_DIR, slug, "brief.json")
        brief = json.loads(read_file(bp)) if os.path.exists(bp) else None
        gp = os.path.join(PIECES_DIR, slug, "gate.json")
        gate = json.loads(read_file(gp)) if os.path.exists(gp) else None
        if gate and gate.get("verdict") != "READY":
            print(f"  research gate: {gate.get('verdict')} "
                  f"({len(gate.get('gaps') or [])} gap(s), see review sheet)")
        if brief and brief.get("fit") == "poor":
            rows.append({"slug": slug, "entry": entry, "status": "poor-fit",
                         "brief": brief, "gate": gate, "credit": credit})
            print(f"  POOR FIT (${credit:.2f}) -> defaults to SKIP")
        elif rc == 0 and brief:
            rows.append({"slug": slug, "entry": entry, "status": "ok",
                         "brief": brief, "gate": gate, "credit": credit})
            print(f"  ok: {brief.get('problem_label', '')} (${credit:.2f})")
        else:
            rows.append({"slug": slug, "entry": entry, "status": "error",
                         "reason": _last_halt_reason(out, err), "gate": gate,
                         "credit": credit})
            print("  ERROR generating brief")

    _write_batch_review(args.manifest, rows)
    _write_batch_approvals(args.manifest, rows)
    total = sum(r.get("credit", 0.0) for r in rows)
    print("\n" + "=" * 70)
    print("BATCH BRIEFS DONE")
    print("=" * 70)
    print(f"  {os.path.basename(_batch_path(args.manifest, 'review.md'))}     "
          "<- read every brief here")
    print(f"  {os.path.basename(_batch_path(args.manifest, 'approvals.txt'))}  "
          "<- edit APPROVE/SKIP per piece, then run batch-build")
    print(f"\nApprox ${total:.2f} subscription credit across {len(rows)} briefs.")


def _write_batch_review(manifest_path, rows):
    out = ["# Sentrada batch — brief review", "",
           f"{len(rows)} pieces. Read each brief, then edit the approvals file "
           "(APPROVE/SKIP) and run `batch-build`.", ""]
    pairs = [("Fit", "fit"), ("Problem label", "problem_label"),
             ("Key metric", "key_metric"), ("Core problem", "core_problem"),
             ("Environment", "environment"), ("Moment", "moment"),
             ("Operational details", "operational_details"),
             ("Companion hook", "companion_card_hook"),
             ("Reserve detail", "reserve_detail")]
    for r in rows:
        out.append(f"## {r['slug']}  ({r['entry']['format']})")
        g = r.get("gate")
        if g and (g.get("verdict") != "READY" or g.get("gaps")):
            out.append(f"- **Research gate: {g.get('verdict', '?')}**")
            for gap in (g.get("gaps") or []):
                out.append(f"  - {gap}")
        if r["status"] == "poor-fit":
            out.append(f"- **POOR FIT** — {r['brief'].get('fit_reason', '')}. Default SKIP.")
        elif r["status"] == "error":
            out.append(f"- **ERROR** — {r.get('reason', 'brief did not generate')}. Default SKIP.")
        else:
            b = r["brief"]
            out.append(f"- Snapshot: {b.get('snapshot', '')}")
            for label, key in pairs:
                if b.get(key):
                    out.append(f"- {label}: {b[key]}")
        out.append("")
    write_file(_batch_path(manifest_path, "review.md"), "\n".join(out))


def _write_batch_approvals(manifest_path, rows):
    out = ["# Edit the first word (APPROVE or SKIP) on each line, then run batch-build.",
           "# Poor-fit and errored briefs default to SKIP. To revise a brief, re-run",
           "# it singly with: generate ... --brief-only --feedback \"notes\", then APPROVE.",
           ""]
    for r in rows:
        if r["status"] == "ok":
            decision, note = "APPROVE", f"fit: {r['brief'].get('fit', '?')} | {r['brief'].get('problem_label', '')}"
        elif r["status"] == "poor-fit":
            decision, note = "SKIP", "POOR FIT"
        else:
            decision, note = "SKIP", "ERROR generating brief"
        g = r.get("gate") or {}
        if g.get("verdict") == "NOT_READY":
            decision = "SKIP"
            note += " | research NOT_READY (see review sheet)"
        elif g.get("verdict") == "READY_WITH_GAPS":
            note += " | research gaps (see review sheet)"
        out.append(f"{decision:8} {r['slug']:38} | {note}")
    write_file(_batch_path(manifest_path, "approvals.txt"), "\n".join(out) + "\n")


def _read_batch_approvals(manifest_path):
    path = _batch_path(manifest_path, "approvals.txt")
    if not os.path.exists(path):
        die(f"approvals file not found: {path}. Run batch-brief first.")
    out = {}
    for line in read_file(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) >= 2 and parts[0].upper() in ("APPROVE", "SKIP"):
            out[parts[1]] = parts[0].upper()
    return out


def cmd_batch_build(args):
    entries, _ = _load_manifest(args.manifest)
    # The staleness guard: a gate template edited since the last harness run is
    # an unmeasured gate, and a real batch is the wrong place to find out what
    # it now misses. Warn and require an explicit yes; never silently proceed.
    warn = gate_staleness_warning()
    if warn and not args.ignore_stale_gates:
        print("\n" + "!" * 70)
        print(f"[guard] {warn}.")
        print("[guard] The grounding gate is unmeasured against the exam. Run\n"
              "        `python runner/sentrada_runner.py harness` first.")
        print("!" * 70)
        ans = input("Type 'yes' to build this batch anyway: ").strip()
        if ans.lower() != "yes":
            die("batch stopped. Run the harness, then re-run batch-build.")
    by_slug = {_piece_slug(e): e for e in entries}
    approvals = _read_batch_approvals(args.manifest)
    results = []
    for slug, decision in approvals.items():
        if decision != "APPROVE":
            results.append({"slug": slug, "status": "skipped", "credit": 0.0})
            continue
        entry = by_slug.get(slug)
        if not entry:
            results.append({"slug": slug, "status": "error",
                            "detail": "slug not in manifest", "credit": 0.0})
            continue
        print(f"[batch-build] {slug} ({entry['format']}) ...")
        rc, out, err = _run_piece(entry, "build", args.config)
        results.append(_assess_build(slug, entry, rc, out, err))
        print(f"  {results[-1]['status']} (${results[-1].get('credit', 0.0):.2f})")
    _write_batch_summary(args.manifest, results)
    # Durability: push every completed piece's render + card to the deliverables
    # branch immediately. runner/pieces/ does not survive a container restart;
    # the deliverables branch does. Guarded so a snapshot failure only warns.
    if not getattr(args, "no_snapshot", False):
        _auto_snapshot([r["slug"] for r in results if r["status"] == "complete"],
                       _batch_label(args.manifest))


def _assess_build(slug, entry, rc, out, err):
    credit = _parse_credit(out)
    base = {"slug": slug, "credit": credit}
    if entry["format"].lower() == "claymation":
        ready = os.path.exists(os.path.join(PIECES_DIR, slug, "image_prompt.txt"))
        return dict(base, status="claymation prompt ready" if (rc == 0 and ready) else "error",
                    detail="" if ready else _last_halt_reason(out, err))
    if "CHAIN STOPPED — Prompt 6 returned FAIL" in out:
        return dict(base, status="HELD: P6 FAIL")
    if "CHAIN STOPPED" in out and "WOULD BIN" in out:
        return dict(base, status="HELD: 6B WOULD BIN")
    if rc == 0 and "PIECE COMPLETE" in out:
        m = re.search(r"Prompt 6 \(craft\): ([A-Z]+)", out)
        return dict(base, status="complete", p6=(m.group(1) if m else "?"),
                    p6b=extract_6b_verdict(out) or "?")
    return dict(base, status="error", detail=_last_halt_reason(out, err))


def _write_batch_summary(manifest_path, results):
    total = sum(r.get("credit", 0.0) for r in results)
    done = sum(1 for r in results if r["status"] in ("complete", "claymation prompt ready"))
    held = sum(1 for r in results if str(r["status"]).startswith("HELD"))
    errs = sum(1 for r in results if r["status"] == "error")
    out = ["# Sentrada batch — build summary", "",
           f"{done} ready, {held} held for review, {errs} errored, {len(results)} total.",
           f"Approx ${total:.2f} subscription credit.", ""]
    for r in results:
        line = f"- **{r['slug']}** — {r['status']}"
        if r.get("p6"):
            line += f" | P6 {r['p6']}"
        if r.get("p6b"):
            line += f" | 6B {r['p6b']}"
        if r.get("detail"):
            line += f" | {r['detail']}"
        if r.get("credit"):
            line += f" | ${r['credit']:.2f}"
        out.append(line)
    out += ["", "Each piece's files are in runner/pieces/<slug>/ (render, qc_review.md, "
            "qc_recipient.md, followup.md). Held pieces: read the reason before re-running.",
            "", "Before staging PNGs for the print supplier, run "
            "`ship-check --manifest <this manifest>` to confirm none were re-rendered "
            "after QC."]
    write_file(_batch_path(manifest_path, "summary.md"), "\n".join(out))
    print("\n" + "=" * 70)
    print("BATCH BUILD DONE")
    print("=" * 70)
    print("\n".join(out))


# --- reply (P7b): delegate to reply.py ---------------------------------------

def cmd_reply(args):
    _usage_reset()
    import reply as reply_module
    reply_module.run(args)
    _print_usage("reply")


# --- CLI --------------------------------------------------------------------

# --- gate-probe: regression harness for the copy gates -----------------------
# Turns the validation method that found the edition-line / stat_source /
# postscript blind spots into a repeatable command. Each probe plants a known
# violation into a copy of a real piece's data and asserts the grounding gate
# flags it; the clean copy must still pass (baseline). Probes go through the
# format's copy-text builder on purpose: a probe planted in a field the builder
# omits comes back MISSED, which is exactly the blind-spot signal.
# Run after any edit to prompt4b, house_rules, or a copy-text builder.

_GATE_PROBES = {
    "newspaper": [
        ("fabricated-city", "reykjavik",
         lambda d: d.__setitem__("edition_line", d.get("edition_line", "") + ", Reykjavik")),
        ("fabricated-metric", "23.7",
         lambda d: d.__setitem__("lead_article", d.get("lead_article", "")
                                 + " Internal figures put the gain at 23.7% in a single quarter.")),
        ("litigation-leverage", "tribunal",
         lambda d: d.__setitem__("lead_article", d.get("lead_article", "")
                                 + " With an employment tribunal hanging over the programme, "
                                   "the case for acting now is sharper still.")),
        ("personal-detail", "village",
         lambda d: d.__setitem__("lead_article", d.get("lead_article", "")
                                 + " Away from the office, he is understood to live quietly "
                                   "with his family in a village outside the city.")),
        ("over-attributed-proof", "ftse",
         lambda d: d.__setitem__("lead_article", d.get("lead_article", "")
                                 + " Pieces like this one have already earned replies from "
                                   "three FTSE 100 chief executives.")),
        ("misattributed-quote", "memo",
         lambda d: d.__setitem__("pull_quote_attribution", "Company internal memo, 2026")),
        # The world-plausible industry commonplace (P4b violation 6): the model
        # believes these rather than knows them; two reached print files.
        ("industry-commonplace", "one per cent",
         lambda d: d.__setitem__("lead_article", d.get("lead_article", "")
                                 + " Reply rates to cold email at this seniority have, "
                                   "on most estimates, fallen below one per cent.")),
        ("elapsed-time-arithmetic", "eighteen months",
         lambda d: d.__setitem__("lead_article", d.get("lead_article", "")
                                 + " Eighteen months on from the 2019 launch, the "
                                   "programme is still the company's clearest bet.")),
    ],
    "email": [
        ("fabricated-postscript", "initech",
         lambda d: d.__setitem__("postscript",
                                 "P.S. Your peers at Initech saw a 40% lift within a month.")),
        ("fabricated-customer", "globex",
         lambda d: d["body"].append({"type": "p", "text": "Teams at Globex Corporation cut "
                                     "their integration backlog in half with us."})),
    ],
    "crossword": [
        ("fabricated-clue", "reykjavik",
         lambda d: d["candidates"].append({"answer": "REYKJAVIK",
                                           "clue": "Home of the company's newest office"})),
        ("litigation-clue", "tribunal",
         lambda d: d["candidates"].append({"answer": "TRIBUNAL",
                                           "clue": "Where the restructuring row may end up"})),
    ],
}

_COPY_TEXT_BUILDERS = {"newspaper": lambda d: newspaper_copy_text(d),
                       "email": lambda d: email_copy_text(d),
                       "crossword": lambda d: crossword_copy_text(d)}


def _lint_probes():
    """Deterministic lint probes: build tiny synthetic piece folders and assert
    _copy_lint raises the expected hold/warn. Free (no model calls)."""
    import tempfile
    cases = [
        ("lint-em-dash", "newspaper",
         {"followup.md": "**TOUCH 1 EMAIL**\nSubject: x\n\nBody — with an em dash.\n"},
         "hold", "em dash"),
        ("lint-banned-opener", "newspaper",
         {"followup.md": "**TOUCH 1 EMAIL**\nSubject: x\n\nSomething arrived on your desk this week.\n"},
         "hold", "banned line"),
        ("lint-missing-subject", "newspaper",
         {"followup.md": "**TOUCH 1 EMAIL**\n\nNo subject line here at all.\n"},
         "hold", "no subject line"),
        ("lint-long-connection-note", "newspaper",
         {"followup.md": "**TOUCH 1 EMAIL**\nSubject: x\n\nBody.\n\n"
                         "**TOUCH 2 CONNECTION NOTE VARIANT**\n" + ("w" * 300) + "\n\n**TOUCH 3**\nx\n"},
         "hold", "connection-note"),
        ("lint-grid-ref", "crossword",
         {"data.json": json.dumps({"title": "T", "subtitle": "The one that stings is 14-Across",
                                   "candidates": []})},
         "hold", "grid-number"),
        ("lint-litigation-warn", "newspaper",
         {"followup.md": "**TOUCH 1 EMAIL**\nSubject: x\n\nThe lawsuit changes the maths.\n"},
         "warn", "litigation"),
        ("lint-restated-ask", "newspaper",
         {"followup.md": "**TOUCH 1 EMAIL**\nSubject: x\n\nJane,\n\nBody one.\n\n"
                         "Happy to pick this up with whoever runs ABM on your side.\n\n"
                         "**TOUCH 2 LINKEDIN**\n\nJane,\n\n"
                         "Happy to pick this up with whoever owns the programme.\n\n"
                         "**TOUCH 3 BUMP EMAIL**\n\nJane,\n\nFresh detail here.\n"},
         "hold", "advance the angle"),
        # The blind-spot class itself: a rendered text field the copy-text
        # builder does not include must be caught, not shipped ungated.
        ("lint-ungated-field", "crossword",
         {"data.json": json.dumps({"title": "TEST CROSSWORD", "subtitle": "A test line",
                                   "candidates": [],
                                   "promo_line": "Printed in Milton Keynes since 1962"})},
         "hold", "invisible to the grounding gate"),
    ]
    results = []
    for name, fmt, files, kind, needle in cases:
        with tempfile.TemporaryDirectory() as td:
            for fn, content in files.items():
                write_file(os.path.join(td, fn), content)
            holds, warns = _copy_lint(td, fmt)
            hits = holds if kind == "hold" else warns
            caught = any(needle.lower() in h.lower() for h in hits)
            results.append((name, caught,
                            "; ".join(hits)[:70] if hits else "(nothing raised)"))
    return results


def cmd_gate_probe(args):
    _usage_reset()
    config = load_config(args.config)
    folder = args.folder
    only = {c.strip() for c in (args.classes or "").split(",") if c.strip()}

    print("\n" + "=" * 70)
    print("GATE PROBE — regression harness for the copy gates")
    print("=" * 70)

    failures = 0

    print("\n[lint probes] deterministic, no model calls:")
    for name, caught, detail in _lint_probes():
        mark = "CAUGHT" if caught else "MISSED"
        failures += 0 if caught else 1
        print(f"  {mark:7} {name:28} {detail}")

    if args.lint_only:
        _finish_gate_probe(failures)
        return

    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    fmt = meta.get("format")
    probes = _GATE_PROBES.get(fmt)
    if not probes:
        die(f"gate-probe has no model probes for format '{fmt}'.")
    data = json.loads(read_file(os.path.join(folder, "data.json")))
    research = read_file(os.path.join(folder, "research.md"))
    build = _COPY_TEXT_BUILDERS[fmt]
    # The piece's copy may legitimately cite its BUILD-TIME sender's proof, so
    # judge it against the sender it was written under (meta snapshot), not
    # whatever sender happens to be live in config today.
    sender = meta.get("sender") or config.get("sender", {})
    sender_facts = "\n".join(
        f"- {x}" for x in (sender.get("company", ""), sender.get("what_they_sell", ""),
                           sender.get("proof_points", "")) if str(x).strip())

    if not args.skip_baseline:
        print(f"\n[baseline] the piece's own copy must pass clean ({fmt})...")
        g, issues = grounding_check(config, research, build(data), sender_facts=sender_facts)
        if g:
            print("  CLEAN   baseline grounded (as required)")
        else:
            failures += 1
            print("  DIRTY   baseline copy is NOT grounded; probe results below are "
                  "unreliable. Fix the piece or pick a clean one:")
            for i in issues:
                print(f"          - {i.get('claim', '')[:60]} -> {i.get('issue', '')[:60]}")

    print(f"\n[model probes] one grounding call each ({fmt}):")
    import copy as _copy
    for name, marker, inject in probes:
        if only and name not in only:
            continue
        poisoned = _copy.deepcopy(data)
        inject(poisoned)
        g, issues = grounding_check(config, research, build(poisoned),
                                    sender_facts=sender_facts)
        blob = json.dumps(issues).lower()
        caught = (not g) and marker.lower() in blob
        mark = "CAUGHT" if caught else "MISSED"
        failures += 0 if caught else 1
        why = "" if caught else (" (gate passed it)" if g else
                                 f" (flagged, but no issue mentions '{marker}')")
        print(f"  {mark:7} {name:28}{why}")

    _finish_gate_probe(failures)


def _finish_gate_probe(failures):
    print("-" * 70)
    if failures:
        print(f"{failures} probe(s) MISSED. A gate or copy-text builder has a blind "
              "spot (or the baseline is dirty). Do not trust the gates until fixed.")
        _print_usage("gate-probe")
        sys.exit(1)
    print("All probes caught. The gates see what they need to see.")
    _print_usage("gate-probe")


# --- engine-probe: regression harness for the layout engines ------------------
# The third probe leg: gate-probe covers the copy gates, capture-probe the
# address flow, engine-probe the deterministic render contract every engine
# promises (--check exits non-zero on oversized copy without rendering, renders
# carry the right print size, DPI metadata and sRGB tag, a failed render
# quarantines as *.FAILED.png with no clean output, a re-render is
# byte-identical) plus the runner's own pure logic: slug/verdict parsing, the
# ungated-field lint, the DELIVERY block parser and snapshot idempotence in a
# throwaway git repo. No model calls; free to run. Run after any edit to an
# engine, a run_*_engine helper, or the snapshot/CSV code.

def _probe_break_data(fmt, data):
    """Mutate a copy of a known-good data dict so the engine must refuse it."""
    if fmt == "newspaper":
        data["lead_article"] = " ".join([data["lead_article"]] * 3)
    elif fmt == "crossword":
        data["subtitle"] = ("An interminable subtitle that cannot possibly be "
                            "shrunk onto the line ") * 20
    elif fmt == "email":
        filler = {"type": "p",
                  "text": "This paragraph repeats far past the sheet. " * 30}
        data["body"] = data["body"] + [dict(filler) for _ in range(30)]
    elif fmt == "card":
        data["body"] = [p + (" and the point keeps going on" * 40)
                        for p in data["body"]]
    return data


def _engine_probe_specs(config):
    """(fmt, engine, template, bundled test data, print size in mm) per engine.
    Sizes: production formats print at A2, the companion card at A6."""
    return [
        ("newspaper", config.get("engine"), config.get("newspaper_template"),
         os.path.join(REPO_ROOT, "newspaper", "cognism.json"), (420, 594)),
        ("crossword", config.get("crossword_engine"), config.get("crossword_template"),
         os.path.join(REPO_ROOT, "crossword", "test_cognism.json"), (420, 594)),
        ("email", config.get("email_engine"), None,
         os.path.join(REPO_ROOT, "email", "test_qflow.json"), (420, 594)),
        ("card", config.get("card_engine"), None,
         os.path.join(REPO_ROOT, "card", "samples", "chris.json"), (105, 148)),
    ]


def _probe_engine_cmd(engine, template, data_path, output=None, check=False, dpi=None):
    """Invoke an engine exactly as the run_*_engine helpers do (same
    interpreter, cwd, flag shape), with a --print-dpi override for fast probes."""
    cmd = [sys.executable, engine, "--data", data_path]
    if template:
        cmd += ["--template", template]
    if check:
        cmd.append("--check")
    if output:
        cmd += ["--output", output, "--print-dpi", str(dpi or 360)]
    r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _probe_png_report(path, dpi, size_mm):
    """Problems with a rendered PNG's print contract; empty list means clean."""
    from PIL import Image
    problems = []
    with Image.open(path) as img:
        w, h = img.size
        if img.mode != "RGB":
            problems.append(f"mode {img.mode}, expected RGB")
        if not img.info.get("icc_profile"):
            problems.append("no ICC profile (sRGB tag missing)")
        meta = img.info.get("dpi")
        if not meta or abs(meta[0] - dpi) > 1 or abs(meta[1] - dpi) > 1:
            problems.append(f"dpi metadata {meta}, expected {dpi}")
        want = sorted(round(m / 25.4 * dpi) for m in size_mm)
        got = sorted((w, h))
        tol = max(2, dpi // 10)          # ~2.5mm: rounding, never a wrong size
        if any(abs(a - b) > tol for a, b in zip(got, want)):
            problems.append(f"size {w}x{h}px, expected ~{want[0]}x{want[1]} at {dpi}dpi")
    return problems


def _probe_runner_units(check):
    check("slugify collapses punctuation runs",
          slugify("Jane O'Brien @ Acme!") == "jane-o-brien-acme")
    check("slugify never returns empty", slugify("!!!") == "x")
    j = extract_last_json('x ```json\n{"a": 1}\n``` y ```json\n{"b": 2}\n```')
    check("extract_last_json takes the last block", j == {"b": 2})
    check("extract_last_json tolerates malformed JSON",
          extract_last_json("```json\n{oops\n```") is None)
    check("P6 verdict read through markdown wrap",
          extract_p6_verdict("**VERDICT: BORDERLINE**") == "BORDERLINE")
    check("P6 verdict takes the earliest keyword on the line",
          extract_p6_verdict("VERDICT: FAIL (would otherwise PASS)") == "FAIL")
    check("P6 verdict absent reads as None",
          extract_p6_verdict("no verdict anywhere") is None)
    check("6B verdict phrase scan",
          extract_6b_verdict("blah WOULD ADMIRE AND IGNORE blah")
          == "WOULD ADMIRE AND IGNORE")
    check("surname drops honours and punctuation",
          _surname("Jane van Helsing OBE") == "Helsing"
          and _surname("Amy Smith-Jones") == "SmithJones")
    check("company condensed keeps casing, drops punctuation",
          _company_condensed("Verizon Business, Inc.") == "VerizonBusinessInc")
    email_data = json.loads(read_file(os.path.join(REPO_ROOT, "email",
                                                   "test_qflow.json")))
    check("ungated-field lint: bundled email data fully gated",
          ungated_copy_fields(email_data, "email") == [])
    email_data["promo_line"] = "Printed in Milton Keynes since 1962"
    check("ungated-field lint flags a field the builder omits",
          ungated_copy_fields(email_data, "email") == ["promo_line"])


def _probe_delivery(check):
    global PIECES_DIR
    saved = PIECES_DIR
    td = tempfile.mkdtemp(prefix="engine-probe-pieces-")
    PIECES_DIR = td
    try:
        os.makedirs(os.path.join(td, "p1"))
        write_file(os.path.join(td, "p1", "research.md"),
                   "prose...\nDELIVERY_STATUS: confirmed\n"
                   "DELIVERY_ADDRESS: 1 High St, London EC1A 1AA\n"
                   "DELIVERY_NOTES: reception, ask for the post room\n")
        d = _delivery_from_research("p1")
        check("DELIVERY block parsed from research.md",
              d == {"status": "CONFIRMED", "address": "1 High St, London EC1A 1AA",
                    "notes": "reception, ask for the post room"})
        os.makedirs(os.path.join(td, "p2"))
        write_file(os.path.join(td, "p2", "research.md"),
                   "DELIVERY_STATUS: BLOCKED\nDELIVERY_ADDRESS: <office address>\n"
                   "DELIVERY_NOTES:\n")
        d = _delivery_from_research("p2")
        check("unfilled address placeholder reads as empty",
              d["address"] == "" and d["status"] == "BLOCKED" and d["notes"] == "")
        check("missing research.md reads as empty",
              _delivery_from_research("nope")
              == {"status": "", "address": "", "notes": ""})
    finally:
        PIECES_DIR = saved
        shutil.rmtree(td, ignore_errors=True)


def _probe_snapshot(check):
    """Prove _deliverables_snapshot's promises against a throwaway origin:
    a new render is pushed, an identical re-run is skipped (idempotence), and
    the file lands under batch-<label>/ on the deliverables branch."""
    global REPO_ROOT
    saved = REPO_ROOT
    base = tempfile.mkdtemp(prefix="engine-probe-git-")
    try:
        origin = os.path.join(base, "origin.git")
        work = os.path.join(base, "work")
        for cmd, cwd in ((["git", "init", "--bare", origin], base),
                         (["git", "init", work], base),
                         (["git", "remote", "add", "origin", origin], work)):
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            if r.returncode != 0:
                check("snapshot: throwaway git repo builds", False)
                return
        src = os.path.join(base, "x.png")
        with open(src, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nprobe-bytes")
        REPO_ROOT = work
        r1 = _deliverables_snapshot([(src, "x.png")], "probe", push=True)
        check("snapshot pushes a new render to deliverables",
              r1.get("staged") == 1 and r1.get("pushed") == 1
              and not r1.get("failed"))
        r2 = _deliverables_snapshot([(src, "x.png")], "probe", push=True)
        check("snapshot is idempotent (identical file skipped)",
              r2.get("skipped") == 1 and r2.get("staged") == 0)
        ls = subprocess.run(["git", "ls-tree", "-r", "--name-only", "deliverables"],
                            cwd=origin, capture_output=True, text=True)
        check("deliverables branch holds batch-probe/x.png",
              "batch-probe/x.png" in (ls.stdout or ""))
    finally:
        REPO_ROOT = saved
        shutil.rmtree(base, ignore_errors=True)


def cmd_engine_probe(args):
    config = load_config(args.config)
    only = {e.strip() for e in (args.engines or "").split(",") if e.strip()}
    results = []

    def check(label, ok, detail=""):
        results.append((label, ok))
        line = ("  PASS  " if ok else "  FAIL  ") + label
        if detail and not ok:
            line += "\n          " + detail.strip().replace("\n", "\n          ")
        print(line)

    print("\n" + "=" * 70)
    print("ENGINE PROBE — render contract + runner logic (no model calls)")
    print("=" * 70)
    td = tempfile.mkdtemp(prefix="engine-probe-")
    try:
        for fmt, engine, template, data_path, size_mm in _engine_probe_specs(config):
            if only and fmt not in only:
                continue
            print(f"\n[{fmt}]")
            if not engine or not os.path.exists(os.path.join(REPO_ROOT, engine)):
                check(f"{fmt}: engine present in config and on disk", False)
                continue
            rc, out = _probe_engine_cmd(engine, template, data_path, check=True)
            check(f"{fmt}: --check passes the bundled data", rc == 0, out[-400:])
            broken = _probe_break_data(fmt, json.loads(read_file(data_path)))
            bpath = os.path.join(td, fmt + "-broken.json")
            write_file(bpath, json.dumps(broken))
            rc, out = _probe_engine_cmd(engine, template, bpath, check=True)
            check(f"{fmt}: --check refuses oversized copy (non-zero exit)", rc != 0)
            if args.skip_render:
                continue
            out1 = os.path.join(td, fmt + "-a.png")
            rc, out = _probe_engine_cmd(engine, template, data_path,
                                        output=out1, dpi=args.dpi)
            ok = rc == 0 and os.path.exists(out1)
            check(f"{fmt}: renders the bundled data", ok, out[-400:])
            if ok:
                problems = _probe_png_report(out1, args.dpi, size_mm)
                check(f"{fmt}: render carries print size, DPI and sRGB tag",
                      not problems, "; ".join(problems))
                out2 = os.path.join(td, fmt + "-b.png")
                rc2, _ = _probe_engine_cmd(engine, template, data_path,
                                           output=out2, dpi=args.dpi)
                check(f"{fmt}: re-render is byte-identical (deterministic)",
                      rc2 == 0 and os.path.exists(out2)
                      and filecmp.cmp(out1, out2, shallow=False))
            qout = os.path.join(td, fmt + "-broken.png")
            rc, out = _probe_engine_cmd(engine, template, bpath,
                                        output=qout, dpi=args.dpi)
            qpath = os.path.join(td, fmt + "-broken.FAILED.png")
            check(f"{fmt}: failed render quarantines as *.FAILED.png, no clean "
                  "output", rc != 0 and not os.path.exists(qout)
                  and os.path.exists(qpath))
        print("\n[runner logic]")
        _probe_runner_units(check)
        _probe_delivery(check)
        print("\n[snapshot]")
        _probe_snapshot(check)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    fails = [label for label, ok in results if not ok]
    print("-" * 70)
    if fails:
        print(f"{len(fails)} probe(s) FAILED. Do not trust renders (or the "
              "snapshot path) until fixed.")
        sys.exit(1)
    print(f"All {len(results)} probes passed. Engines honour the render "
          "contract; runner logic intact.")


# --- qc-harness: the vision-QC exam -------------------------------------------
# What `harness` is to the P4b text gate, qc-harness is to the P6/6B vision
# verdicts: a committed library of cases (deterministic render recipe + optional
# defacement + expected-verdict constraints) run against the CURRENT prompt6/6b
# templates, one vision call per prompt per case. Because vision verdicts are
# judgment calls, cases assert DIRECTIONS, not exact matches: a clean render
# must not FAIL craft QC, a defaced/illegible one must, a generic piece must
# never earn WOULD TAKE THE MEETING. Case folder: runner/qc_cases/<id>/ with
# case.json ({format, run, data, mutate?, meta, brief?, expected}) and
# research.md. Renders build from engine data at run time (engines are
# deterministic), so no PNGs are committed.

QC_CASES_DIR = os.path.join(RUNNER_DIR, "qc_cases")
QC_SCORECARD_PATH = os.path.join(QC_CASES_DIR, "scorecard.json")
QC_TEMPLATES = ("prompt6_review.md", "prompt6b_recipient.md")


def _qc_case_render(case_dir, spec, config, out_png):
    """Render the case's engine data (repo-relative or case-local ref) and apply
    the optional defacement mutation. Raises on a failed render."""
    specs = {s[0]: s for s in _engine_probe_specs(config)}
    fmt, engine, template = spec["format"], None, None
    if fmt not in specs:
        raise RuntimeError(f"unknown format {fmt!r}")
    _, engine, template, _, _ = specs[fmt]
    ref = spec.get("data", "data.json")
    local = os.path.join(case_dir, ref)
    data_path = local if os.path.exists(local) else os.path.join(REPO_ROOT, ref)
    rc, out = _probe_engine_cmd(engine, template, data_path,
                                output=out_png, dpi=150)
    if rc != 0 or not os.path.exists(out_png):
        raise RuntimeError(f"case render failed: {out[-300:]}")
    mut = spec.get("mutate")
    if mut:
        from PIL import Image, ImageDraw, ImageFilter
        img = Image.open(out_png)
        w, h = img.size
        if mut["type"] == "blackout":
            x0, y0, x1, y1 = mut["box"]
            ImageDraw.Draw(img).rectangle(
                [x0 * w, y0 * h, x1 * w, y1 * h], fill=(12, 12, 12))
        elif mut["type"] == "blur":
            img = img.filter(ImageFilter.GaussianBlur(
                mut.get("radius_frac", 0.004) * h))
        else:
            raise RuntimeError(f"unknown mutation {mut['type']!r}")
        img.save(out_png)


def _qc_case_judge(outcome, expected):
    """Compare verdicts against the case's constraints. Returns (ok, reasons)."""
    ok, why = True, []
    for key, verdict in outcome.items():
        if verdict is None:
            ok = False
            why.append(f"{key}: verdict unreadable")
            continue
        must = expected.get(key + "_must")
        if must and verdict != must:
            ok = False
            why.append(f"{key}={verdict}, expected {must}")
        if verdict in (expected.get(key + "_must_not") or []):
            ok = False
            why.append(f"{key}={verdict} is forbidden for this case")
    return ok, why


def cmd_qc_harness(args):
    _usage_reset()
    config = load_config(args.config)
    only = {c.strip() for c in (args.cases or "").split(",") if c.strip()}
    if not os.path.isdir(QC_CASES_DIR):
        die(f"no vision-QC cases at {QC_CASES_DIR}")
    case_ids = sorted(d for d in os.listdir(QC_CASES_DIR)
                      if os.path.isdir(os.path.join(QC_CASES_DIR, d)))
    if only:
        case_ids = [c for c in case_ids if c in only]
    if not case_ids:
        die("no matching cases")

    print("\n" + "=" * 70)
    print("QC HARNESS — the vision-QC exam (P6 craft / 6B recipient sim)")
    print("=" * 70)
    results, failures = {}, 0
    for cid in case_ids:
        case_dir = os.path.join(QC_CASES_DIR, cid)
        spec = json.loads(read_file(os.path.join(case_dir, "case.json")))
        rp = os.path.join(case_dir, "research.md")
        research = read_file(rp) if os.path.exists(rp) else "(no research on file)"
        outcome = {}
        with tempfile.TemporaryDirectory(prefix="qc-harness-") as td:
            png = os.path.join(td, cid + ".png")
            try:
                _qc_case_render(case_dir, spec, config, png)
            except RuntimeError as e:
                results[cid] = {"ok": False, "why": [str(e)]}
                failures += 1
                print(f"  BROKEN  {cid:30} ({e})")
                continue
            if "p6" in spec.get("run", []):
                _, v = _p6(config, td, png, spec["meta"], spec.get("brief", {}),
                           research)
                outcome["p6"] = v
            if "p6b" in spec.get("run", []):
                _, v = _p6b(config, td, png, spec["meta"], research)
                outcome["p6b"] = v
        ok, why = _qc_case_judge(outcome, spec.get("expected", {}))
        failures += 0 if ok else 1
        got = ", ".join(f"{k}={v}" for k, v in outcome.items())
        print(f"  {'PASS' if ok else 'FAIL':7} {cid:30} {got}"
              + ("" if ok else "  [" + "; ".join(why) + "]"))
        results[cid] = {"ok": ok, "outcome": outcome, "why": why}

    write_file(QC_SCORECARD_PATH, json.dumps({
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "templates": {t: _engine_file_sha1(os.path.join(RUNNER_DIR, "templates", t))
                      for t in QC_TEMPLATES},
        "cases": len(results), "failed": failures,
        "results": results,
    }, indent=2))
    print("-" * 70)
    if failures:
        print(f"{failures} case(s) FAILED. A vision prompt has drifted (or a case "
              "expectation needs recalibrating — check the verdicts above).")
        _print_usage("qc-harness")
        sys.exit(1)
    print(f"All {len(results)} vision-QC cases hold. Scorecard: "
          f"{os.path.relpath(QC_SCORECARD_PATH, REPO_ROOT)}")
    _print_usage("qc-harness")


# --- harness: the grounding-gate exam ----------------------------------------
# A committed library of real cases (research + gate-visible copy + expected
# verdict) that the CURRENT 4b template is run over, one grounding call per
# case, nothing else in the chain. Where gate-probe plants synthetic violations
# into one clean piece to find copy-text-builder blind spots, the harness holds
# the durable exam: every incident that ever shipped (or nearly shipped) becomes
# a named case, so a template edit that quietly un-learns an old lesson shows up
# as a named regression. Scope is factual and credibility errors only.
#
# Case folder: runner/cases/<case-id>/ containing research.md, copy.txt,
# expected.json, and (email cases only) sender_facts.txt. expected.json is
# either {"verdict": "pass"} or {"verdict": "must_flag", "violation_type": ...,
# "offending_detail": ..., "match_terms": [...]}. A must-flag case counts as
# CAUGHT only when the gate flags it AND one of the match_terms appears in the
# gate's issues; flagged-for-a-different-claim counts as a MISS (listed as
# such), because the real error still ships. Cases created by `flag` carry
# "draft": true until `retro` confirms them.

CASES_DIR = os.path.join(RUNNER_DIR, "cases")
SCORECARD_PATH = os.path.join(CASES_DIR, "scorecard.json")
RETRO_LOG_PATH = os.path.join(CASES_DIR, "retro_log.md")
GROUNDING_TEMPLATE = "prompt4b_grounding.md"


def gate_template_files():
    """Every template file the grounding gate loads at call time. The 4b
    template is standalone today; if it ever gains the {{house_rules}} include,
    the guard automatically starts watching house_rules.md too."""
    files = [GROUNDING_TEMPLATE]
    text = read_file(os.path.join(TEMPLATE_DIR, GROUNDING_TEMPLATE))
    if "{{house_rules}}" in text:
        files.append("house_rules.md")
    return files


def gate_templates_state():
    return {name: _engine_file_sha1(os.path.join(TEMPLATE_DIR, name))
            for name in gate_template_files()}


def gate_staleness_warning():
    """None when the last saved harness run exercised the current gate
    templates; otherwise a one-line explanation for the batch guard."""
    if not os.path.exists(SCORECARD_PATH):
        return "no harness run is recorded (runner/cases/scorecard.json missing)"
    sc = json.loads(read_file(SCORECARD_PATH))
    stale = [name for name, sha in gate_templates_state().items()
             if sc.get("templates", {}).get(name) != sha]
    if stale:
        return (", ".join(stale) + " changed since the last harness run"
                + (f" ({sc.get('run_at')})" if sc.get("run_at") else ""))
    return None


def _load_cases(only=None):
    if not os.path.isdir(CASES_DIR):
        die(f"no cases folder at {os.path.relpath(CASES_DIR, REPO_ROOT)}.")
    cases, skipped = [], []
    for d in sorted(os.listdir(CASES_DIR)):
        path = os.path.join(CASES_DIR, d)
        if not os.path.isdir(path):
            continue
        if only and d not in only:
            continue
        needed = [os.path.join(path, f) for f in ("research.md", "copy.txt",
                                                  "expected.json")]
        if not all(os.path.exists(p) for p in needed):
            skipped.append(d)
            continue
        expected = json.loads(read_file(needed[2]))
        if expected.get("verdict") not in ("pass", "must_flag"):
            skipped.append(d)
            continue
        sf_path = os.path.join(path, "sender_facts.txt")
        cases.append({
            "id": d,
            "research": read_file(needed[0]),
            "copy_text": read_file(needed[1]),
            "sender_facts": read_file(sf_path) if os.path.exists(sf_path) else "",
            "expected": expected,
        })
    for d in skipped:
        print(f"[warn] case '{d}' is malformed (missing files or bad verdict); skipped.")
    return cases


def _judge_case(case, grounded, issues):
    """Score one gate result against the case's expected verdict. Returns
    (ok: bool, outcome: str, detail: str)."""
    exp = case["expected"]
    if exp["verdict"] == "pass":
        if grounded:
            return True, "clean", ""
        flagged = "; ".join(f"\"{i.get('claim', '')[:60]}\"" for i in issues)
        return False, "false_alarm", flagged
    blob = json.dumps(issues).lower()
    terms = [str(t).lower() for t in (exp.get("match_terms") or []) if str(t).strip()]
    if grounded:
        return False, "missed", "(gate passed it)"
    if not terms:
        # A fresh flag with no confirmed terms yet: any flag counts, noted as such.
        return True, "caught", "(no match terms yet; any flag counted)"
    if any(t in blob for t in terms):
        return True, "caught", ""
    return False, "missed", ("(flagged, but no issue mentions "
                             + " or ".join(f"'{t}'" for t in terms) + ")")


def run_harness(config, only=None):
    """Run the grounding gate over the case library and print the scorecard.
    Returns the results dict. Saves the scorecard (and compares against the
    previous one) only on full runs; a --only subset is a spot check."""
    cases = _load_cases(only=only)
    if not cases:
        die("no valid cases found in runner/cases/.")
    must_flags = [c for c in cases if c["expected"]["verdict"] == "must_flag"]
    must_passes = [c for c in cases if c["expected"]["verdict"] == "pass"]

    print("\n" + "=" * 70)
    print("HARNESS — grounding-gate exam over runner/cases/")
    print("=" * 70)
    print(f"\n[cases] {len(cases)} loaded ({len(must_flags)} must-flag, "
          f"{len(must_passes)} must-pass); one grounding call each "
          f"({model_for(config, 'p4b')})...\n")

    results = {}
    for case in cases:
        grounded, issues = grounding_check(config, case["research"],
                                           case["copy_text"],
                                           sender_facts=case["sender_facts"])
        ok, outcome, detail = _judge_case(case, grounded, issues)
        results[case["id"]] = {
            "expected": case["expected"]["verdict"], "outcome": outcome,
            "detail": detail,
            "violation_type": case["expected"].get("violation_type", ""),
            "draft": bool(case["expected"].get("draft")),
        }
        mark = {"caught": "CAUGHT", "clean": "CLEAN", "missed": "MISSED",
                "false_alarm": "FALSE"}[outcome]
        note = results[case["id"]]["violation_type"] or ""
        if results[case["id"]]["draft"]:
            note = (note + " (draft)").strip()
        print(f"  {mark:7} {case['id']:36} {note}"
              + (f"  {detail}" if detail and not ok else ""))

    caught = [i for i, r in results.items()
              if r["expected"] == "must_flag" and r["outcome"] == "caught"]
    missed = [i for i, r in results.items() if r["outcome"] == "missed"]
    false_alarms = [i for i, r in results.items() if r["outcome"] == "false_alarm"]

    print("-" * 70)
    print(f"Caught {len(caught)} of {len(must_flags)} must-flag(s)."
          + ("" if not missed else " Misses:"))
    for i in missed:
        print(f"  - {i} {results[i]['detail']}")
    print(f"Wrongly flagged {len(false_alarms)} of {len(must_passes)} "
          "must-pass(es)." + ("" if not false_alarms else " False alarms:"))
    for i in false_alarms:
        print(f"  - {i}: {results[i]['detail']}")

    if only:
        print("\n[scorecard] subset run (--only); baseline not updated.")
        _print_usage("harness (subset)")
        return results

    previous = (json.loads(read_file(SCORECARD_PATH))
                if os.path.exists(SCORECARD_PATH) else None)
    _compare_scorecards(previous, results)
    history = (previous or {}).get("history", [])
    if previous:
        history = history + [{"run_at": previous.get("run_at"),
                              "caught": previous.get("caught"),
                              "must_flags": previous.get("must_flags"),
                              "false_alarms": previous.get("false_alarms"),
                              "must_passes": previous.get("must_passes")}]
    write_file(SCORECARD_PATH, json.dumps({
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "templates": gate_templates_state(),
        "caught": len(caught), "must_flags": len(must_flags),
        "false_alarms": len(false_alarms), "must_passes": len(must_passes),
        "results": results,
        "history": history[-50:],
    }, indent=2))
    print(f"[scorecard] saved to {os.path.relpath(SCORECARD_PATH, REPO_ROOT)}")
    _print_usage("harness")
    return results


def _compare_scorecards(previous, results):
    if not previous or not previous.get("results"):
        print("\nvs last run: first saved run, nothing to compare against.")
        return
    old = previous["results"]
    both = [i for i in results if i in old]
    newly_caught = [i for i in both if results[i]["outcome"] == "caught"
                    and old[i]["outcome"] == "missed"]
    newly_missed = [i for i in both if results[i]["outcome"] == "missed"
                    and old[i]["outcome"] == "caught"]
    new_false = [i for i in both if results[i]["outcome"] == "false_alarm"
                 and old[i]["outcome"] == "clean"]
    cleared_false = [i for i in both if results[i]["outcome"] == "clean"
                     and old[i]["outcome"] == "false_alarm"]
    if newly_missed or new_false:
        verdict = "WORSE"
    elif newly_caught or cleared_false:
        verdict = "BETTER"
    else:
        verdict = "SAME"
    print(f"\nvs last run ({previous.get('run_at', '?')}): {verdict}")
    for label, ids in (("newly caught", newly_caught),
                       ("newly missed", newly_missed),
                       ("new false alarms", new_false),
                       ("false alarms cleared", cleared_false)):
        if ids:
            print(f"  {label}: " + ", ".join(ids))


def cmd_harness(args):
    _usage_reset()
    config = load_config(args.config)
    only = {c.strip() for c in (args.only or "").split(",") if c.strip()} or None
    results = run_harness(config, only=only)
    bad = sum(1 for r in results.values() if r["outcome"] in ("missed", "false_alarm"))
    sys.exit(1 if bad else 0)


# --- flag / retro: shipped failures become cases, cases become rules ---------
# `flag` turns a piece I caught a factual problem in into a new must-flag case
# in one step and logs it. `retro` gathers everything flagged since the last
# retro, drafts a candidate rule + exemplar pair per flag for approval, applies
# each approved rule to the 4b template, and immediately re-runs the harness so
# a rule that regresses the gate is caught the moment it lands. Nothing edits a
# template without an explicit per-rule approval inside retro.


def _sender_facts_for(meta, config):
    """The sender-facts block a piece was gated under (meta snapshot first,
    live config as fallback), matching gate-probe's convention."""
    sender = (meta or {}).get("sender") or config.get("sender", {})
    return "\n".join(
        f"- {x}" for x in (sender.get("company", ""), sender.get("what_they_sell", ""),
                           sender.get("proof_points", "")) if str(x).strip())


def _quoted_terms(note):
    """Match terms drafted from the note: any \"quoted\" phrases in it."""
    return [t.strip() for t in re.findall(r'"([^"]{3,60})"', note) if t.strip()]


def cmd_flag(args):
    config = load_config(args.config)
    case_id = args.case_id or ("flagged-" + slugify(args.slug))
    case_dir = os.path.join(CASES_DIR, case_id)
    if os.path.exists(case_dir):
        die(f"case '{case_id}' already exists. Pass --case-id to pick another name.")

    folder = os.path.join(PIECES_DIR, args.slug)
    sender_facts = ""
    if os.path.isdir(folder):
        research = read_file(os.path.join(folder, "research.md"))
        meta_path = os.path.join(folder, "meta.json")
        meta = json.loads(read_file(meta_path)) if os.path.exists(meta_path) else {}
        fmt = meta.get("format", args.format)
        data_path = os.path.join(folder, "data.json")
        if os.path.exists(data_path) and fmt in _COPY_TEXT_BUILDERS:
            copy_text = _COPY_TEXT_BUILDERS[fmt](json.loads(read_file(data_path)))
        elif args.copy:
            copy_text = read_file(args.copy)
        else:
            die(f"{args.slug} has no data.json a copy-text builder understands "
                f"(format '{fmt}'). Pass the gate-visible copy with --copy.")
        if fmt == "email":
            sender_facts = _sender_facts_for(meta, config)
    else:
        # Piece folders are ephemeral; a flag must still be possible after the
        # container that built the piece is gone.
        if not (args.research and args.copy):
            die(f"no piece folder at runner/pieces/{args.slug}. Pass --research "
                "and --copy to archive the case from files.")
        research = read_file(args.research)
        copy_text = read_file(args.copy)
        if args.format == "email":
            sender_facts = _sender_facts_for({}, config)

    os.makedirs(case_dir)
    write_file(os.path.join(case_dir, "research.md"), research)
    write_file(os.path.join(case_dir, "copy.txt"), copy_text)
    if sender_facts:
        write_file(os.path.join(case_dir, "sender_facts.txt"), sender_facts)
    write_file(os.path.join(case_dir, "note.md"), args.note + "\n")
    write_file(os.path.join(case_dir, "expected.json"), json.dumps({
        "verdict": "must_flag",
        "violation_type": args.violation or "UNCLASSIFIED",
        "offending_detail": args.note,
        "match_terms": _quoted_terms(args.note),
        "draft": True,
    }, indent=2))

    stamp = time.strftime("%Y-%m-%d")
    line = f"- {stamp} | {case_id} | {args.slug} | {args.note}\n"
    header = ("# Retro log\n\nEvery `flag` appends here; every `retro` closes "
              "the entries above it with a marker.\n\n")
    existing = read_file(RETRO_LOG_PATH) if os.path.exists(RETRO_LOG_PATH) else header
    write_file(RETRO_LOG_PATH, existing + line)
    print(f"[flag] archived as {os.path.relpath(case_dir, REPO_ROOT)} "
          "(must-flag, draft until the next retro)")
    print(f"[flag] logged to {os.path.relpath(RETRO_LOG_PATH, REPO_ROOT)}")
    if not _quoted_terms(args.note):
        print("[flag] no \"quoted\" phrase in the note, so no match terms were "
              "drafted; any gate flag will count until retro confirms terms.")
    print("[flag] commit runner/cases/ so the case survives this container.")


def _flags_since_last_retro():
    if not os.path.exists(RETRO_LOG_PATH):
        return []
    entries = []
    for line in read_file(RETRO_LOG_PATH).splitlines():
        if line.startswith("## Retro"):
            entries = []
        elif line.startswith("- ") and line.count("|") >= 3:
            date, case_id, slug, note = [p.strip() for p in
                                         line[2:].split("|", 3)]
            entries.append({"date": date, "case_id": case_id, "slug": slug,
                            "note": note})
    return entries


def _append_retro_rule(rule_title, rule_text):
    """Insert the approved rule as the next numbered content violation in the
    4b template, before the inputs section. Returns the template's prior text
    so the caller can revert."""
    path = os.path.join(TEMPLATE_DIR, GROUNDING_TEMPLATE)
    before = read_file(path)
    anchor = "\nSource research (facts about the RECIPIENT"
    if anchor not in before:
        die("could not find the inputs section in the 4b template; apply the "
            "rule by hand.")
    next_num = max([int(n) for n in re.findall(r"(?m)^(\d+)\.\s+[A-Z]", before)]
                   or [0]) + 1
    block = f"{next_num}. {rule_title.strip().upper()}: {rule_text.strip()}\n"
    write_file(path, before.replace(anchor, "\n" + block + anchor, 1))
    return before


def cmd_retro(args):
    _usage_reset()
    config = load_config(args.config)
    entries = _flags_since_last_retro()
    if not entries:
        print("[retro] nothing flagged since the last retro. Flag pieces with:\n"
              "  python runner/sentrada_runner.py flag --slug <slug> --note '...'")
        return

    template_path = os.path.join(TEMPLATE_DIR, GROUNDING_TEMPLATE)
    decisions = []
    for e in entries:
        case_dir = os.path.join(CASES_DIR, e["case_id"])
        if not os.path.isdir(case_dir):
            print(f"[retro] case folder missing for {e['case_id']}; skipping.")
            decisions.append((e, "missing", None))
            continue
        expected = json.loads(read_file(os.path.join(case_dir, "expected.json")))
        print("\n" + "=" * 70)
        print(f"RETRO — {e['case_id']} (flagged {e['date']})")
        print("=" * 70)
        print(f"Note: {e['note']}")

        # Not fill(): the embedded gate template carries its own {{placeholders}}
        # which fill() would substitute or strip. Insert it verbatim, last.
        prompt = load_template("harness_retro.md")
        for key, val in (("research", read_file(os.path.join(case_dir, "research.md"))),
                         ("copy_text", read_file(os.path.join(case_dir, "copy.txt"))),
                         ("note", e["note"]),
                         ("offending_detail", expected.get("offending_detail", e["note"]))):
            prompt = prompt.replace("{{" + key + "}}", val)
        prompt = prompt.replace("{{gate_template}}", read_file(template_path))
        print(f"\n[retro] drafting a candidate rule ({model_for(config, 'p4b')})...")
        _, draft = cli_json(prompt, model_for(config, "p4b"))
        title = str(draft.get("rule_title", "")).strip() or "UNCLASSIFIED"
        rule_text = str(draft.get("rule_text", "")).strip()
        terms = [str(t) for t in (draft.get("match_terms") or []) if str(t).strip()]
        changelog = str(draft.get("changelog_line", "")).strip()
        print(f"\nCandidate rule {title}:\n  {rule_text}")
        print(f"Match terms for the case: {terms}")

        ans = input("\nType 'approve' to apply this rule to the 4b template and "
                    "re-run the harness, anything else to decline: ").strip()
        if ans.lower() != "approve":
            decisions.append((e, "declined", changelog))
            continue

        backup = _append_retro_rule(title, rule_text)
        expected["violation_type"] = title
        expected["match_terms"] = sorted(set((expected.get("match_terms") or [])
                                             + terms))
        expected.pop("draft", None)
        write_file(os.path.join(case_dir, "expected.json"),
                   json.dumps(expected, indent=2))
        print("\n[retro] rule applied. Running the harness to confirm nothing "
              "regressed...")
        results = run_harness(config)
        regressed = [i for i, r in results.items()
                     if r["outcome"] in ("missed", "false_alarm")]
        if regressed:
            ans = input("\nThe scorecard is not clean after this rule. Type "
                        "'revert' to remove the rule again, anything else to "
                        "keep it: ").strip()
            if ans.lower() == "revert":
                write_file(template_path, backup)
                decisions.append((e, "reverted", changelog))
                print("[retro] rule reverted; the case stays confirmed so the "
                      "miss remains visible.")
                continue
        decisions.append((e, "approved", changelog))

    stamp = time.strftime("%Y-%m-%d")
    lines = [f"\n## Retro {stamp}\n"]
    for e, decision, changelog in decisions:
        lines.append(f"- {e['case_id']}: {decision}")
        if decision == "approved" and changelog:
            lines.append(f"  - Notion changelog line: {changelog}")
    write_file(RETRO_LOG_PATH, read_file(RETRO_LOG_PATH) + "\n".join(lines) + "\n")
    print("\n" + "=" * 70)
    print("RETRO DONE")
    print("=" * 70)
    for e, decision, changelog in decisions:
        print(f"  {decision:9} {e['case_id']}")
        if decision == "approved" and changelog:
            print(f"            paste to the Notion 4b page: {changelog}")
    print("\nCommit runner/templates/ and runner/cases/ so the rules and "
          "scorecard survive this container.")
    _print_usage("retro")


def main():
    ap = argparse.ArgumentParser(description="Sentrada chain runner")
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="run the main pipeline for one piece")
    g.add_argument("--name", required=True, help="recipient full name")
    g.add_argument("--title", required=True, help="recipient job title")
    g.add_argument("--company", required=True, help="recipient company")
    g.add_argument("--format", required=True, help="newspaper, email, or crossword")
    g.add_argument("--email-copy", default="",
                   help="email format only: path to a client-supplied email "
                        "(JSON or .txt) to render verbatim instead of writing copy")
    g.add_argument("--research", help="path to the pasted research file (required unless --resume)")
    g.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    g.add_argument("--brief-only", action="store_true",
                   help="run the brief and stop at the checkpoint (non-interactive approval)")
    g.add_argument("--resume", action="store_true",
                   help="skip the brief; continue from the approved brief.json in the piece folder")
    g.add_argument("--feedback", default="",
                   help="with --brief-only, regenerate the brief incorporating this feedback")
    g.add_argument("--source-signals", default="",
                   help="JSON list of sourcing signals (stamped into meta.json "
                        "so outcomes can be cut by signal source; normally set "
                        "by batch manifests from sourcing/ export)")
    g.add_argument("--delivery-date", default="to be confirmed on delivery",
                   help="delivery date passed to the chained follow-up (placeholder is fine; "
                        "the follow-up is generated and held until delivery is confirmed)")
    g.set_defaults(func=cmd_generate)

    q = sub.add_parser("qc", help="run Prompts 6 and 6B against a final image")
    q.add_argument("--folder", required=True, help="the piece folder")
    q.add_argument("--image", required=True, help="the final image file (PNG or JPG)")
    q.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    q.set_defaults(func=cmd_qc)

    cr = sub.add_parser("cold-review",
                        help="advisory pre-print review: blind cold-eyes read of the "
                             "render (fresh context), strategist pass, and a render "
                             "fact-check against the research. Writes qc_cold.md and "
                             "qc_render_factcheck.md; gates nothing.")
    crg = cr.add_mutually_exclusive_group(required=True)
    crg.add_argument("--folder", help="review a single piece folder")
    crg.add_argument("--manifest", help="review every piece in a batch manifest")
    cr.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    cr.set_defaults(func=cmd_cold_review)

    pq = sub.add_parser("package-qc",
                        help="run the 6B package pass: piece + companion card judged "
                             "as the full send. Use --card-file to import custom card "
                             "copy (plain text, blank-line paragraphs) first.")
    pq.add_argument("--folder", required=True, help="the piece folder")
    pq.add_argument("--image", default="",
                    help="the final image file (defaults to <slug>.png in the folder)")
    pq.add_argument("--card-file", default="",
                    help="plain-text card copy to import as <slug>-card.json "
                         "(custom-card senders); omit to reuse the existing card")
    pq.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    pq.set_defaults(func=cmd_package_qc)

    gp = sub.add_parser("gate-probe",
                        help="regression-test the copy gates by planting known "
                             "violations in a piece's data and asserting they are caught")
    gp.add_argument("--folder", required=True,
                    help="a piece folder with meta.json, data.json and research.md "
                         "whose copy is known-clean (the baseline)")
    gp.add_argument("--lint-only", action="store_true",
                    help="run only the free deterministic lint probes")
    gp.add_argument("--skip-baseline", action="store_true",
                    help="skip the clean-copy baseline check")
    gp.add_argument("--classes", default="",
                    help="comma-separated probe names to run (default: all for the format)")
    gp.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    gp.set_defaults(func=cmd_gate_probe)

    sc = sub.add_parser("ship-check",
                        help="print-readiness gate: hold any piece whose render is newer "
                             "than its QC, missing QC, P6 FAIL, or 6B WOULD BIN. Run before "
                             "staging PNGs for the print supplier. Exits non-zero if any held.")
    scg = sc.add_mutually_exclusive_group(required=True)
    scg.add_argument("--folder", help="check a single piece folder")
    scg.add_argument("--manifest", help="check every piece in a batch manifest")
    scg.add_argument("--all", action="store_true", help="check every piece in runner/pieces/")
    sc.set_defaults(func=cmd_ship_check)

    oc = sub.add_parser("outcome",
                        help="record what happened to a sent piece (feeds 6B "
                             "calibration and the monthly review)")
    oc.add_argument("--slug", required=True, help="the piece folder name, e.g. chris-evans-cognism")
    oc.add_argument("--result", default="", choices=[""] + list(OUTCOME_CHOICES),
                    help="what happened (defaults to no_response until a reply "
                         "changes it)")
    oc.add_argument("--date", default="", help="when it happened, e.g. 2026-07-10")
    oc.add_argument("--company", default="",
                    help="recipient company; needed when the piece folder has "
                         "no meta.json (post-reset recording). The sourcing "
                         "thread-hold check matches holds on this field")
    oc.add_argument("--notes", default="", help="optional context, e.g. 'replied to Touch 2'")
    oc.add_argument("--format", default="", dest="format",
                    help="piece format, when no piece folder remains to read it from")
    oc.add_argument("--angle", default="",
                    help="angle type: problem-tension | achievement-gap | "
                         "open-loop | other")
    oc.add_argument("--signal", default="",
                    help="source signal: live-need-signal | job-change | "
                         "funding-or-announcement | referral-or-community | "
                         "cold-list | other")
    oc.add_argument("--delivered", default="",
                    help="delivery confirmed date, e.g. 2026-07-08")
    oc.add_argument("--first-reply", default="", dest="first_reply",
                    help="date the first reply arrived (P7b stamps this "
                         "automatically when it processes a reply)")
    oc.add_argument("--reply-language", default="", dest="reply_language",
                    choices=[""] + list(REPLY_LANGUAGES),
                    help="P7b classification: gift | problem | mixed | none")
    oc.set_defaults(func=cmd_outcome)

    mr = sub.add_parser("monthly-review",
                        help="compile every piece record and outcome into one "
                             "document and draft doctrine observations "
                             "(proposals only; nothing changes automatically)")
    mr.add_argument("--month", default="",
                    help="label for the review file, e.g. 2026-07 (default: "
                         "the current month)")
    mr.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    mr.set_defaults(func=cmd_monthly_review)

    h = sub.add_parser("harness",
                       help="run the current 4b template over every case in "
                            "runner/cases/ and print the scorecard against the "
                            "last saved run")
    h.add_argument("--only", default="",
                   help="comma-separated case ids to spot-check (does not "
                        "update the saved scorecard)")
    h.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    h.set_defaults(func=cmd_harness)

    fl = sub.add_parser("flag",
                        help="archive a piece's research + copy + your note as "
                             "a new must-flag harness case and log it for the "
                             "next retro")
    fl.add_argument("--slug", required=True,
                    help="the piece folder name (or, with --research/--copy, "
                         "just the id to file the case under)")
    fl.add_argument("--note", required=True,
                    help="what shipped that should have been flagged; put the "
                         "offending phrase in \"quotes\" to seed the match terms")
    fl.add_argument("--violation", default="",
                    help="violation type if you already know it, e.g. "
                         "'STRIPPED QUALIFIER'")
    fl.add_argument("--case-id", default="", dest="case_id",
                    help="case folder name (default: flagged-<slug>)")
    fl.add_argument("--research", default="",
                    help="research file, when the piece folder no longer exists")
    fl.add_argument("--copy", default="",
                    help="gate-visible copy text file, when the piece folder "
                         "no longer exists or has no data.json")
    fl.add_argument("--format", default="", dest="format",
                    help="piece format (needed with --research/--copy; 'email' "
                         "attaches the sender facts)")
    fl.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    fl.set_defaults(func=cmd_flag)

    rt = sub.add_parser("retro",
                        help="gather everything flagged since the last retro, "
                             "draft a candidate rule + exemplar per flag for "
                             "approval, apply approved rules to the 4b template "
                             "and immediately re-run the harness")
    rt.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    rt.set_defaults(func=cmd_retro)

    cal = sub.add_parser("calibration",
                         help="compare 6B predictions against recorded outcomes")
    cal.set_defaults(func=cmd_calibration)

    pr = sub.add_parser("printed",
                        help="mark a piece printed in the capture ledger "
                             "(tease refuses to run without it)")
    pr.add_argument("--piece", required=True, help="the piece slug, e.g. jane-doe-acme")
    pr.add_argument("--name", default="",
                    help="recipient full name (needed when the piece folder is "
                         "not in this container)")
    pr.set_defaults(func=cmd_printed)

    te = sub.add_parser("tease",
                        help="generate the one-off address-capture link for a printed "
                             "piece and mark it tease-sent")
    te.add_argument("--piece", required=True, help="the piece slug")
    te.add_argument("--channel", required=True, choices=list(TEASE_CHANNELS),
                    help="where the tease goes out")
    te.add_argument("--variant", required=True, choices=list(TEASE_VARIANTS),
                    help="mystery (text only) or teaser (with the corner image)")
    te.add_argument("--again", action="store_true",
                    help="rotate the token for a piece that already has a live tease")
    te.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    te.set_defaults(func=cmd_tease)

    tz = sub.add_parser("teaser",
                        help="crop a corner of the piece's render as a small inline "
                             "jpg for the teaser-image variant")
    tz.add_argument("--piece", required=True, help="the piece slug")
    tz.add_argument("--corner", default="tl", choices=["tl", "tr", "bl", "br"],
                    help="which corner to crop (default tl; pick another if the "
                         "default shows too much or too little)")
    tz.add_argument("--image", default="",
                    help="path to the render (default: the piece folder's <slug>.png)")
    tz.add_argument("--width", type=int, default=1200,
                    help="longest side of the output jpg in px (default 1200)")
    tz.add_argument("--out", default="", help="output path (default: <slug>-teaser.jpg "
                                              "beside the render)")
    tz.set_defaults(func=cmd_teaser)

    nu = sub.add_parser("nudge", help="record that the day-5 nudge was sent")
    nu.add_argument("--piece", required=True, help="the piece slug")
    nu.set_defaults(func=cmd_nudge)

    sw = sub.add_parser("swap",
                        help="mark a silent piece swapped-after-silence (your decision; "
                             "the runner only ever recommends)")
    sw.add_argument("--piece", required=True, help="the piece slug")
    sw.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    sw.set_defaults(func=cmd_swap)

    ad = sub.add_parser("address",
                        help="pull a submitted delivery address from the capture store "
                             "and print it (terminal only; never written to disk)")
    ad.add_argument("--piece", required=True, help="the piece slug")
    ad.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    ad.set_defaults(func=cmd_address)

    dl = sub.add_parser("delivered",
                        help="mark a piece delivered and delete its address from the "
                             "capture store (the deletion the page promises)")
    dl.add_argument("--piece", required=True, help="the piece slug")
    dl.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    dl.set_defaults(func=cmd_delivered)

    cp = sub.add_parser("capture",
                        help="address-capture status: sync submissions from the store, "
                             "show per-piece statuses, nudge-due / swap-recommended "
                             "flags, and the share rate by variant")
    cp.add_argument("--offline", action="store_true",
                    help="skip the store poll; show the committed ledger only")
    cp.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    cp.set_defaults(func=cmd_capture)

    cpr = sub.add_parser("capture-probe",
                         help="regression-test the address-capture flow end to end "
                              "against a local harness running the real api/ "
                              "functions; run after any edit to api/ or the "
                              "capture commands")
    cpr.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    cpr.set_defaults(func=cmd_capture_probe)

    ep = sub.add_parser("engine-probe",
                        help="regression-test the layout engines' render contract "
                             "(--check refusal, print size/DPI/sRGB, *.FAILED "
                             "quarantine, deterministic re-render) plus the "
                             "runner's pure logic and snapshot idempotence; free "
                             "(no model calls), run after any engine or runner edit")
    ep.add_argument("--engines", default="",
                    help="comma-separated subset (newspaper,crossword,email,card)")
    ep.add_argument("--skip-render", action="store_true",
                    help="contract checks only, no renders (fast)")
    ep.add_argument("--dpi", type=int, default=96,
                    help="probe render DPI (default 96 for speed; 360 = full print size)")
    ep.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    ep.set_defaults(func=cmd_engine_probe)

    qh = sub.add_parser("qc-harness",
                        help="the vision-QC exam: run the CURRENT P6/6B templates "
                             "over runner/qc_cases/ (deterministic renders, some "
                             "deliberately defaced) and assert the verdict "
                             "constraints; one vision call per prompt per case. "
                             "Run after any edit to prompt6/prompt6b")
    qh.add_argument("--cases", default="",
                    help="comma-separated case ids to run (default: all)")
    qh.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    qh.set_defaults(func=cmd_qc_harness)

    bc = sub.add_parser("birch-csv",
                        help="emit the print supplier's shipping CSV (code, recipient, "
                             "company, address, notes, file_stem) from a manifest. Writes "
                             "beside the manifest; holds addresses, so never commit it.")
    bc.add_argument("--manifest", required=True, help="path to the JSON manifest")
    bc.add_argument("--prefix", default="CP", help="code prefix (default CP)")
    bc.add_argument("--start", type=int, default=1, help="first code number (default 1)")
    bc.add_argument("--output", help="CSV path (default: <manifest>-birch.csv beside the manifest)")
    bc.add_argument("--stage-pngs", action="store_true",
                    help="also copy each delivered PNG to <file_stem>.png in a local folder beside the CSV")
    bc.set_defaults(func=cmd_birch_csv)

    # P7b lives in reply.py beside this file; the parser arguments are defined
    # there once and shared, so the two entry points cannot drift.
    import reply as _reply_module
    rp = sub.add_parser("reply",
                        help="P7b: classify an inbound reply, draft the "
                             "response (classify -> draft -> grounding gate). "
                             "Never sends; you copy, edit and send")
    _reply_module.add_reply_arguments(rp)
    rp.set_defaults(func=cmd_reply)

    f = sub.add_parser("followup", help="run Prompt 7 to write the follow-up sequence")
    f.add_argument("--folder", required=True, help="the piece folder")
    f.add_argument("--delivery-date", required=True, help="delivery date, e.g. '16 June 2026'")
    f.add_argument("--no-card", action="store_true",
                   help="do not regenerate the card or package pass (use for pieces "
                        "whose card has already shipped; touches only)")
    f.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    f.set_defaults(func=cmd_followup)

    bb = sub.add_parser("batch-brief",
                        help="phase 1: run the brief for every recipient in a manifest")
    bb.add_argument("--manifest", required=True, help="path to the JSON manifest")
    bb.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    bb.set_defaults(func=cmd_batch_brief)

    bd = sub.add_parser("batch-build",
                        help="phase 2: build every APPROVEd piece from the manifest")
    bd.add_argument("--manifest", required=True, help="path to the JSON manifest")
    bd.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    bd.add_argument("--ignore-stale-gates", action="store_true",
                    dest="ignore_stale_gates",
                    help="skip the warning when a gate template is newer than "
                         "the last harness run (non-interactive use)")
    bd.add_argument("--no-snapshot", action="store_true", dest="no_snapshot",
                    help="skip auto-pushing built renders to the deliverables "
                         "branch (they will not survive a container restart)")
    bd.set_defaults(func=cmd_batch_build)

    sn = sub.add_parser("snapshot",
                        help="push a batch's render + card PNGs to the durable "
                             "deliverables branch (survives container restarts)")
    sn.add_argument("--manifest", help="path to the JSON manifest")
    sn.add_argument("--folder", help="a single runner/pieces/<slug> folder")
    sn.add_argument("--batch-label", dest="batch_label",
                    help="batch label, e.g. 2026-07-09 (derived from the "
                         "manifest name when omitted; required with --folder)")
    sn.add_argument("--no-push", action="store_true", dest="no_push",
                    help="stage into the worktree without pushing (testing)")
    sn.set_defaults(func=cmd_snapshot)

    rs = sub.add_parser("restore",
                        help="pull a batch's render + card PNGs back from the "
                             "deliverables branch after a container restart")
    rs.add_argument("--manifest", help="path to the JSON manifest")
    rs.add_argument("--batch-label", dest="batch_label",
                    help="batch label, e.g. 2026-07-09 (or use --manifest)")
    rs.add_argument("--all", action="store_true",
                    help="restore every batch on the deliverables branch "
                         "(used by the session-start self-heal hook)")
    rs.set_defaults(func=cmd_restore)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
