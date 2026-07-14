# P7b: reply handler

A new runner command, `reply`. It drafts the sender's response when a recipient replies to a delivered piece. It never sends anything: the operator copies, edits and sends.

This folder mirrors the runner repo's layout so it can be copied straight over:

```
runner/reply.py                         the command
runner/templates/prompt7b_classify.md   step 1 (Sonnet)
runner/templates/prompt7b_reply.md      step 2 (best model)
runner/templates/prompt7b_reply_gate.md step 3 (Opus, 4b logic + house rules)
validation/run_validation.py            campaign-replay harness
validation/cases/example-synthetic/     case shape (synthetic data only)
```

**Move this into the private runner repo and delete this branch after copying.** This repo is public; the templates are Sentrada IP, and real validation cases (which contain third-party correspondence) must never be committed here.

## Usage

```
python runner/reply.py <piece-id> [--channel email|linkedin] [--reply-file path]
```

Then paste the reply (or the whole thread, latest message last) and finish with Ctrl-D or a line containing only `END`. `--channel` defaults to email.

Output: the draft, plus one line stating the classification and the rule branch taken. On a referral, the new name is flagged for routing; no research is improvised.

## What it loads from the piece record

`research.md` (required; the gate cannot run without it), `brief.md|json`, `copy.md`/`data.json`, `factcheck.md` (warned if missing), `qc_package.md`/`qc_recipient.md` (6B), `followup.md`, and `meta.json` for the crossword open-loop record (`answer, clue, clue_number, direction, metric, question, tier, tier_A_number`) and touch-sent dates (`touch1_sent_date` etc., overridable with `--touches-sent 1,2`). `house_rules.md` is injected from `runner/templates/`. Sender facts come from config (`sender` block).

## The three steps

1. **Classify** (config key `p7b_classify`, Sonnet). Reply language: gift / problem / mixed / none. Intent: interested, question, objection, polite_deflection, rejection, referral, auto_reply. The last two intents were added from campaign one data: a flat "No thank you" is not a polite deflection, and two of the eight historical replies were out-of-office autoresponders that must not get a drafted reply.
2. **Draft** (config key `p7b_draft`, best model available). Gift language bridges from the craft to the tension the piece named. Problem language accelerates towards the ask. Crossword pieces deploy the holdback where it fits naturally (Tier A reveals, Tier B offers to measure; clue referenced by printed number; never re-explained if a sent touch already deployed it). Referrals thank and route, and the new name is flagged rather than researched from nothing. House rules plus the outbound reply rules are injected: British English, no em dashes, no exclamation marks, no subject lines on replies, never a generic opener, body connects back to the tension. Replies are shorter than first touches; LinkedIn shorter still. Where the winning reply needs a fact only the sender holds, the draft carries `[SENDER INPUT NEEDED: ...]` instead of inventing or writing around it (this rule came directly from the campaign one validation: the best human reply won on privately-held relationship facts no research contained).
3. **Gate** (config key `p7b_gate`, Opus, same tier as the chain's 4b). Fact errors only: unsupported claims (including true-in-the-world facts absent from the research and unsourced industry commonplaces), contradictions, stripped qualifiers, misattribution; plus the house rules. Deterministic lint runs first (em dash, exclamation, subject line, "gift", generic openers, length caps: 130 words email / 60 LinkedIn). One failure regenerates with the violations listed. A second failure HALTs and prints both drafts with their violations, same behaviour as the chain.

## Record keeping

On the first reply, `first_reply_date` and `reply_language` are written to `meta.json` (the Operating Manual's leading conversion indicator). Every run writes `replies/reply-NNN/` in the piece folder: `reply.txt`, `classification.json`, `draft_attemptN.md`, `draft.md` (accepted), `gate.json`, and `HALTED.json` on a double failure, so stats and the monthly review can read them.

## Integration with the chain runner

Drop `reply.py` and the three templates into the runner repo. If `sentrada_runner.call_model` is importable it is used automatically (same retries, same config); otherwise the built-in fallback uses the Anthropic API when `ANTHROPIC_API_KEY` is set, else `claude -p` headless (the Max subscription path). To register it as a subcommand instead of a separate script, add a `reply` subparser in `sentrada_runner.py` that calls `reply.main()`.

Default models (override in config under `models`): `p7b_classify: claude-sonnet-5`, `p7b_draft: claude-fable-5`, `p7b_gate: claude-opus-4-8`.

## Validation

`python validation/run_validation.py --cases <local-folder>` replays historical replies through the real command and writes `out/comparison.md` with the tool's draft next to what was actually sent. Case folder shape is in `cases/example-synthetic/`. Build the real campaign one cases locally from the piece records; do not commit them.
