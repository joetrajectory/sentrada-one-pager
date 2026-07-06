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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import tempfile

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
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


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


def _stamp_engine(output_path, engine_path):
    """Record which engine build produced a render (engine_stamp.json beside it).
    ship-check compares this against the current engine file and warns when a
    render predates an engine change — the lesson of the MongoDB dateline, where a
    fixed engine bug shipped inside a stale render."""
    try:
        folder = os.path.dirname(os.path.abspath(output_path))
        write_file(os.path.join(folder, "engine_stamp.json"), json.dumps({
            "engine": os.path.relpath(engine_path, REPO_ROOT),
            "sha1": _engine_file_sha1(engine_path),
        }, indent=2))
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
        # The edition line's city list reads as the company's real locations, so
        # the grounding gate must see it (a fabricated city once shipped inside
        # the "fictional furniture" blind spot). The masthead stays out: it is
        # furniture by design and P4b is told never to flag it.
        "EDITION LINE: " + data.get("edition_line", ""),
        "HEADLINE: " + data.get("headline", ""),
        "STAT: " + data.get("stat_number", "") + " " + data.get("stat_descriptor", ""),
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
    grounded = bool(data.get("grounded", True)) and not issues
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
    return {
        "recipient_name": args.name, "recipient_title": args.title,
        "recipient_company": args.company, "format": fmt,
        "problem_label": brief.get("problem_label", ""),
        "companion_card_hook": brief.get("companion_card_hook", ""),
        "sender": sender,
    }


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
        verdict = "NOT_READY" if structural else "READY"
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
        research = read_file(os.path.join(folder, "research.md"))
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
        return
    slug = os.path.basename(os.path.normpath(folder))
    data_path = os.path.join(folder, f"{slug}-card.json")
    card_png = os.path.join(folder, f"{slug}-card.png")
    write_file(data_path, json.dumps(card_engine_data(body, first, sender), indent=2))
    print(f"\n[card] step 8b: rendering the A6 companion card ({len(body)} paragraphs)...")
    rc, out = run_card_engine(config, engine, data_path, check=True)
    if rc != 0:
        print("[card] WITHHELD: copy overflows A6 even at the compact tier. Trim the "
              "card under 150 words and re-run. Engine output:\n" + out.strip())
        return
    rc, out = run_card_engine(config, engine, data_path, card_png)
    if rc != 0:
        print("[card] render failed:\n" + out.strip())
        return
    print(f"[card] rendered: {os.path.relpath(card_png, REPO_ROOT)}")


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
    _generate_card(config, folder, reply)        # step 8b: companion card
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

def extract_6b_verdict(text):
    for phrase in ("WOULD TAKE THE MEETING", "WOULD ENGAGE IF FOLLOWED UP WELL",
                   "WOULD ADMIRE AND IGNORE", "WOULD BIN"):
        if phrase in text:
            return phrase
    return None


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
    return review, extract_p6_verdict(review)


def _p6b(config, folder, image_path, meta, research):
    # The card sits on top of the piece in the box and is read first, so the
    # simulation should see it when it exists (it shapes the verdict P7 builds on).
    slug = os.path.basename(os.path.normpath(folder))
    card_context = ""
    card_path = os.path.join(folder, slug + "-card.json")
    if os.path.exists(card_path):
        try:
            card = json.loads(read_file(card_path))
            lines = [card.get("salutation", "").rstrip(",") + ","] + list(card.get("body", []))
            card_text = "\n".join(x for x in lines if x.strip())
            if card_text.strip():
                card_context = ("\nOn top of the piece sat an A6 companion card, which you "
                                "read first. It says:\n\n" + card_text + "\n")
        except (ValueError, OSError):
            pass
    sender = (config.get("sender") or meta.get("sender") or {})
    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "research": research,
        "card_context": card_context,
        "sender_company": sender.get("company", "unknown"),
        "sender_what": sender.get("what_they_sell", ""),
    }
    print(f"[prompt 6B] simulating the recipient ({model_for(config, 'p6b')}, vision)...")
    sixb = vision_call(config, fill(load_template("prompt6b_recipient.md"), values),
                       model_for(config, "p6b"), image_path)
    write_file(os.path.join(folder, "qc_recipient.md"), sixb)
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
    print("\n" + "=" * 70)
    print("QC COMPLETE")
    print("=" * 70)
    print("  qc_review.md     <- Prompt 6 craft review")
    print("  qc_recipient.md  <- Prompt 6B recipient simulation")
    print(f"\nPrompt 6 verdict:  {v6 or '(see qc_review.md)'}")
    print(f"Prompt 6B verdict: {v6b or '(see qc_recipient.md)'}")
    _print_usage("qc")


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
        if data:
            strings = list(_strings_in(data))
            if any("—" in s for s in strings):
                holds.append("lint: em dash in data.json copy (house rule: none)")
            if any("!" in s for s in strings):
                holds.append("lint: exclamation mark in data.json copy (house rule: none)")
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
                "holds": [], "warns": ["claymation is prompt-only; nothing to print-check"]}

    holds, warns, p6, p6b = [], [], None, None
    if not os.path.exists(folder):
        return {"slug": slug, "format": fmt, "shippable": False, "p6": None, "p6b": None,
                "holds": ["piece folder not found (build it first)"], "warns": []}
    if not os.path.exists(render):
        return {"slug": slug, "format": fmt, "shippable": False, "p6": None, "p6b": None,
                "holds": [f"no render found ({slug}.png)"], "warns": []}

    png_m = os.path.getmtime(render)
    if not os.path.exists(review):
        holds.append("no qc_review.md — P6 craft QC never ran")
    else:
        p6 = extract_p6_verdict(read_file(review))
        if os.path.getmtime(review) < png_m:
            holds.append("render is NEWER than qc_review.md — re-QC required "
                         "(P6 has not seen the delivered file)")
        if p6 == "FAIL":
            holds.append("P6 verdict is FAIL")
        elif p6 == "BORDERLINE":
            warns.append("P6 verdict is BORDERLINE — review craft notes before print")

    if not os.path.exists(recipient):
        holds.append("no qc_recipient.md — P6B recipient sim never ran")
    else:
        p6b = extract_6b_verdict(read_file(recipient))
        if os.path.getmtime(recipient) < png_m:
            holds.append("render is NEWER than qc_recipient.md — re-QC required "
                         "(P6B has not seen the delivered file)")
        if p6b == "WOULD BIN":
            holds.append("P6B verdict is WOULD BIN — do not print")

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
            ep = os.path.join(REPO_ROOT, stamp.get("engine", ""))
            if os.path.exists(ep) and _engine_file_sha1(ep) != stamp.get("sha1"):
                warns.append("engine has changed since this render — re-render "
                             "recommended (delivered file may predate an engine fix)")
        except (ValueError, OSError):
            pass

    return {"slug": slug, "format": fmt, "shippable": not holds,
            "holds": holds, "warns": warns, "p6": p6, "p6b": p6b}


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

    print("\n" + "=" * 70)
    print("SHIP CHECK — print-readiness gate")
    print("=" * 70)
    held = 0
    for s in statuses:
        mark = "n/a " if s["shippable"] is None else ("SHIP" if s["shippable"] else "HOLD")
        if s["shippable"] is False:
            held += 1
        verds = [v for v in (f"P6 {s['p6']}" if s["p6"] else "",
                             f"6B {s['p6b']}" if s["p6b"] else "") if v]
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
# OVERRIDE the research (a human correction):
#   {"name": "...", "company": "...", "format": "...",
#    "delivery": {"address": "Full address on one line", "notes": ""}}

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
        status = (res.get("status") or "").upper()
        address = (override.get("address") or res.get("address") or "").strip()
        notes = (override.get("notes") or res.get("notes") or "").strip()
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
        if not address and not flagged:
            missing.append(slug)
        file_stem = f"{code}_{_surname(recipient)}_{_company_condensed(company)}"
        rows.append({"code": code, "recipient": recipient, "company": company,
                     "delivery_address": address, "notes": notes,
                     "file_stem": file_stem, "slug": slug})

    out = args.output or os.path.join(
        mdir, os.path.splitext(os.path.basename(args.manifest))[0] + "-birch.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "recipient", "company", "delivery_address", "notes", "file_stem"])
        for r in rows:
            w.writerow([r["code"], r["recipient"], r["company"],
                        r["delivery_address"], r["notes"], r["file_stem"]])

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


def cmd_followup(args):
    _usage_reset()
    config = load_config(args.config)
    reply = _p7(config, args.folder, args.delivery_date)
    print("\n" + "=" * 70)
    print("FOLLOW-UP WRITTEN")
    print("=" * 70)
    print("  followup.md  <- companion card + Touch 1-3 + reception nudge + fact check")
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
    sixb_path = os.path.join(folder, "qc_recipient.md")
    if os.path.exists(sixb_path):
        v = extract_6b_verdict(read_file(sixb_path))
        if v:
            rec["verdict_6b"] = v
    rec["result"] = args.result
    if args.date:
        rec["date"] = args.date
    if args.notes:
        rec["notes"] = args.notes
    ledger[args.slug] = rec
    write_file(OUTCOMES_PATH, json.dumps(ledger, indent=2))
    print(f"[outcome] {args.slug}: {args.result}"
          + (f"  (6B predicted: {rec.get('verdict_6b')})" if rec.get("verdict_6b") else ""))
    print(f"[outcome] ledger updated: {os.path.relpath(OUTCOMES_PATH, REPO_ROOT)} "
          "— commit and push it so the record survives this container")


def cmd_calibration(args):
    positive = {"replied", "meeting", "opportunity"}
    ledger = json.loads(read_file(OUTCOMES_PATH)) if os.path.exists(OUTCOMES_PATH) else {}
    rows = [(slug, rec.get("verdict_6b", "?"), rec.get("result", "?"))
            for slug, rec in sorted(ledger.items()) if rec.get("result")]
    pending = []
    if os.path.isdir(PIECES_DIR):
        for d in sorted(os.listdir(PIECES_DIR)):
            if d.startswith("_") or d in ledger:
                continue
            sixb_path = os.path.join(PIECES_DIR, d, "qc_recipient.md")
            if os.path.exists(sixb_path):
                pending.append((d, extract_6b_verdict(read_file(sixb_path)) or "?"))

    print("\n" + "=" * 70)
    print("6B CALIBRATION — prediction vs outcome")
    print("=" * 70)
    if not rows:
        print("No outcomes recorded yet. Record one with:")
        print("  python runner/sentrada_runner.py outcome --slug <slug> "
              "--result replied|meeting|opportunity|no_response|bounced")
    for d, v, res in rows:
        hit = "✓" if ((res in positive) == ("WOULD" in v and "BIN" not in v and "IGNORE" not in v)) else "✗"
        print(f"  {hit}  {d:38} 6B: {v:38} actual: {res}")
    if rows:
        got = sum(1 for _, _, res in rows if res in positive)
        print("-" * 70)
        print(f"  {len(rows)} outcome(s) recorded, {got} positive "
              f"({100 * got // len(rows)}% response rate)")
        print("  When ~30 outcomes exist, feed the misses back into prompt6b as "
              "calibration examples.")
    if pending:
        print(f"\n  {len(pending)} piece(s) with a 6B prediction and no outcome yet:")
        for d, v in pending:
            print(f"     {d:38} 6B: {v}")


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


# --- CLI --------------------------------------------------------------------

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
    g.add_argument("--delivery-date", default="to be confirmed on delivery",
                   help="delivery date passed to the chained follow-up (placeholder is fine; "
                        "the follow-up is generated and held until delivery is confirmed)")
    g.set_defaults(func=cmd_generate)

    q = sub.add_parser("qc", help="run Prompts 6 and 6B against a final image")
    q.add_argument("--folder", required=True, help="the piece folder")
    q.add_argument("--image", required=True, help="the final image file (PNG or JPG)")
    q.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    q.set_defaults(func=cmd_qc)

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
                        help="record what happened to a sent piece (feeds 6B calibration)")
    oc.add_argument("--slug", required=True, help="the piece folder name, e.g. chris-evans-cognism")
    oc.add_argument("--result", required=True, choices=list(OUTCOME_CHOICES))
    oc.add_argument("--date", default="", help="when it happened, e.g. 2026-07-10")
    oc.add_argument("--notes", default="", help="optional context, e.g. 'replied to Touch 2'")
    oc.set_defaults(func=cmd_outcome)

    cal = sub.add_parser("calibration",
                         help="compare 6B predictions against recorded outcomes")
    cal.set_defaults(func=cmd_calibration)

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

    f = sub.add_parser("followup", help="run Prompt 7 to write the follow-up sequence")
    f.add_argument("--folder", required=True, help="the piece folder")
    f.add_argument("--delivery-date", required=True, help="delivery date, e.g. '16 June 2026'")
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
    bd.set_defaults(func=cmd_batch_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
