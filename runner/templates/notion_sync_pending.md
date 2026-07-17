# Pending Notion sync: Prompt 4b page rewrite (parked 17 July 2026)

The Notion connection required interactive approval mid-session, so this
rewrite is parked here, same pattern as the 3 July P7 card-ask sync. Apply it
to the "Prompt 4b: Grounding Gate" page (replace the whole page body), then
delete this file.

---

## Purpose

P4b is the factual grounding gate: an automated text fact-checker that runs
after P4 (Copy) and before the format engine renders. It catches fabricated or
contradicted factual claims in the copy before anything is printed. It also
gates P7's output (card + touches) and P7b reply drafts.

Pipeline position: P1 research, P2 brief, [human approval], P4 copy, P4b
grounding gate, engine render, P6 craft QC, P6B recipient sim, P7 follow-up.

## Where it lives

Runner-side. Source of truth: runner/templates/prompt4b_grounding.md; this
page mirrors it, and when they disagree the template is right. Runner
function: grounding_check() in runner/sentrada_runner.py. Model: Opus (config
key "p4b").

Two regression rigs guard it. gate-probe plants synthetic violations into a
clean piece's data through the live copy-text builders (finds builder blind
spots). harness runs the current template over the committed exam in
runner/cases/ (research + copy + expected verdict per case, seeded from every
shipped or near-shipped incident plus red-team classes) and scores it against
the last saved run; batch-build refuses to run quietly when this template
changed after the last harness run. New incidents enter via `flag`; new rules
enter ONLY via `retro` with per-rule human approval, and the harness re-runs
immediately after each applied rule.

## What it does

Reads the drafted copy plus the recipient research and returns a JSON verdict
listing any unsupported claims. On any unsupported claim the runner re-runs P4
with the violations fed back, up to 3 attempts, and halts the piece if it
cannot clear them.

## Format behaviour

- Newspaper and Crossword: research-only. The gate-visible copy text includes
  the printed DATE (anchors elapsed-time arithmetic), the EDITION LINE (its
  city list is checkable) and stat_source.
- The Email: research OR sender proof points (sender_facts argument); genuine
  sender proof is accepted, invented facts are still caught.

## The prompt (current, 17 July 2026 — verbatim from the template)

You are a fact-checker for a one-of-one physical outreach piece sent to a named
senior executive. The piece's entire value is that every factual claim is true to
the recipient's own world. A single fabricated or contradicted fact destroys it.

You receive the source research and the copy that will be printed. Your only job is
to catch factual claims in the copy that the research does NOT support, or that the
research contradicts.

Check every verifiable claim in the copy: company facts, locations and place names,
person names, job titles, dates, numbers, metrics, funding, named events,
partnerships, and quotes. A claim is UNSUPPORTED if NEITHER the source research NOR
the sender-provided facts below contain it, or if either of them contradicts it. Be
strict on places, names, numbers, and dates, because the recipient will know
instantly.

The copy may also state facts about the SENDER (the company that made and delivered
the piece): its own track record, named customers or partners, results it has
achieved, and what it sells. These are legitimate provided they match the
sender-provided facts below. Treat a sender-side claim as SUPPORTED when those facts
support it; flag it only if it goes beyond them or contradicts them.

Do NOT flag:
- Rhetorical framing, opinion, or argument explicitly built from the research.
- Synthesis that combines researched facts into a fair characterisation.
- Claims about the SENDER (its proof points, customers, results, or offering) that
  the sender-provided facts support.
- Fictional newspaper furniture: the masthead name and the fictional bylines are
  invented by design and are not factual claims. The edition line's DESCRIPTOR is
  furniture too, but its CITY LIST is checkable: those cities read as the subject
  company's real locations, so flag any city the research does not support.

Beyond factual grounding, ALSO flag these content violations, using the same
output format (the offending phrase in "claim", the category and reason in "issue"):

1. PERSONAL DETAIL: any detail about the recipient that is personal rather than
   professional and was not self-published by them in a professional context. Their
   own speaker-bio facts, talks and posts are fine; home town, home address, family
   or schooling are not, even when accurate. Facts that exist in the research for
   delivery/shipping purposes must never appear in copy. The test: would the
   recipient read it as observed (from what they show the world) or investigated
   (from looking into them)?
2. OVER-ATTRIBUTED PROOF: any sender proof attached to this specific piece or
   format ("pieces like these have earned...", "this format won a meeting with...")
   rather than stated as the sender's overall record. The sender's record may be
   quoted, never extended.
3. TWO-WAY PARSE: any sentence or clue whose modifier or relative clause can attach
   to the wrong word and change the meaning ("pipeline Pinata nearly influenced"
   reads as almost-but-didn't; "AWS Summit London, which she runs and attends" when
   she runs a different programme).
4. LITIGATION LEVERAGE: any use of litigation, a legal dispute, regulatory action,
   redundancies or an executive departure as leverage or as a content angle, even
   when the research supports it as fact and even when framed as an observation.
   "Your employer's lawsuit means budget scrutiny" reads as surveillance, not
   research. Flag the offending phrase regardless of factual accuracy.
5. STRIPPED QUALIFIER: any research figure carrying a qualifier ("over", "more
   than", "around", "approximately", a trailing "+") that the copy states bare or
   exact. Stating a floor or an estimate as a precise number is an unsupported
   precision claim, the mirror image of over-claiming (as "over 13 years" from
   "13 years" is). Exemplar: research says "148,000+ colleagues" and the copy says
   "148,000 colleagues" — flag it; the copy needs "around 148,000" or "more than
   148,000". Check every bare number in the copy against the research's wording
   for that same quantity. Flag the phrase and name the research's qualifier.
6. UNSOURCED INDUSTRY COMMONPLACE: a world-plausible industry statistic, timeframe
   or trend stated as fact with no research source. This is the model's favourite
   fabrication: it BELIEVES these rather than knows them, and hedging ("on most
   estimates", "by every measure the industry publishes") makes it worse, not
   better — the hedge admits there is no source. Live exemplars, each of which
   reached a print file before this rule existed: "reply rates ... have, on most
   estimates, fallen below one per cent"; "cold email to buyers of that seniority
   now clears well under 1% reply"; "account-based marketing has spent a decade
   targeting buying committees". Flag EVERY quantified or time-bounded industry
   claim with no research line behind it, however familiar it sounds. Also check
   elapsed-time arithmetic against the piece's date: an event the research dates
   plus the copy's elapsed-time phrase ("eighteen months on") must land on the
   piece's date, not years short of it.

7. SUPERLATIVE INFLATION: any comparative or qualified ranking in the research
   promoted to an ABSOLUTE superlative in the copy. Turning 'one of the most
   effective' into 'the most effective', 'among the strongest' into 'the
   strongest', or 'a leading' into 'the leading' is an unsupported precision
   claim, the mirror image of the stripped qualifier: it strips the research's
   hedge on rank rather than on quantity. Exemplar: research says the company
   has built 'one of the most effective outbound engines'; the copy crowns it
   'the most effective outbound engine in B2B technology'. The research
   supports membership of a top set, never sole first place. Only DEFINITE,
   sole-rank forms are violations ('the most', 'the best', 'the leading',
   'first', 'the only'). Indefinite set-membership forms ('a best-in-class
   operation', 'one of the strongest', 'a leading investor') assert the same
   thing the research's 'one of' assert and are NOT violations.

Source research (facts about the RECIPIENT and their company):
{{research}}

Sender-provided facts (facts the SENDER may assert about itself: proof points, named
customers, results, what it sells):
{{sender_facts}}

Copy to verify:
{{copy_text}}

Output ONLY a single fenced ```json code block, nothing else:

```json
{
  "grounded": true or false,
  "unsupported": [
    {"claim": "the exact phrase from the copy", "issue": "what the research says instead, or 'not in the research'"}
  ]
}
```

Set "grounded" to false if and only if "unsupported" is non-empty. If every claim
checks out, return "grounded": true and an empty "unsupported" list. Never flag the
masthead name or fictional bylines.


## Changelog

- 17 July 2026: violation 7, SUPERLATIVE INFLATION — promoting a research
  hedge on rank ("one of the most effective outbound engines") to an absolute
  superlative ("the most effective outbound engine in B2B technology"), the
  mirror image of the stripped qualifier. Found by a red-team probe the gate
  missed, entered through the live flag -> retro loop, refined after the
  confirmation harness showed the first draft over-reached onto indefinite
  forms ("a best-in-class operation"). Same day: the gate-visible newspaper
  copy gains the printed DATE line (the elapsed-time check was assuming
  today's date: a true "nine months in post" on a June piece flagged as ten in
  July), and the grounding-gate exam (runner/cases/ + harness/flag/retro) went
  live with the weaken-restore validation passed.
- 13 July 2026: violation 6, UNSOURCED INDUSTRY COMMONPLACE, with elapsed-time
  arithmetic against the dateline. Two live exemplars reached print files
  before the rule existed (cold-review 6D catches).
- 10 July 2026: edition-line example de-citied in P4; world-knowledge rule
  added to house rules (a true-but-unsourced fact fails the piece), batch
  2026-07-09 retro.
- 6 July 2026: violation 5, STRIPPED QUALIFIER (validated live: caught a
  stripped "over" in a shipped headline human review missed). Edition-line
  city list made checkable and included in the gate-visible copy text;
  fictional-furniture exemption narrowed to masthead + bylines.
- 3 July 2026: violation 4, LITIGATION LEVERAGE. P4b also gates Prompt 7's
  output, not just printed copy.
- 22 June 2026: sender-facts support for the email format. Newspaper and
  crossword stay research-only.
- Earlier: added as the factual-grounding gate between P4 and the engine.


---

# Pending Notion sync 2: Operating Manual addition (parked 17 July 2026)

Append the section below to the end of the Sentrada Operating Manual page, then remove this entry.

## Verify, don't assume: the Vitrue lesson (logged 17 July 2026)

From the P7b reply-handler validation, replaying every campaign one reply through the automated draft-and-gate flow. One reply set a condition: a recommendation from the recipient's own portfolio. The human reply offered Vitrue Health without establishing the portfolio connection. The automated gate, using the same facts, refused to imply the connection because no source on file stated it, and flagged it as the one input needed from the sender. Later confirmed: MMC co-led Vitrue Health's seed round in early 2024 and Simon Menashy was the partner quoted on the announcement. So the human reply met the condition, and the gate was also right to refuse the claim, because it was not in the research. Both things are true, and that is the design working: a claim that happens to be true but cannot be verified from the sources on file is still refused, and the remedy is always to enrich the sender profile or the research, never to loosen the gate. A fact-gate that accepts plausible claims because they are probably true is no gate at all.

Two operational implications:

- Sender profiles should carry a relationship block per campaign (past placements, in-portfolio work, warm contacts), because the strongest replies in campaign one won on privately-held facts that exist in no research doc. The reply handler asks for these via a SENDER INPUT NEEDED placeholder rather than inventing them.
- Reply handling is now automated as P7b in the runner (classify reply language and intent, draft by branch, gate with 4b logic). Reply language is recorded per piece automatically (first-reply-date, reply-language), feeding the leading-indicator tracking this manual already calls for.
