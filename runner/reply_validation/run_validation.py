#!/usr/bin/env python3
"""P7b validation harness.

Runs every case under validation/cases/ through the real reply command and
writes a side-by-side comparison (tool draft vs what was actually sent) to
validation/out/comparison.md.

A case is a folder containing:
    case.json          {"channel": "email", "format": "...", "recipient": "...",
                        "company": "...", "notes": "..."}
    reply.txt          the inbound reply (or thread, latest last)
    research.md        research stand-in the gate checks against
    factcheck.md       optional
    meta.json          optional (open_loop record, touchN_sent_date, ...)
    actually_sent.md   optional: what was actually sent, for the comparison

Requires model access (ANTHROPIC_API_KEY or the claude CLI). Historical
campaign replies contain third-party personal data: keep real cases OUT of
any public repo. Point --cases at a local folder.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPLY = HERE.parent / "reply.py"


def run_case(case_dir, out_dir):
    with open(case_dir / "case.json") as f:
        case = json.load(f)
    piece = Path(tempfile.mkdtemp(prefix="p7b-val-")) / case_dir.name
    piece.mkdir()
    for name in ("research.md", "factcheck.md", "meta.json"):
        src = case_dir / name
        if src.exists():
            shutil.copy(src, piece / name)
    if not (piece / "meta.json").exists():
        (piece / "meta.json").write_text(json.dumps({
            "recipient": case.get("recipient", ""),
            "company": case.get("company", ""),
            "format": case.get("format", ""),
        }, indent=2))

    cmd = [sys.executable, str(REPLY), str(piece),
           "--reply-file", str(case_dir / "reply.txt"),
           "--channel", case.get("channel", "email")]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    result = {"case": case_dir.name, "exit": proc.returncode,
              "stdout": proc.stdout, "stderr": proc.stderr}
    reply_dir = piece / "replies" / "reply-001"
    for name in ("classification.json", "draft.md", "gate.json",
                 "HALTED.json"):
        p = reply_dir / name
        if p.exists():
            result[name] = p.read_text()
    dest = out_dir / case_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "result.json").write_text(json.dumps(result, indent=2))
    return case, result


def comparison_block(case_dir, case, result):
    lines = ["## %s" % case_dir.name, ""]
    if case.get("notes"):
        lines += [case["notes"], ""]
    lines += ["**The reply:**", "", "```",
              (case_dir / "reply.txt").read_text().strip(), "```", ""]
    if "classification.json" in result:
        c = json.loads(result["classification.json"])
        lines += ["**Classification:** %s language, %s" %
                  (c.get("reply_language"), c.get("intent")), ""]
    if "draft.md" in result:
        lines += ["**Tool draft:**", "", "```",
                  result["draft.md"].strip(), "```", ""]
    elif result["exit"] != 0:
        lines += ["**Tool result:** HALTED or errored (see out/%s/result.json)"
                  % case_dir.name, ""]
    else:
        lines += ["**Tool result:** no draft (see stdout in result.json)", ""]
    sent = case_dir / "actually_sent.md"
    if sent.exists():
        lines += ["**Actually sent:**", "", "```",
                  sent.read_text().strip(), "```", ""]
    else:
        lines += ["**Actually sent:** (not on file)", ""]
    lines += ["**Where the human was better / adjustment:** _fill in after "
              "review_", "", "---", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(HERE / "cases"))
    ap.add_argument("--out", default=str(HERE / "out"))
    args = ap.parse_args()

    cases_dir, out_dir = Path(args.cases), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    blocks = ["# P7b validation: tool drafts vs what was actually sent", ""]
    case_dirs = sorted(d for d in cases_dir.iterdir()
                       if d.is_dir() and (d / "case.json").exists())
    if not case_dirs:
        sys.exit("No cases found in %s" % cases_dir)
    for case_dir in case_dirs:
        print("Running %s ..." % case_dir.name)
        case, result = run_case(case_dir, out_dir)
        blocks.append(comparison_block(case_dir, case, result))
        status = "OK" if result["exit"] == 0 else "HALT/ERROR"
        print("  -> %s" % status)
    (out_dir / "comparison.md").write_text("\n".join(blocks))
    print("\nComparison written to %s" % (out_dir / "comparison.md"))


if __name__ == "__main__":
    main()
