# Pending Notion mirror updates (apply next session, then delete this file)

These changelog notes belong on the Notion prompt pages but could not be
written on 3 July 2026 (unstable MCP connection). The repo templates are
authoritative; these are cosmetic mirrors. Already applied: P1 (research
weighting + header fix) and P2 (reserve-detail litigation ban).

## Prompt 4: Copy Agent — append as "Changelog (3 July 2026)"

Shared house rules extracted: the cross-cutting copy rules (tone, metric
inversion, factual accuracy, personalisation, content boundary, proof
attribution, litigation/misfortune leverage ban) now live once in
runner/templates/house_rules.md, injected into the newspaper, crossword and
email copy templates via {{house_rules}}. Edit rules there, not per template.
Format-specific rules stay in the format templates and win where they nuance
a house rule. Prompt 4b gains a fourth content violation, LITIGATION
LEVERAGE, and now also gates Prompt 7's output (card + touches).

## Prompt 6: Review Agent — append as "Changelog (3 July 2026)"

Test 5 (Customisation Communication) reworded for chain order: card copy is
written at Prompt 7, after this review runs, so in the standard chain it is
N/A; it applies only on a qc re-run where card copy is supplied.

## Prompt 7: Followup Agent — append to the existing changelog

P7 output now passes the Prompt 4b grounding gate (max 3 attempts,
followup_gate.json). The piece reference and sender block are derived at run
time from data.json and live config, never from the build-time meta.json
snapshot. 6B emits a machine-readable JSON tail P7 consumes. Sender
custom_card: true skips generated card copy entirely. Never propose sender
spend (no COMMERCIAL OPTION flags; the CTA commits nothing).
