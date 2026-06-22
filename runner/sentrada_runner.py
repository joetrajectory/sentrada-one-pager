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
"""

import argparse
import base64
import json
import os
import re
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
        return fh.read()


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


def run_engine(engine_path, template_path, data_path, output_path=None, check=False, dpi=360):
    cmd = [sys.executable, engine_path, "--template", template_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", str(dpi)]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
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
            }
            for c in (data.get("candidates") or [])
        ],
    }


def run_crossword_engine(engine_path, template_path, data_path, output_path=None,
                         check=False, dpi=360):
    """Render or validate the crossword. crossword.py mirrors newspaper.py:
    --check validates (and exits nonzero on FAIL) without writing output;
    --output renders the master at the given DPI. Same exit-code contract."""
    cmd = [sys.executable, engine_path, "--template", template_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", str(dpi)]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


# --- The Email gates and engine --------------------------------------------
# Parallel to the newspaper/crossword helpers. email.py has NO template image
# (the Gmail chrome is drawn procedurally), so its run helper omits --template.
# Two copy sources: authored (P4 writes the cold email from the brief) or
# client-supplied (rendered verbatim, fit-checked only). Both feed one schema.

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


def email_engine_data(data, args, sender, config):
    """Assemble the engine's --data from the email copy plus the sender identity
    (config) and recipient (args). The sender signs the piece, so name/email/
    avatar always come from config, never from the model."""
    sender_name = sender.get("sender_name") or sender.get("company", "Sentrada")
    body = []
    for b in (data.get("body") or []):
        t = b.get("type", "p")
        if t == "signature":
            lines = b.get("lines") or [x for x in (b.get("name"), b.get("detail")) if x]
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
            # optional sender logo/photo, clipped to the avatar disc if set
            **({"avatar_image": sender["avatar_image"]}
               if sender.get("avatar_image") else {}),
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


def run_email_engine(engine_path, data_path, output_path=None, check=False, dpi=360):
    """Render or validate The Email. email.py has no --template (procedural,
    fully vector/type chrome), so it renders straight to an exact A2 at the given
    DPI with no upscale step. Same exit-code contract as the other engines:
    --check validates and exits nonzero on FAIL; --output renders the master."""
    cmd = [sys.executable, engine_path, "--data", data_path]
    if check:
        cmd.append("--check")
    if output_path:
        cmd += ["--output", output_path, "--print-dpi", str(dpi)]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


# --- Factual grounding gate (text, not vision) -----------------------------
# Catches fabricated/contradicted claims in the copy before a render exists.
# More reliable than the post-render vision review for verifying text claims.

def newspaper_copy_text(data):
    parts = [
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


def grounding_check(config, research, copy_text):
    """Return (grounded: bool, issues: list of {claim, issue})."""
    prompt = fill(load_template("prompt4b_grounding.md"),
                  {"research": research, "copy_text": copy_text})
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
    elif fmt == "crossword":
        _generate_crossword(args, config, folder, base_values, brief, meta)
    elif fmt == "email":
        _generate_email(args, config, folder, base_values, brief, meta)
    else:
        _generate_claymation(args, config, folder, base_values, brief, meta)


def fmt_of(args):
    return args.format.lower()


def cmd_generate(args):
    _usage_reset()
    config = load_config(args.config)
    sender = config["sender"]
    fmt = args.format.lower()
    if fmt not in ("newspaper", "claymation", "crossword", "email"):
        die("format must be 'newspaper', 'claymation', 'crossword', or 'email'.")

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
        if attempt >= 2:
            die("Prompt 4 newspaper failed the gates twice (word count and/or "
                "factual grounding):\n  - " + "\n  - ".join(problems))
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
    rc, out = run_engine(engine_path, template_path, data_path, output_path=output_path,
                         dpi=config.get("print_dpi", 360))
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


def _generate_crossword(args, config, folder, base_values, brief, meta):
    """Crossword path. Mirrors _generate_newspaper: P4 (crossword branch) ->
    structural + factual-grounding gates -> render via the crossword engine ->
    the shared P6 -> P6B -> P7 chain. Nothing here touches the newspaper path."""
    template = load_template("prompt4_copy_crossword.md")
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
    data_path = os.path.join(folder, "data.json")
    engine_path = os.path.join(REPO_ROOT, config.get("crossword_engine", "crossword/crossword.py"))
    template_path = os.path.join(
        REPO_ROOT, config.get("crossword_template", "crossword/crossword_template.png"))
    if not os.path.exists(engine_path):
        die(f"crossword engine not found at {engine_path}. The engine ships separately; "
            f"place crossword/crossword.py in the repo, or set 'crossword_engine' in "
            f"config.json.")
    if not os.path.exists(template_path):
        die(f"crossword template not found at {template_path}. Place "
            f"crossword/crossword_template.png in the repo or set 'crossword_template' "
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
            write_file(data_path, json.dumps(engine_data, indent=2))
            print("[engine] running --check on data.json (grid placement + clue fit)...")
            rc, out = run_crossword_engine(engine_path, template_path, data_path, check=True)
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
                problems.append("An anchor answer could not be placed. Use at most one "
                                "anchor, or choose a shorter, more letter-friendly anchor "
                                "(common letters E S T A R N I O intersect best).")

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
    rc, out = run_crossword_engine(engine_path, template_path, data_path, output_path=output_path,
                                   dpi=config.get("print_dpi", 360))
    print(out.strip())
    if rc != 0 or not os.path.exists(output_path):
        die("the crossword render failed (see engine output above).")

    meta["piece_reference"] = (
        f'the "{args.company}" crossword, subtitle "{engine_data.get("subtitle", "")}"')
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
        attempt, feedback_block = 0, ""
        while True:
            attempt += 1
            prompt = fill(template, dict(brief_values, feedback_block=feedback_block))
            print(f"\n[prompt 4] writing the cold email ({model}, attempt {attempt})...")
            reply, data = cli_json(prompt, model)
            copy_part, factcheck = split_factcheck(reply)

            problems = list(email_violations(data))
            print(f"[prompt 4b] factual grounding check ({model_for(config, 'p4b')})...")
            grounded, issues = grounding_check(config, research, email_copy_text(data))
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
    rc, out = run_email_engine(engine_path, data_path, output_path=output_path,
                               dpi=config.get("email_print_dpi", 360))
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
    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "research": research,
    }
    print(f"[prompt 6B] simulating the recipient ({model_for(config, 'p6b')}, vision)...")
    sixb = vision_call(config, fill(load_template("prompt6b_recipient.md"), values),
                       model_for(config, "p6b"), image_path)
    write_file(os.path.join(folder, "qc_recipient.md"), sixb)
    return sixb, extract_6b_verdict(sixb)


def _p7(config, folder, delivery_date):
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    brief = json.loads(read_file(os.path.join(folder, "brief.json")))
    research = read_file(os.path.join(folder, "research.md"))
    sender = meta["sender"]

    sixb_path = os.path.join(folder, "qc_recipient.md")
    if os.path.exists(sixb_path):
        sixb = read_file(sixb_path)
        verdict = extract_6b_verdict(sixb) or "WOULD ENGAGE IF FOLLOWED UP WELL"
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
    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "format": fmt_label,
        "piece_reference": meta.get("piece_reference", ""),
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
    }
    print(f"[prompt 7] writing the companion card and 3-touch follow-up ({model_for(config, 'p7')})...")
    reply = cli_text(fill(load_template("prompt7_followup.md"), values), model_for(config, "p7"))
    write_file(os.path.join(folder, "followup.md"), reply)
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
        if e["format"].lower() not in ("newspaper", "claymation", "crossword"):
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
        if brief and brief.get("fit") == "poor":
            rows.append({"slug": slug, "entry": entry, "status": "poor-fit",
                         "brief": brief, "credit": credit})
            print(f"  POOR FIT (${credit:.2f}) -> defaults to SKIP")
        elif rc == 0 and brief:
            rows.append({"slug": slug, "entry": entry, "status": "ok",
                         "brief": brief, "credit": credit})
            print(f"  ok: {brief.get('problem_label', '')} (${credit:.2f})")
        else:
            rows.append({"slug": slug, "entry": entry, "status": "error",
                         "reason": _last_halt_reason(out, err), "credit": credit})
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
            "qc_recipient.md, followup.md). Held pieces: read the reason before re-running."]
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
    g.add_argument("--format", required=True,
                   help="newspaper, claymation, crossword, or email")
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
