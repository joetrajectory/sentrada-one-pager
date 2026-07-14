# Sentrada chain runner

Runs the Sentrada prompt chain to produce one-of-one physical outreach pieces. It
feeds a deterministic layout engine — the newspaper engine in `../newspaper/` or
the crossword engine in `../crossword/` — or assembles a paste-ready claymation
image prompt for you to run by hand.

Model calls go through the **Claude Code CLI in headless mode (`claude -p`)**, so
they draw on your **Max subscription's credit pool** rather than a pay-per-token
API key. The only prerequisite is being logged in (`claude /login`).

Research (Prompt 1) happens **outside** this runner: you do the deep research
yourself and paste it in as a file. The runner does the rest, pausing once for
your approval of the brief.

## What it does, in order

There are **two human checkpoints**: you approve the brief, and you approve the
finished package for print. Everything else runs automatically.

1. **Prompt 2 (brief).** Prints the snapshot, the seven brief fields, and the
   held-back reserve detail, then **stops and waits**. Type `approve`, or paste
   feedback to regenerate. Nothing proceeds without approval.
   - If the brief declares a **poor fit**, the runner halts.
2. **Prompt 4 (copy)**, branching by format:
   - **Newspaper:** Claude returns the layout engine's flat JSON schema. The runner
     gates it on (a) word counts (lead 600–640, exactly 3 sidebars × 60–80 words)
     **and** (b) a **Prompt 4b factual-grounding check** — a text pass that flags any
     claim not supported by the research (names, places, numbers, dates). On any
     violation it reruns Prompt 4 **once** quoting them; if it fails twice it halts.
     It then runs the engine's `--check`, and only on pass renders the A2 PNG.
   - **Crossword:** Claude returns a title, subtitle, and 25–30 answer/clue
     candidates. The runner gates them on (a) candidate structure (25–30, single
     ALL-CAPS words 3–15 chars, unique, each clued) **and** (b) the same **Prompt 4b
     grounding check** on every clue. It then runs the crossword engine's `--check`
     (grid placement + clue fit); a layout failure feeds **back into Prompt 4** to
     shorten clues, up to 3 attempts, then renders the A2 PNG. The engine selects the
     best 15–20 candidates and builds the interlocking grid.
   - **Claymation:** Claude returns the scene copy (also grounding-checked), then
     **Prompt 5** assembles the paste-ready image prompt. The chain stops here (no
     rendered image to QC automatically); run `qc`/`followup` after you generate
     and upscale the image.
3. **Newspaper and crossword — automatic QC + follow-up chain after render:**
   - **Prompt 6** (vision) craft review. If **FAIL**, the chain stops and shows the
     reason and regeneration instructions; 6B and 7 do not run.
   - **Prompt 6B** (vision) recipient simulation. If **WOULD BIN**, the chain stops
     and shows the suppression flag; 7 does not run.
   - **Prompt 7** writes the companion card + 3-touch follow-up, grounded in the 6B
     output (for all other 6B verdicts).
4. Prints the **complete package** — render path, P6 verdict + flags, 6B verdict +
   highest-leverage change, companion card and follow-up — for your single
   print/no-print decision. Fact-check lists are written for your sign-off.

`qc` and `followup` remain standalone commands (rerun QC on a revised image, or
regenerate follow-up copy) — they share the same code the chain uses.

## Setup

```bash
# 1. Claude Code logged in (provides the subscription auth the runner calls).
claude /login          # one-time; no ANTHROPIC_API_KEY needed

# 2. Newspaper engine dependencies (Pango/Cairo + Python libs)
cd newspaper && ./setup.sh && cd ..

# 3. Your sender profile
cp runner/config.example.json runner/config.json
# then edit runner/config.json: your company, what you sell, proof points,
# booking link, your name, and the 2-3 problems your product solves.
```

No API key is required: every model call runs through `claude -p` on your
subscription. (The one exception is if you set `"vision_backend": "sdk"` in
config — see Models below — which bills QC per-token and needs
`ANTHROPIC_API_KEY` plus `pip install -r runner/requirements.txt`.)

`runner/config.json` and `runner/pieces/` are gitignored.

## Run a piece

```bash
python runner/sentrada_runner.py generate \
  --name "Jane Doe" --title "VP Sales" --company "Acme Corp" \
  --format newspaper \
  --research path/to/your-research.md
```

- `--format` is `newspaper`, `crossword`, or `claymation`.
- `--research` is a plain text/markdown file with the deep research you ran.
- Output lands in `runner/pieces/jane-doe-acme-corp/`.

For a crossword, swap `--format crossword`:

```bash
python runner/sentrada_runner.py generate \
  --name "Jane Doe" --title "VP Sales" --company "Acme Corp" \
  --format crossword \
  --research path/to/your-research.md
```

The crossword engine (`../crossword/`) selects the best 15–20 of Prompt 4's
candidates, builds a British-style interlocking grid, and renders the A2 PNG — same
automatic QC + follow-up chain as newspaper.

You will be asked to approve the brief once. After that the piece runs to
completion on its own.

### Quality control (after you have a final image)

For newspapers the final image is the rendered PNG. For claymation it is the
image you generated from `image_prompt.txt` and upscaled.

```bash
python runner/sentrada_runner.py qc \
  --folder runner/pieces/jane-doe-acme-corp \
  --image runner/pieces/jane-doe-acme-corp/jane-doe-acme-corp.png
```

Runs Prompt 6 (craft review) and Prompt 6B (recipient simulation) with vision and
writes `qc_review.md` and `qc_recipient.md`.

### Follow-up sequence

Run `qc` first so the follow-up is grounded in the 6B simulation.

```bash
python runner/sentrada_runner.py followup \
  --folder runner/pieces/jane-doe-acme-corp \
  --delivery-date "16 June 2026"
```

Runs Prompt 7 and writes `followup.md` (companion card + 3-touch sequence +
reception nudge + fact check list).

## Batch (many recipients at once)

Batch fans the same chain across a list of recipients, each with its own
pre-prepared research file. It keeps **both human checkpoints** by running in two
phases, and isolates failures — one piece halting never stops the batch.

You provide a JSON manifest (see `batch.example.json`); research paths are relative
to the manifest:

```json
[
  {"name": "Chris Evans", "title": "CRO", "company": "Cognism",
   "format": "newspaper", "research": "research/cognism.md"},
  {"name": "Jane Doe", "title": "VP Sales", "company": "Acme Corp",
   "format": "claymation", "research": "research/acme.md", "delivery_date": "30 June 2026"}
]
```

**Phase 1 — briefs:**
```bash
python runner/sentrada_runner.py batch-brief --manifest mybatch.json
```
Runs Prompt 2 for everyone and writes, next to the manifest:
- `mybatch.review.md` — every brief (snapshot + seven fields) to read in one pass.
- `mybatch.approvals.txt` — one `APPROVE`/`SKIP` line per piece (poor-fit and
  errored briefs default to `SKIP`). **Edit the first word per line**, then:

**Phase 2 — build the approved pieces:**
```bash
python runner/sentrada_runner.py batch-build --manifest mybatch.json
```
For each `APPROVE`, runs copy → grounding gate → engine check → render → P6 → P6B →
P7 (newspaper and crossword), or copy → grounding → P5 paste-ready prompt
(claymation). Writes `mybatch.summary.md`:
per-piece status (ready / **held: P6 FAIL** / **held: 6B WOULD BIN** / error), the
P6 and 6B verdicts, and the total credit used. Held pieces are flagged for your
attention rather than silently shipped.

To **revise** one brief, re-run it singly (`generate ... --brief-only --feedback
"notes"`), then set it to `APPROVE`. Pieces run **sequentially**; a batch of 20 is
roughly 45–90 minutes and ~$20–40 of subscription credit.

## What lands in a piece folder

```
research.md          your pasted research (the input)
brief.md             Prompt 2 full working
brief.json           the seven brief fields, structured
copy.md              Prompt 4 written copy
factcheck.md         claims + sources, for your sign-off before print
meta.json            recipient + format + sender, so qc/followup run standalone
# newspaper:
data.json            engine input (engine schema fields ONLY)
<name>-<company>.png the print-ready A2 newspaper at 360 DPI
# crossword:
data.json            engine input (company_name, subtitle, min/max, seed, candidates)
<name>-<company>.png the print-ready A2 crossword at 360 DPI
# claymation:
claymation_copy.json scene, caption, in-scene text, hook
image_prompt.txt     paste-ready image prompt (run + upscale by hand)
# after qc / followup:
qc_review.md         Prompt 6 verdict
qc_recipient.md      Prompt 6B simulation
followup.md          Prompt 7 card + follow-up
```

## Address capture (the tease flow)

For remote-likely targets whose desk address you do not have: print the piece,
tease its existence, and let the recipient hand you the address on a one-off
page at sentrada.io/for/<token>. One-time setup: `SENTRADA_RUNNER_SECRET=...`
in `.env` (matching the deployment's `RUNNER_SECRET`); the API base comes from
config `capture_api`.

```bash
# 1. The tease claims one copy exists, so that must be true first.
python runner/sentrada_runner.py printed --piece jane-doe-acme

# 2. Generate the link (refuses if not marked printed). Prints the URL once;
#    the ledger keeps only a hash. Tokens expire after 30 days.
python runner/sentrada_runner.py tease --piece jane-doe-acme --channel linkedin --variant mystery

# 2b. Teaser-image variant: crop a corner of the render for inline embedding.
python runner/sentrada_runner.py teaser --piece jane-doe-acme            # --corner tr/bl/br to reframe

# 3. Day to day: sync submissions, see statuses, flags and the share rate.
#    Flags: NUDGE-DUE 5 days after the tease, SWAP-RECOMMENDED 7 days after
#    the nudge. Advisory only; you decide.
python runner/sentrada_runner.py capture

# 4. Record the nudge when you send it; swap when you decide to.
python runner/sentrada_runner.py nudge --piece jane-doe-acme
python runner/sentrada_runner.py swap --piece jane-doe-acme

# 5. An address arrived (email notification or capture sync): print it for the
#    Birch CSV or a manifest delivery override. Terminal only, never a file.
python runner/sentrada_runner.py address --piece jane-doe-acme

# 6. Signed for: delete the address from the store, everywhere, for good.
python runner/sentrada_runner.py delivered --piece jane-doe-acme
```

State lives in `runner/capture.json`, committed like `outcomes.json` so it
survives ephemeral containers. It holds statuses, dates, channel, variant and
token hashes; it never holds addresses or raw tokens. Commit it after every
capture command.

Guarantees, and the probe that enforces them: the notification email never
contains the address; a re-tease can never orphan a submitted address (409);
`delivered` leaves the store empty; every store key self-deletes after 90 days
as a backstop; token pages are noindexed and rate-limited. After any edit to
`api/` or the capture commands, re-run the regression probe (it spins up
`tools/capture-harness.js`, the real functions over a mock store, and needs
only node):

```bash
python runner/sentrada_runner.py capture-probe
```

## Where the prompts live

**`runner/templates/*.md` is the single source of truth — these are what the
runner executes.** Edit prompts here, never in the code. `../sentrada-prompts-all-v4.md`
is a reference/design doc only; it may lag and the runtime never reads it. The
runner just fills `{{placeholders}}`, calls `claude -p`, gates the output, and
drives the engine.

| Template | Prompt |
|---|---|
| `prompt2_brief.md` | Brief Agent |
| `prompt4_copy_newspaper.md` | Copy Agent (newspaper → engine schema) |
| `prompt4_copy_crossword.md` | Copy Agent (crossword → answer/clue candidates) |
| `prompt4_copy_claymation.md` | Copy Agent (claymation) |
| `prompt4b_grounding.md` | Factual grounding gate (text fact-check on the copy) |
| `prompt5_assembly_claymation.md` | Assembly Agent (claymation image prompt) |
| `prompt6_review.md` | Review Agent |
| `prompt6b_recipient.md` | Recipient Agent |
| `prompt7_followup.md` | Followup Agent |
| `layer_a_claymation.md`, `layer_c.md` | Claymation format module + variation menu |

## Models

Per-prompt, set in `config.json` under `"models"` (change any without touching
code). Values are CLI aliases (`opus`/`sonnet`/`haiku`) or full IDs:

| Prompt | Default | Why |
|---|---|---|
| `p2` brief | `opus` | Picks the problem angle. Quality decides whether the piece earns a meeting. |
| `p4` copy | `opus` | Writes the words on the physical piece. |
| `p4b` grounding | `opus` | Fact-checks the copy against the research. Accuracy is the product. |
| `p5` claymation assembly | `sonnet` | Mechanical prompt assembly. |
| `p7` follow-up | `sonnet` | Template-driven transformation. |
| `p6` review (vision) | `opus` | Last check before print. |
| `p6b` recipient sim (vision) | `opus` | Last check before print. |

All calls run via `claude -p` on your subscription. The headless invocation uses
a neutral working directory (so the project `CLAUDE.md` never biases generation)
and `--append-system-prompt` to keep the call to single-shot output; `--bare` is
deliberately avoided because it breaks subscription auth.

**Vision backend.** `"vision_backend": "cli"` (default) runs QC through `claude -p`
with the Read tool on your subscription. Set `"vision_backend": "sdk"` to route
P6/P6B through the Anthropic Python SDK instead — this bills per-token and needs
`ANTHROPIC_API_KEY` and the `anthropic` package.

**Reliability.** `claude -p` returns text, so the runner extracts the fenced
`json` block itself and retries (up to 3x) with a reminder if a reply is missing
or malformed; transient call failures retry up to 3x before halting.

## Notes and gotchas

- **The newspaper gate is stricter than the engine.** The runner requires sidebar
  bodies of 60–80 words and a 600–640 word lead; the engine itself tolerates
  40–110 and 580–660. Older worked examples (e.g. `newspaper/mmc.json`) have
  shorter sidebars and would be reruns under the runner's gate. This is intended.
- **`data.json` contains engine schema fields only.** The fact check list and any
  other prose are written to separate files, never into the engine input.
- **Claymation image generation and Magnific upscaling stay manual** by design.
  The runner produces the paste-ready prompt and stops.
- **Final factual sign-off is yours.** The runner's last act per piece is writing
  the files and printing the fact check list; review the rendered piece against it
  before print.
- The newspaper template is the 25mb upscaled JPG by default; change it in
  `config.json` (`newspaper_template`).
