#!/usr/bin/env python3
"""Sentrada P7b: reply handler.

Drafts the sender's response when a recipient replies to a delivered piece.
It NEVER sends anything. The operator copies, edits and sends.

Usage:
    python reply.py <piece-id-or-path> [--channel email|linkedin]
                    [--reply-file path] [--reply-date YYYY-MM-DD]
                    [--touches-sent 1,2] [--pieces-dir path]

Flow:
    1. classify  (Sonnet)   reply language + intent
    2. draft     (Opus)     branch by classification, house rules injected
    3. gate      (Opus)     4b grounding logic, fact errors only, + house rules
                            one failure regenerates, a second HALTs

Runs as `python runner/sentrada_runner.py reply ...` (or directly). All model
calls go through the chain runner's `claude -p` plumbing: same retries, same
usage accounting, same subscription auth.
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- integration
# reply.py lives beside sentrada_runner.py and uses its plumbing directly.
# Model keys follow the runner convention; override in config.json "models".

import sentrada_runner as _runner

RUNNER_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = RUNNER_DIR / "templates"

DEFAULT_MODELS = {
    "p7b_classify": "sonnet",
    "p7b_draft": "opus",   # the best drafting model available to the CLI;
                           # point config models.p7b_draft at a stronger alias
                           # (e.g. "fable") while one is available
    "p7b_gate": "opus",    # same tier as the chain's 4b gate
}


def load_config():
    return _runner.load_config(str(RUNNER_DIR / "config.json"))


def call_model(prompt, model_key, cfg):
    models = dict(DEFAULT_MODELS)
    models.update(cfg.get("models", {}))
    model = models.get(model_key, DEFAULT_MODELS[model_key])
    return _runner.cli_text(prompt, model)


# ------------------------------------------------------------------ utilities


def die(msg):
    print("HALT: " + msg, file=sys.stderr)
    sys.exit(1)


def read_if_exists(piece, names):
    for name in names:
        p = piece / name
        if p.exists():
            return p.read_text()
    return None


def extract_json(text):
    """Return the last valid JSON object in a model response."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = fenced if fenced else []
    if not candidates:
        dec = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, _ = dec.raw_decode(text[m.start():])
                candidates.append(json.dumps(obj))
            except ValueError:
                continue
    for c in reversed(candidates):
        try:
            return json.loads(c)
        except ValueError:
            continue
    raise ValueError("No JSON object found in model output:\n" + text[:2000])


def fill(template_name, mapping):
    path = TEMPLATE_DIR / template_name
    if not path.exists():
        die("Missing template: %s" % path)
    text = path.read_text()
    for key, value in mapping.items():
        text = text.replace("{{%s}}" % key, value if value else "(none)")
    leftover = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)))
    for key in leftover:
        text = text.replace("{{%s}}" % key, "(none)")
    return text


# --------------------------------------------------------------- piece record


def resolve_piece(piece_arg, cfg):
    p = Path(piece_arg)
    if p.is_dir():
        return p
    roots = ([Path(cfg["pieces_dir"])] if cfg.get("pieces_dir")
             else [Path(_runner.PIECES_DIR)])
    matches = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and piece_arg.lower() in child.name.lower():
                if child.resolve() not in [m.resolve() for m in matches]:
                    matches.append(child)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        die("No piece folder matching '%s' found." % piece_arg)
    die(
        "Piece id '%s' is ambiguous: %s"
        % (piece_arg, ", ".join(str(m) for m in matches))
    )


def load_piece(piece):
    record = {}
    record["research"] = read_if_exists(piece, ["research.md"])
    record["brief"] = read_if_exists(piece, ["brief.md", "brief.json"])
    record["copy"] = read_if_exists(piece, ["copy.md", "data.json", "copy.txt"])
    record["factcheck"] = read_if_exists(piece, ["factcheck.md"])
    record["sim"] = read_if_exists(piece, ["qc_package.md", "qc_recipient.md"])
    record["followup"] = read_if_exists(piece, ["followup.md"])
    meta_text = read_if_exists(piece, ["meta.json"])
    record["meta"] = json.loads(meta_text) if meta_text else {}

    if not record["research"]:
        die(
            "No research.md in %s. The grounding gate cannot run without the "
            "research; a reply draft that cannot be fact-checked must not be "
            "generated." % piece
        )
    if not record["factcheck"]:
        print(
            "WARNING: no factcheck.md found. Gate will check against the "
            "research alone.",
            file=sys.stderr,
        )
    for optional in ("brief", "copy", "sim", "followup"):
        if not record[optional]:
            print("WARNING: no %s file found in piece record." % optional,
                  file=sys.stderr)
    return record


def touches_sent_summary(meta, override):
    if override:
        nums = [n.strip() for n in override.split(",") if n.strip()]
        return ", ".join("Touch %s sent" % n for n in nums) or "None sent"
    parts = []
    for i in (1, 2, 3):
        for key in ("touch%d_sent_date" % i, "touch%d_sent" % i):
            if meta.get(key):
                parts.append("Touch %d sent %s" % (i, meta[key]))
                break
        else:
            parts.append("Touch %d not sent" % i)
    return "; ".join(parts)


def open_loop_summary(meta, touches_override):
    ol = meta.get("open_loop")
    if not ol or ol in ("none", "None"):
        if meta.get("open_loop_fallback"):
            return "(open-loop placement fell back on this piece; no record)"
        return None
    touch1_sent = bool(
        meta.get("touch1_sent_date") or meta.get("touch1_sent")
        or (touches_override and "1" in touches_override)
    )
    lines = [
        "Grid answer: %s" % ol.get("answer", ""),
        "Printed position: %s %s" % (ol.get("clue_number", "?"),
                                     ol.get("direction", "")),
        "Clue: %s" % ol.get("clue", ""),
        "Withheld metric: %s" % ol.get("metric", ""),
        "Question it answers: %s" % ol.get("question", ""),
        "Tier: %s" % ol.get("tier", ""),
    ]
    if ol.get("tier") == "A" and ol.get("tier_A_number") is not None:
        lines.append("Tier A number: %s" % ol.get("tier_A_number"))
    if touch1_sent:
        lines.append(
            "NOTE: Touch 1 has been sent, so the reveal/offer has already "
            "been deployed. Reference it, do not re-explain the mechanic."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- gating

GENERIC_OPENERS = [
    "quick follow", "following up", "just following", "checking in",
    "circling back", "touching base", "bumping this", "just wanted to",
    "hope you are well", "hope you're well", "i hope this finds",
    "thanks for getting back", "great to hear from you",
]


def lint_draft(draft, channel):
    """Deterministic house-rule checks. Returns a list of violation dicts."""
    v = []
    if "—" in draft:
        v.append({"type": "HOUSE RULE", "issue": "Em dash in draft."})
    if "!" in draft:
        v.append({"type": "HOUSE RULE", "issue": "Exclamation mark in draft."})
    if re.search(r"(?im)^\s*subject\s*:", draft):
        v.append({"type": "HOUSE RULE",
                  "issue": "Subject line on a reply. Replies never carry one."})
    if re.search(r"\bgift\b", draft, re.I):
        v.append({"type": "HOUSE RULE",
                  "issue": "The word 'gift' is banned in Sentrada copy."})
    body = draft.strip()
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if lines:
        first_body = lines[1] if (len(lines) > 1 and lines[0].endswith(","
                                  ) and len(lines[0]) < 40) else lines[0]
        low = first_body.lower()
        for opener in GENERIC_OPENERS:
            if low.startswith(opener) or low.startswith("just " + opener):
                v.append({"type": "HOUSE RULE",
                          "issue": "Generic opener: '%s...'" % first_body[:40]})
                break
    words = len(re.findall(r"\b[\w'&£$%.-]+\b", body))
    limit = 60 if channel == "linkedin" else 130
    if words > limit:
        v.append({"type": "HOUSE RULE",
                  "issue": "Too long for a %s reply: %d words (limit %d). "
                           "Replies are shorter than first touches."
                           % (channel, words, limit)})
    return v


def run_gate(draft, record, sender_facts, cfg, channel):
    violations = lint_draft(draft, channel)
    prompt = fill("prompt7b_reply_gate.md", {
        "research": record["research"],
        "factcheck": record["factcheck"] or "(none)",
        "sender_facts": sender_facts,
        "draft": draft,
    })
    verdict = extract_json(call_model(prompt, "p7b_gate", cfg))
    for item in verdict.get("violations", []) or []:
        violations.append({
            "type": item.get("type", "UNSUPPORTED CLAIM"),
            "issue": item.get("issue", ""),
            "claim": item.get("claim", ""),
        })
    return violations


# ----------------------------------------------------------------------- main


def format_sender_facts(cfg):
    sender = cfg.get("sender", {})
    if not sender:
        return "(none provided)"
    keys = ("name", "company", "what_they_sell", "proof_points",
            "measurement_capabilities", "booking_link")
    lines = []
    for k in keys:
        if sender.get(k):
            val = sender[k]
            if isinstance(val, list):
                val = "; ".join(str(x) for x in val)
            lines.append("%s: %s" % (k, val))
    return "\n".join(lines) or "(none provided)"


def read_reply_text(args):
    if args.reply_file:
        return Path(args.reply_file).read_text().strip()
    print(
        "Paste the reply (or the whole thread, latest message last).\n"
        "Finish with Ctrl-D, or a line containing only END:\n"
    )
    lines = []
    try:
        for line in sys.stdin:
            if line.strip() == "END":
                break
            lines.append(line)
    except KeyboardInterrupt:
        die("Cancelled.")
    text = "".join(lines).strip()
    if not text:
        die("No reply text provided.")
    return text


BRANCHES = {
    "gift": "gift bridge (craft to the tension the piece named)",
    "problem": "problem acceleration (towards the ask)",
    "mixed": "gift bridge, then accelerate",
    "none": "intent-led (no reply-language signal)",
}

NO_DRAFT_INTENTS = {
    "auto_reply": "Auto-reply / out of office. No draft generated. Note the "
                  "return date and time the next touch after it.",
    "rejection": None,  # drafts a one-line close, handled in template
}


def next_reply_dir(piece):
    replies = piece / "replies"
    replies.mkdir(exist_ok=True)
    n = 1
    while (replies / ("reply-%03d" % n)).exists():
        n += 1
    d = replies / ("reply-%03d" % n)
    d.mkdir()
    return d


def add_reply_arguments(ap):
    """Shared between direct invocation and the chain runner's `reply`
    subcommand, so the two entry points can never drift apart."""
    ap.add_argument("piece", help="Piece id (folder-name substring) or path")
    ap.add_argument("--channel", choices=["email", "linkedin"],
                    default="email")
    ap.add_argument("--reply-file", help="Read reply text from a file "
                    "instead of pasting")
    ap.add_argument("--reply-date", help="Date the reply arrived "
                    "(YYYY-MM-DD, default today)")
    ap.add_argument("--touches-sent", help="Override which follow-up touches "
                    "have been sent, e.g. '1' or '1,2'")
    ap.add_argument("--pieces-dir", help="Root folder for piece records")
    return ap


def main():
    args = add_reply_arguments(argparse.ArgumentParser(
        description="Sentrada P7b reply handler")).parse_args()
    run(args)


def run(args):
    cfg = load_config()
    if args.pieces_dir:
        cfg["pieces_dir"] = args.pieces_dir

    piece = resolve_piece(args.piece, cfg)
    record = load_piece(piece)
    meta = record["meta"]
    reply_text = read_reply_text(args)
    reply_date = args.reply_date or datetime.date.today().isoformat()

    recipient = meta.get("recipient") or meta.get("recipient_name") or piece.name
    company = meta.get("company") or meta.get("recipient_company") or ""
    fmt = meta.get("format", "(unknown format)")
    sender_facts = format_sender_facts(cfg)
    house_rules = read_if_exists(TEMPLATE_DIR, ["house_rules.md"]) or "(none)"

    # ---- Step 1: classify (Sonnet) ----
    classify_prompt = fill("prompt7b_classify.md", {
        "channel": args.channel,
        "format": fmt,
        "recipient": "%s%s" % (recipient, (", " + company) if company else ""),
        "brief": (record["brief"] or "")[:4000],
        "reply_text": reply_text,
    })
    classification = extract_json(call_model(classify_prompt, "p7b_classify",
                                             cfg))
    language = classification.get("reply_language", "none")
    intent = classification.get("intent", "question")
    branch = BRANCHES.get(language, BRANCHES["none"])
    if intent == "referral":
        branch = "referral routing (thank and route; new name flagged)"
    elif intent == "rejection":
        branch = "rejection close (one line, no ask, door left open)"
    elif intent == "auto_reply":
        branch = "auto-reply hold (no draft)"

    reply_dir = next_reply_dir(piece)
    (reply_dir / "reply.txt").write_text(reply_text + "\n")
    (reply_dir / "classification.json").write_text(
        json.dumps(classification, indent=2) + "\n")

    # ---- record keeping: first reply ----
    if not meta.get("first_reply_date"):
        meta["first_reply_date"] = reply_date
        meta["reply_language"] = language
        (piece / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    if intent == "auto_reply":
        print("\nClassification: %s language, %s. Branch: %s." %
              (language, intent.replace("_", " "), branch))
        print(NO_DRAFT_INTENTS["auto_reply"])
        (reply_dir / "result.json").write_text(json.dumps(
            {"intent": intent, "action": "no draft"}, indent=2) + "\n")
        return

    # ---- Step 2 + 3: draft, then gate; one retry, then HALT ----
    touches = touches_sent_summary(meta, args.touches_sent)
    open_loop = open_loop_summary(meta, args.touches_sent)
    sim_block = record["sim"] or "(no 6B simulation on file)"

    base_mapping = {
        "channel": args.channel,
        "format": fmt,
        "recipient": recipient,
        "company": company,
        "house_rules": house_rules,
        "research": record["research"],
        "brief": record["brief"] or "(none)",
        "final_copy": record["copy"] or "(none)",
        "factcheck": record["factcheck"] or "(none)",
        "simulation": sim_block,
        "followup": record["followup"] or "(none)",
        "touches_sent": touches,
        "open_loop": open_loop or "(none: never reference grid numbers or a "
                                  "withheld metric)",
        "sender_facts": sender_facts,
        "reply_text": reply_text,
        "classification": json.dumps(classification, indent=2),
    }

    drafts, gate_results = [], []
    for attempt in (1, 2):
        mapping = dict(base_mapping)
        if attempt == 2:
            mapping["violations"] = (
                "## Your previous draft failed the gate\n\n"
                "Fix every violation below. Remove or replace the offending "
                "claims; do not defend them.\n\n" + "\n".join(
                    "- [%s] %s %s" % (v["type"], v.get("claim", ""),
                                      v["issue"])
                    for v in gate_results[0]))
        else:
            mapping["violations"] = ""
        draft = call_model(fill("prompt7b_reply.md", mapping), "p7b_draft",
                           cfg).strip()
        # strip any accidental fencing/labels
        draft = re.sub(r"^```[a-z]*\n|\n```$", "", draft).strip()
        drafts.append(draft)
        (reply_dir / ("draft_attempt%d.md" % attempt)).write_text(draft + "\n")
        violations = run_gate(draft, record, sender_facts, cfg, args.channel)
        gate_results.append(violations)
        (reply_dir / "gate.json").write_text(json.dumps(
            [{"attempt": i + 1, "violations": g}
             for i, g in enumerate(gate_results)], indent=2) + "\n")
        if not violations:
            (reply_dir / "draft.md").write_text(draft + "\n")
            print("\n" + "=" * 60)
            print(draft)
            print("=" * 60)
            print("\nClassification: %s language, %s. Branch: %s." %
                  (language, intent.replace("_", " "), branch))
            if classification.get("referral_name"):
                print("NEW NAME FLAGGED: %s. No research exists on this "
                      "person; do not improvise it. Route and log."
                      % classification["referral_name"])
            if attempt == 2:
                print("(First draft failed the gate and was regenerated.)")
            print("\nDraft saved to %s. Copy, edit, send yourself."
                  % (reply_dir / "draft.md"))
            return

    # Second failure: HALT, show both drafts with violations.
    (reply_dir / "HALTED.json").write_text(json.dumps({
        "reason": "grounding/house-rules gate failed twice",
        "violations": gate_results}, indent=2) + "\n")
    print("\nHALT: the reply draft failed the gate twice.", file=sys.stderr)
    for i, (d, g) in enumerate(zip(drafts, gate_results), 1):
        print("\n----- DRAFT %d -----\n%s\n----- VIOLATIONS -----" % (i, d),
              file=sys.stderr)
        for v in g:
            print("- [%s] %s %s" % (v["type"], v.get("claim", ""),
                                    v["issue"]), file=sys.stderr)
    print("\nClassification: %s language, %s. Branch: %s." %
          (language, intent.replace("_", " "), branch), file=sys.stderr)
    print("Both drafts and violations saved under %s." % reply_dir,
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
