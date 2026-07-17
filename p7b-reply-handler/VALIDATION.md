# P7b live validation record (17 July 2026)

All six campaign one replies ran through the real `reply` command with live models: Sonnet 5 classify, Fable 5 draft, Opus 4.8 gate. Research stand-ins were assembled from the campaign touch emails and the Notion campaign records; the sender profile carried only facts the sender has stated himself. The reply verbatims, the tool drafts, and the actually-sent texts are NOT in this public repo; they live in the comparison document delivered in the build session, in the Notion Results Post page, and in the inbox threads.

## Results

| Case | Language | Intent | Branch | Gate | Outcome |
|---|---|---|---|---|---|
| 1. MMC investor | gift | objection | gift bridge + meet condition | pass (1st) | Draft matched the human send on substance and beat it on mechanics; see Vitrue note below |
| 2. Notion Capital | none | rejection | rejection close | pass (1st) | NO SEND RECOMMENDED; matches what was actually done (silence) |
| 3. firstminute EA | none | referral | referral routing, new name flagged | fail then pass (2nd) | Live retry loop fired on a misattribution nuance and cleared |
| 4. Hoxton founder | gift | polite_deflection | gift bridge | fail then pass (2nd) | Canonical craft-to-tension bridge, clean |
| 5. Episode 1 GP | gift | objection | concede + pivot, no invented fix | pass (1st) | Refused to fabricate a material-fix claim |
| 6. Seedcamp (OOO) | none | auto_reply | no draft | n/a | Return date noted; stats untouched |

Every classification was correct. No case reached HALT. The retry loop worked twice in production.

## The Vitrue lesson (settled 17 July 2026)

Case 1's reply set a condition: a recommendation from the recipient's own portfolio. The human send offered a past placement (Vitrue Health) without establishing the portfolio connection; the tool used the same facts but refused to imply that connection because no source on file stated it, and flagged it as `[SENDER INPUT NEEDED]`. It was later confirmed the connection is real: MMC co-led Vitrue Health's seed round in early 2024 and Simon Menashy was the partner quoted on the announcement. So the human reply met the condition, AND the gate was right to refuse the claim, because it was not in the research. Verify, don't assume, worked exactly as designed. The fix when this happens is always to enrich the sender profile or research, never to loosen the gate.

## Adjustments the validation drove (all implemented)

1. `[SENDER INPUT NEEDED]` placeholder: the strongest human replies won on privately-held facts; the draft demands them rather than inventing or writing around them, and the gate whitelists the placeholder.
2. `rejection` intent + NO SEND RECOMMENDED output: a flat no gets silence or one line, never a re-pitch.
3. `auto_reply` intent: OOO robots get no draft and do not set first-reply-date or reply-language.
4. Referral covers inbound routers (an EA replying on the recipient's behalf), with the new name flagged and never researched from nothing.
5. Salutation and sign-off rules (email opens with the latest writer's first name, signs off with the sender's first name; LinkedIn skips both), added after the first live run and re-validated across all six cases.

## Known limits

- The four comparisons where the human's actual send is not on file are one-sided (recorded as such; proceed without them).
- The problem-language branch (accelerate to the ask) is validated in code and template but UNTESTED against a real converting reply; campaign one's best problem-language exchange was not recoverable. Treat the first live problem-language reply as a watch-closely case.
- The objection branch alternates run-to-run between concede-and-pivot and demanding sender input. Both are legitimate; the operator edit decides.
- Research stand-ins were thinner than real piece folders; watch the first real-piece run.
