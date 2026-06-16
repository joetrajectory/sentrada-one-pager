#!/usr/bin/env python3
"""
Sentrada chain runner.

Runs the Sentrada prompt chain via the Anthropic API to produce one-of-one
physical outreach pieces. Research (Prompt 1) happens outside this runner: you
paste your deep research in as a file. The runner then executes Prompt 2 (brief),
pauses for your approval, runs Prompt 4 (copy), and either renders a newspaper
with the layout engine or assembles a paste-ready claymation image prompt.

Three commands:

  generate  Run the main pipeline for one piece.
  qc        Run Prompts 6 and 6B (vision) against a final image.
  followup  Run Prompt 7 to write the companion card and 3-touch follow-up.

Prompt text lives in runner/templates/*.md, never in this file. Edit the prompts
there. This file only fills placeholders, calls the API, gates the output, and
drives the layout engine.

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

import anthropic

# --- Paths -----------------------------------------------------------------

RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(RUNNER_DIR)
TEMPLATE_DIR = os.path.join(RUNNER_DIR, "templates")
PIECES_DIR = os.path.join(RUNNER_DIR, "pieces")
DEFAULT_CONFIG = os.path.join(RUNNER_DIR, "config.json")

# --- Models (per the build spec) -------------------------------------------

MODEL_TEXT = "claude-sonnet-4-6"    # Prompts 2, 4, 5, 7
MODEL_VISION = "claude-opus-4-8"    # Prompts 6 and 6B (strongest vision)

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


# --- Anthropic calls --------------------------------------------------------

def make_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        die("ANTHROPIC_API_KEY is not set in the environment.")
    # SDK auto-retries 429/5xx/connection errors; bump to 4 for transient blips.
    return anthropic.Anthropic(max_retries=4)


def call_text(client, prompt, model=MODEL_TEXT, max_tokens=16000):
    with client.messages.stream(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for _ in stream.text_stream:
            pass
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def call_vision(client, prompt, image_path, model=MODEL_VISION, max_tokens=16000):
    if not os.path.exists(image_path):
        die(f"image not found: {image_path}")
    data = base64.standard_b64encode(read_bytes(image_path)).decode("utf-8")
    media = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
        {"type": "text", "text": prompt},
    ]
    with client.messages.stream(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        for _ in stream.text_stream:
            pass
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


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
        cmd += ["--output", output_path, "--print-dpi", "300"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


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
        "format": "Newspaper Front Page" if fmt == "newspaper" else "Claymation Scene",
    }


def _build_meta(args, fmt, brief, sender):
    return {
        "recipient_name": args.name, "recipient_title": args.title,
        "recipient_company": args.company, "format": fmt,
        "problem_label": brief.get("problem_label", ""),
        "companion_card_hook": brief.get("companion_card_hook", ""),
        "sender": sender,
    }


def _run_brief(client, folder, base_values, feedback_text=""):
    feedback_block = ""
    if feedback_text:
        feedback_block = ("FOUNDER FEEDBACK ON YOUR PREVIOUS DRAFT:\n" + feedback_text
                          + "\n\nRegenerate the brief incorporating this feedback.")
    prompt = fill(load_template("prompt2_brief.md"),
                  dict(base_values, feedback_block=feedback_block))
    print("\n[prompt 2] generating the brief...")
    brief_text = call_text(client, prompt)
    brief = extract_last_json(brief_text)
    if brief is None:
        die("could not read the brief JSON block from Prompt 2's reply. "
            "Check runner/templates/prompt2_brief.md output format.")
    write_file(os.path.join(folder, "brief.md"), brief_text)
    write_file(os.path.join(folder, "brief.json"), json.dumps(brief, indent=2))
    return brief


def _continue_after_brief(client, args, config, folder, base_values, brief, sender):
    meta = _build_meta(args, fmt_of(args), brief, sender)
    if fmt_of(args) == "newspaper":
        _generate_newspaper(client, args, config, folder, base_values, brief, meta)
    else:
        _generate_claymation(client, args, folder, base_values, brief, meta)


def fmt_of(args):
    return args.format.lower()


def cmd_generate(args):
    config = load_config(args.config)
    sender = config["sender"]
    fmt = args.format.lower()
    if fmt not in ("newspaper", "claymation"):
        die("format must be 'newspaper' or 'claymation'.")

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
        client = make_client()
        print(f"[resume] {os.path.relpath(folder, REPO_ROOT)} (brief already approved)")
        _continue_after_brief(client, args, config, folder, base_values, brief, sender)
        return

    if not args.research:
        die("--research is required (path to your pasted research file).")
    research = read_file(args.research)
    os.makedirs(folder, exist_ok=True)
    write_file(os.path.join(folder, "research.md"), research)
    print(f"[folder] {os.path.relpath(folder, REPO_ROOT)}")
    base_values = _build_base_values(args, sender, research, fmt)
    client = make_client()

    # --- brief-only: run the brief once, print the checkpoint, then stop ---
    if args.brief_only:
        brief = _run_brief(client, folder, base_values, args.feedback)
        if brief.get("fit") == "poor":
            die("the brief declares a POOR fit: "
                + brief.get("fit_reason", "no problem reached medium/high confidence"))
        print_brief_checkpoint(brief)
        rel = os.path.relpath(__file__, REPO_ROOT)
        print("\n[brief-only] Brief saved. When you approve it, continue with:")
        print(f'  python {rel} generate --name "{args.name}" --title "{args.title}" '
              f'--company "{args.company}" --format {fmt} --resume')
        print(f'  (or revise first: ... --format {fmt} --brief-only --feedback "your notes")')
        return

    # --- default interactive run with the approval checkpoint ---
    feedback_text = ""
    while True:
        brief = _run_brief(client, folder, base_values, feedback_text)
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

    _continue_after_brief(client, args, config, folder, base_values, brief, sender)


def _generate_newspaper(client, args, config, folder, base_values, brief, meta):
    template = load_template("prompt4_copy_newspaper.md")
    brief_values = dict(base_values, **{
        "core_problem": brief.get("core_problem", ""),
        "key_metric": brief.get("key_metric", ""),
        "environment": brief.get("environment", ""),
        "moment": brief.get("moment", ""),
        "problem_label": brief.get("problem_label", ""),
        "operational_details": brief.get("operational_details", ""),
    })

    attempt, feedback_block = 0, ""
    while True:
        attempt += 1
        prompt = fill(template, dict(brief_values, feedback_block=feedback_block))
        print(f"\n[prompt 4] writing newspaper copy (attempt {attempt})...")
        reply = call_text(client, prompt)
        copy_part, factcheck = split_factcheck(reply)
        data = extract_last_json(reply)
        if data is None:
            die("could not read the engine JSON block from Prompt 4's reply.")
        violations = newspaper_violations(data)
        if not violations:
            break
        if attempt >= 2:
            die("Prompt 4 newspaper failed the word-count gate twice:\n  - "
                + "\n  - ".join(violations))
        print("[gate] word-count violations, rerunning Prompt 4 once:")
        for x in violations:
            print(f"  - {x}")
        feedback_block = ("YOUR PREVIOUS DRAFT FAILED THESE HARD CONSTRAINTS. Fix "
                          "every one and keep everything else:\n- " + "\n- ".join(violations))

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

    _final_report(folder, output_path, factcheck)


def _generate_claymation(client, args, folder, base_values, brief, meta):
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
    print("\n[prompt 4] writing claymation copy...")
    reply = call_text(client, fill(template, values))
    copy_part, factcheck = split_factcheck(reply)
    clay = extract_last_json(reply)
    if clay is None:
        die("could not read the claymation JSON block from Prompt 4's reply.")
    write_file(os.path.join(folder, "copy.md"), copy_part)
    write_file(os.path.join(folder, "claymation_copy.json"), json.dumps(clay, indent=2))
    write_file(os.path.join(folder, "factcheck.md"), factcheck or "(none returned)")

    # --- Prompt 5: assemble the paste-ready image prompt ---
    p5 = load_template("prompt5_assembly_claymation.md")
    p5_values = dict(values, **{
        "layer_a_claymation": load_template("layer_a_claymation.md"),
        "layer_c": load_template("layer_c.md"),
        "scene_description": clay.get("scene_description", ""),
        "visual_details": "; ".join(clay.get("visual_details", []))
        if isinstance(clay.get("visual_details"), list) else clay.get("visual_details", ""),
        "in_scene_text": "; ".join(clay.get("in_scene_text", []))
        if isinstance(clay.get("in_scene_text"), list) else clay.get("in_scene_text", ""),
        "caption": clay.get("caption", ""),
    })
    print("[prompt 5] assembling the paste-ready image prompt...")
    image_prompt = call_text(client, fill(p5, p5_values))
    write_file(os.path.join(folder, "image_prompt.txt"), image_prompt)

    meta["piece_reference"] = f'the claymation scene captioned "{clay.get("caption", "")}"'
    if not meta.get("companion_card_hook"):
        meta["companion_card_hook"] = clay.get("companion_card_hook", "")
    write_file(os.path.join(folder, "meta.json"), json.dumps(meta, indent=2))

    print("\n" + "=" * 70)
    print("CLAYMATION COPY AND PASTE-READY IMAGE PROMPT WRITTEN")
    print("=" * 70)
    print(f"\nFolder: {os.path.relpath(folder, REPO_ROOT)}")
    print("  image_prompt.txt   <- paste this into ChatGPT image generation, then upscale")
    print("  copy.md            <- scene, caption, in-scene text")
    print("  factcheck.md       <- review against the rendered image before print")
    print("\nFACT CHECK LIST:\n" + (factcheck or "(none returned)"))


def _final_report(folder, output_path, factcheck):
    print("\n" + "=" * 70)
    print("NEWSPAPER RENDERED — print-ready")
    print("=" * 70)
    print(f"\nFolder: {os.path.relpath(folder, REPO_ROOT)}")
    print(f"  {os.path.basename(output_path)}   <- the print-ready A2 newspaper (300 DPI)")
    print("  data.json          <- engine input (engine schema only)")
    print("  copy.md            <- the full written copy")
    print("  factcheck.md       <- check the rendered piece against this before print")
    print("\nFACT CHECK LIST (review the rendered piece against this before print):\n"
          + (factcheck or "(none returned)"))


# --- qc (Prompts 6 + 6B) ----------------------------------------------------

def cmd_qc(args):
    folder = args.folder
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    brief = json.loads(read_file(os.path.join(folder, "brief.json")))
    research = read_file(os.path.join(folder, "research.md"))
    client = make_client()

    legibility = "Rendered by the layout engine; all text is guaranteed legible."
    lp = os.path.join(folder, "image_prompt.txt")
    if os.path.exists(lp):
        txt = read_file(lp)
        idx = txt.find("LEGIBILITY CHECK:")
        legibility = txt[idx + len("LEGIBILITY CHECK:"):].strip() if idx != -1 else txt

    fmt_label = "Newspaper Front Page" if meta["format"] == "newspaper" else "Claymation Scene"
    review_values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "format": fmt_label,
        "core_problem": brief.get("core_problem", ""), "key_metric": brief.get("key_metric", ""),
        "problem_label": brief.get("problem_label", ""),
        "operational_details": brief.get("operational_details", ""),
        "research": research, "legibility_checklist": legibility,
    }
    print("[prompt 6] reviewing the image (vision)...")
    review = call_vision(client, fill(load_template("prompt6_review.md"), review_values), args.image)
    write_file(os.path.join(folder, "qc_review.md"), review)

    print("[prompt 6B] simulating the recipient (vision)...")
    sixb_values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "research": research,
    }
    sixb = call_vision(client, fill(load_template("prompt6b_recipient.md"), sixb_values), args.image)
    write_file(os.path.join(folder, "qc_recipient.md"), sixb)

    print("\n" + "=" * 70)
    print("QC COMPLETE")
    print("=" * 70)
    print("  qc_review.md     <- Prompt 6 craft review (pass/fail)")
    print("  qc_recipient.md  <- Prompt 6B recipient simulation (would they respond)")
    print("\n--- Prompt 6 verdict line ---")
    for line in review.splitlines():
        if line.strip().upper().startswith("VERDICT"):
            print(line.strip())
            break
    print("\n--- Prompt 6B verdict ---")
    print(extract_6b_verdict(sixb) or "(see qc_recipient.md)")


# --- followup (Prompt 7) ----------------------------------------------------

def extract_6b_verdict(text):
    for phrase in ("WOULD TAKE THE MEETING", "WOULD ENGAGE IF FOLLOWED UP WELL",
                   "WOULD ADMIRE AND IGNORE", "WOULD BIN"):
        if phrase in text:
            return phrase
    return None


def cmd_followup(args):
    folder = args.folder
    meta = json.loads(read_file(os.path.join(folder, "meta.json")))
    brief = json.loads(read_file(os.path.join(folder, "brief.json")))
    research = read_file(os.path.join(folder, "research.md"))
    sender = meta["sender"]
    client = make_client()

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

    fmt_label = "Newspaper Front Page" if meta["format"] == "newspaper" else "Claymation Scene"
    values = {
        "recipient_name": meta["recipient_name"], "recipient_title": meta["recipient_title"],
        "recipient_company": meta["recipient_company"], "format": fmt_label,
        "piece_reference": meta.get("piece_reference", ""),
        "problem_label": brief.get("problem_label", ""), "core_problem": brief.get("core_problem", ""),
        "key_metric": brief.get("key_metric", ""), "operational_details": brief.get("operational_details", ""),
        "companion_card_hook": meta.get("companion_card_hook", "") or brief.get("companion_card_hook", ""),
        "reserve_detail": "",  # left blank by design; Prompt 7 flags this if it matters
        "research": research,
        "verdict_6b": verdict, "failure_mode_6b": failure, "leverage_6b": leverage, "stopped_6b": stopped,
        "sender_name": sender.get("sender_name", ""), "sender_company": sender.get("company", ""),
        "sender_what": sender.get("what_they_sell", ""), "sender_proof": sender.get("proof_points", ""),
        "booking_link": sender.get("booking_link", ""), "delivery_date": args.delivery_date,
    }
    print("[prompt 7] writing the companion card and 3-touch follow-up...")
    reply = call_text(client, fill(load_template("prompt7_followup.md"), values))
    write_file(os.path.join(folder, "followup.md"), reply)

    print("\n" + "=" * 70)
    print("FOLLOW-UP WRITTEN")
    print("=" * 70)
    print(f"  followup.md  <- companion card + Touch 1-3 + reception nudge + fact check")
    print("\n" + reply)


# --- CLI --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Sentrada chain runner")
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="run the main pipeline for one piece")
    g.add_argument("--name", required=True, help="recipient full name")
    g.add_argument("--title", required=True, help="recipient job title")
    g.add_argument("--company", required=True, help="recipient company")
    g.add_argument("--format", required=True, help="newspaper or claymation")
    g.add_argument("--research", help="path to the pasted research file (required unless --resume)")
    g.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    g.add_argument("--brief-only", action="store_true",
                   help="run the brief and stop at the checkpoint (non-interactive approval)")
    g.add_argument("--resume", action="store_true",
                   help="skip the brief; continue from the approved brief.json in the piece folder")
    g.add_argument("--feedback", default="",
                   help="with --brief-only, regenerate the brief incorporating this feedback")
    g.set_defaults(func=cmd_generate)

    q = sub.add_parser("qc", help="run Prompts 6 and 6B against a final image")
    q.add_argument("--folder", required=True, help="the piece folder")
    q.add_argument("--image", required=True, help="the final image file (PNG or JPG)")
    q.set_defaults(func=cmd_qc)

    f = sub.add_parser("followup", help="run Prompt 7 to write the follow-up sequence")
    f.add_argument("--folder", required=True, help="the piece folder")
    f.add_argument("--delivery-date", required=True, help="delivery date, e.g. '16 June 2026'")
    f.set_defaults(func=cmd_followup)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
