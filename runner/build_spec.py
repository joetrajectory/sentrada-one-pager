#!/usr/bin/env python3
"""
Regenerate ../sentrada-prompts-all-v4.md from the runtime templates.

The templates in runner/templates/ are the single source of truth for what the
runner executes. This script assembles them into the combined reference doc so
the two can never drift. Edit the templates, then run:

    python runner/build_spec.py
"""

import os

RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(RUNNER_DIR, "templates")
OUT = os.path.join(os.path.dirname(RUNNER_DIR), "sentrada-prompts-all-v4.md")

# Pipeline order, with human-readable section titles.
SECTIONS = [
    ("prompt2_brief.md", "PROMPT 2: BRIEF AGENT"),
    ("prompt4_copy_newspaper.md", "PROMPT 4: COPY AGENT (NEWSPAPER)"),
    ("prompt4_copy_claymation.md", "PROMPT 4: COPY AGENT (CLAYMATION)"),
    ("prompt4b_grounding.md", "PROMPT 4B: FACTUAL GROUNDING GATE"),
    ("prompt5_assembly_claymation.md", "PROMPT 5: ASSEMBLY AGENT (CLAYMATION)"),
    ("layer_a_claymation.md", "LAYER A: CLAYMATION SCENE"),
    ("layer_c.md", "LAYER C: VARIATION VARIABLES"),
    ("prompt6_review.md", "PROMPT 6: REVIEW AGENT"),
    ("prompt6b_recipient.md", "PROMPT 6B: RECIPIENT AGENT"),
    ("prompt7_followup.md", "PROMPT 7: FOLLOWUP AGENT"),
]

BANNER = """# Sentrada Prompt Chain v4

> GENERATED FILE - do not edit by hand. This document is assembled from the
> runtime templates in `runner/templates/` (the single source of truth for what
> the runner executes) by `runner/build_spec.py`. Edit the templates, then run
> `python runner/build_spec.py` to refresh this file.
>
> Each section below is the exact template the runner fills and sends, including
> the named `{{placeholder}}` tags and the structured output blocks. Prompt bodies
> are verbatim from the Notion Prompts & Architecture folder; the placeholders and
> output blocks are the only runner-specific additions.
>
> P1 (Research Agent) runs outside the runner (manual deep research in Claude).
> P3 (Format Agent) is skipped; the format is passed to the runner as a flag.
"""


def main():
    parts = [BANNER]
    for fname, title in SECTIONS:
        path = os.path.join(TEMPLATE_DIR, fname)
        if not os.path.exists(path):
            print(f"[warn] missing template: {fname}")
            continue
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read().rstrip()
        parts.append(f"\n---\n\n## {title}\n\n"
                     f"*(template: `runner/templates/{fname}`)*\n\n{body}\n")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts) + "\n")
    print(f"wrote {os.path.relpath(OUT, os.path.dirname(RUNNER_DIR))} "
          f"from {len(SECTIONS)} templates")


if __name__ == "__main__":
    main()
