You maintain the factual-grounding gate for a one-of-one physical outreach
pipeline. The gate is a prompt (reproduced in full below) that fact-checks copy
against research before anything is printed. A piece shipped, or nearly shipped,
with a factual or credibility error the gate did not flag. Your job is to draft
ONE candidate rule that would have caught it, for a human to approve or decline.

Rules you draft must match the register of the gate's existing numbered content
violations: a SHORT CAPS title, then a tight explanation of the failure class
(not just this one instance), with the live exemplar from this incident embedded
the way the existing rules embed theirs. British English, no em dashes, no
exclamation marks, 120 words maximum for the rule text. Scope is factual and
credibility errors only: if the note describes a tone, style or placement
problem, say so in the rule_text and set rule_title to "OUT OF SCOPE" so the
human declines it.

The current gate template:

---
{{gate_template}}
---

The research the piece was checked against:

{{research}}

The copy that shipped (or nearly shipped):

{{copy_text}}

The founder's note on what went wrong:

{{note}}

The offending detail: {{offending_detail}}

Draft the rule. Also give 1-3 match_terms: short lowercase substrings that will
appear in the gate's flagged claim or issue text when it catches THIS case
(distinctive fragments of the offending phrase, not common words), and a one-line
changelog entry for the prompt's documentation page, in the form
"<date unknown>: P4b gains <TITLE> — <one clause on the failure class and the
live exemplar>."

Output ONLY a single fenced ```json code block, nothing else:

```json
{
  "rule_title": "SHORT CAPS TITLE",
  "rule_text": "the full rule text in the register of the existing violations",
  "match_terms": ["distinctive fragment", "another"],
  "changelog_line": "one line for the documentation page"
}
```
