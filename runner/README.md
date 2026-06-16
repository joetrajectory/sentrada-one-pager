# Sentrada chain runner

Runs the Sentrada prompt chain to produce one-of-one physical outreach pieces. It
feeds the newspaper layout engine in `../newspaper/`, or assembles a paste-ready
claymation image prompt for you to run by hand.

Model calls go through the **Claude Code CLI in headless mode (`claude -p`)**, so
they draw on your **Max subscription's credit pool** rather than a pay-per-token
API key. The only prerequisite is being logged in (`claude /login`).

Research (Prompt 1) happens **outside** this runner: you do the deep research
yourself and paste it in as a file. The runner does the rest, pausing once for
your approval of the brief.

## What it does, in order

1. **Prompt 2 (brief)** via Claude. Prints the snapshot and the seven brief
   fields, then **stops and waits**. Type `approve` to continue, or paste
   feedback to regenerate the brief. Nothing proceeds without approval.
   - If the brief declares a **poor fit**, the runner halts with a clear message.
2. **Prompt 4 (copy)** via Claude, branching by format:
   - **Newspaper:** Claude returns the layout engine's flat JSON schema. The
     runner checks the copy against a strict gate (lead article 600–640 words,
     exactly 3 sidebar stories, each body 60–80 words). On a violation it reruns
     Prompt 4 **once** quoting the violations; if it fails twice it halts. It then
     runs the engine's own `--check`, and only if that passes does it render the
     print-ready A2 newspaper PNG.
   - **Claymation:** Claude returns the scene copy. **Prompt 5** then assembles
     the final image-generation prompt and writes it to `image_prompt.txt` for you
     to paste into ChatGPT and upscale by hand.
3. Writes everything to the piece folder and prints the **fact check list** for
   your sign-off before print.

`qc` and `followup` are separate commands you run by hand when you need them.

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

- `--format` is `newspaper` or `claymation`.
- `--research` is a plain text/markdown file with the deep research you ran.
- Output lands in `runner/pieces/jane-doe-acme-corp/`.

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
<name>-<company>.png the print-ready A2 newspaper at 300 DPI
# claymation:
claymation_copy.json scene, caption, in-scene text, hook
image_prompt.txt     paste-ready image prompt (run + upscale by hand)
# after qc / followup:
qc_review.md         Prompt 6 verdict
qc_recipient.md      Prompt 6B simulation
followup.md          Prompt 7 card + follow-up
```

## Where the prompts live

Every prompt is a template in `runner/templates/`, extracted from the single
source file `../sentrada-prompts-all-v4.md`. **Edit the prompts there**, not in the
code. The runner only fills `{{placeholders}}`, calls the API, gates the output,
and drives the engine.

| Template | Prompt |
|---|---|
| `prompt2_brief.md` | Brief Agent |
| `prompt4_copy_newspaper.md` | Copy Agent (newspaper → engine schema) |
| `prompt4_copy_claymation.md` | Copy Agent (claymation) |
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
