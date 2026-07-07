# Sentrada v0.1

Physical outreach built for one company at a time.

## What this project is

Sentrada generates AI-crafted, hyper-personalised physical artefacts for B2B sales teams. Each artefact is researched, designed, printed, and delivered to one person's desk. The recipient opens it, sees something made specifically about their situation, and books a meeting.

## Setup

npm install
cp .env.example .env
cp runner/config.example.json runner/config.json   # then fill in the sender block (see Prompt 0)

The chain runner (the production pipeline) authenticates through the logged-in
Claude CLI on a subscription; it needs no API key.

## Environment variables

Required for the chain runner: none.

Optional:
ANTHROPIC_API_KEY=        # only if runner/config.json sets "vision_backend": "sdk"
IMAGE_GEN_API_KEY=        # image-gen formats only (Claymation, parked)

Required for MCP connections:
NOTION_MCP_TOKEN=         # Notion integration token (campaign log, research outputs)
APOLLO_API_KEY=           # Apollo.io API key (contact data, company signals)

Future (not required yet):
PRINT_SUPPLIER_API_KEY=   # Print-on-demand supplier API
SHIPPING_API_KEY=         # Shipping/tracking API
HUBSPOT_API_KEY=          # For triggering follow-up sequences in client's outbound tool

## Verify setup

python runner/sentrada_runner.py ship-check --all
# Runs the print-readiness gate over existing pieces; confirms the runner,
# config and piece folders are healthy. For a full smoke test, run a
# generate --brief-only against a test research file.

## Validate output quality

A good Visual Brief has:
- A core problem you can picture as a scene (not "pipeline challenges" but "the Monday morning forecast review where the numbers don't match")
- A metric that makes you wince (not "low" but "42% of deals marked commit slipped")
- An environment specific enough to name the room (not "in a meeting" but "quarterly board review with external investors present")
- A moment that answers "what was said that caused the silence?" (not "they noticed an issue" but "the CFO asked why the forecast changed and nobody had an answer")
- At least 2 operational details that someone inside the company would recognise

If any of these are missing, the research is too thin. Flag for manual enrichment rather than proceeding.

## Run the pipeline (the chain runner)

Single piece:
python runner/sentrada_runner.py generate --name "Jane Doe" --title "VP Sales" \
    --company "Acme" --format newspaper|email|crossword \
    --research research/jane-doe-acme.md --brief-only
# approve the brief, then:
python runner/sentrada_runner.py generate --name "Jane Doe" --title "VP Sales" \
    --company "Acme" --format <same> --resume

Batches (the normal way to run):
python runner/sentrada_runner.py batch-brief --manifest research/<batch>.json
# read <batch>.review.md, edit <batch>.approvals.txt (APPROVE/SKIP per piece), then:
python runner/sentrada_runner.py batch-build --manifest research/<batch>.json

## Pipeline steps

1. INTAKE: recipient details + chosen format per recipient in the batch manifest
2. RESEARCH (Prompt 1): manual, in the Sentrada Claude Project with Research mode
   on for maximum depth; paste the output in. Ends with the Piece setup header and
   the structured DELIVERY block.
2b. RESEARCH GATE (Prompt 1b): the runner gates pasted research for completeness
   at ingestion (gate.json). Gaps surface in the batch review sheet; NOT_READY
   defaults the piece to SKIP in approvals.
3. BRIEF (Prompt 2): problem-evidence matching, Visual Brief. Human approval
   checkpoint (interactive, or approvals.txt in batches).
4. COPY (Prompt 4, per format): newspaper / email / crossword copy as engine JSON
4b. GATES (Prompt 4b + engine --check): factual grounding + content gate (personal
   detail, over-attributed proof, ambiguity) and layout fit; failures loop back
   into Prompt 4, max 3 attempts
5. RENDER: deterministic layout engine at 360 DPI (text guaranteed legible);
   each render is stamped with the engine build (engine_stamp.json)
6. REVIEW (Prompt 6, vision): craft QC; FAIL halts the chain
6b. RECIPIENT SIM (Prompt 6B, vision): predicted response; WOULD BIN halts
7. FOLLOW-UP (Prompt 7): companion card copy + 3-touch sequence, generated now,
   held until delivery confirmation. Card copy skipped when the sender writes
   their own (config sender "custom_card": true). Output passes the same Prompt
   4b grounding gate as printed copy (max 3 attempts; result in followup_gate.json)
8b. CARD: card/card.py renders the card copy to a print-ready A6 PNG at
   runner/pieces/{slug}/{slug}-card.png. Skipped if sender provided custom copy.
   Overflow past the 150-word cap is refused, not squeezed.
9. SHIP-CHECK: print-readiness gate (QC freshness, engine staleness, P6 FAIL,
   6B WOULD BIN, follow-up freshness vs data.json and the 6B verdict, plus a
   deterministic copy lint: em dashes, banned stock lines, missing Touch 1
   subject, connection-note length, grid-number refs, litigation keywords,
   duplicate Touch 2 openers across the batch) before anything is staged
10. DELIVERABLES: PNGs pushed to the deliverables branch; birch-csv generates the
   shipping CSV (never committed)
11. PRINT & SHIP: Birch prints and ships; tracking CSV comes back from Birch
12. FOLLOW-UP SEND: Touch 1 lands 24-48h after confirmed delivery, Touch 2 day
   3-4, Touch 3 day 7. Sent manually; every touch is tracked in the shared
   client doc (working process, in place)
13. OUTCOME: record what happened with `outcome` (writes the COMMITTED ledger
   runner/outcomes.json, which captures the 6B verdict at record time and
   survives across sessions — commit it after recording); compare predictions
   against reality with `calibration`

Data flows via files in runner/pieces/<slug>/: research.md, gate.json, brief.json,
data.json, the render PNG, qc_review.md, qc_recipient.md, followup.md,
followup_gate.json, <slug>-card.png, outcome.json. Prompt 7 derives the piece
reference and sender block at run time (from data.json and live config), never
from the build-time meta.json snapshot: post-build copy revisions and proof
updates must reach the follow-up. Cross-cutting copy rules live once in
runner/templates/house_rules.md and are injected wherever a template contains
{{house_rules}}; edit rules there, not per template.

Legacy: the original image-generation pipeline (/generate-artefact subagents,
Layer A/B/C prompt assembly, /pipeline/ campaign folders) now applies only to the
parked image-gen formats (Claymation).

## Model selection

Chain runner (runner/config.json "models"): p1b sonnet (research gate), p2 opus
(brief), p4 opus (copy), p4b opus (grounding), p6 opus (vision review), p6b opus
(recipient sim), p7 sonnet (follow-up).
Image-gen pipeline (parked formats): Opus for research/review/format; Sonnet for
brief/copy/prompt/followup.

## Format library (3 production formats, v10.0 — deterministic engines, A2 at 360 DPI)

Newspaper Front Page — newspaper/newspaper.py (Pango/Cairo on the upscaled
  template; 620-word lead, three sidebars, pull quote, hero stat)
The Email — email/email.py (procedural Gmail chrome; a sincere cold email printed
  at A2; the wink lives only in the P.S.)
Crossword — crossword/crossword.py + grid_generator.py (open grid built from
  25-30 researched answer/clue candidates; solvability guaranteed by construction)

Companion card — card/card.py (A6, Warm Stone/Lisbon Clay, ships in the box with
every piece; not a format, part of every send).

All production formats printed at A2 (594 x 420mm) on foam core, 360 DPI, sRGB.
Parked: Claymation Scene (image-gen path, signature aesthetic; revisit when image
  generation is needed), Horoscope, Postcard from the Future
Retired/archived: Cartoon, Board Game (archived/sentrada-boardgame-engine branch)
Future candidates: Magazine Cover, Vintage Ad Parody, Book Cover, Vintage Travel Poster

Image-gen formats keep their Layer A modules in .claude/skills/{format-name}/SKILL.md

## Adding a new format (engine-based, the production path)

1. Build a deterministic layout engine in its own directory, to the house
   contract: --data, --check (exit non-zero on fail, no render), --output,
   --print-dpi 360, *.FAILED quarantine, sRGB. Develop it in a dedicated CC
   session, as newspaper/crossword/email/card were.
2. Add runner/templates/prompt4_copy_<format>.md, a <format>_engine_data() /
   run_<format>_engine() pair, and a dispatch branch in _continue_after_brief.
3. Add the format to the accepted list in cmd_generate and _load_manifest, and to
   Prompt 2's format weighting.
4. Update this format library section and bump the version.

## Three-layer prompt architecture (v3.0 — image-gen formats only)

Applies to the parked image-gen formats (Claymation). The three production
formats are engine-rendered from prompt4 copy templates instead.

Layer A: Format module (swappable per format, defines visual rules)
Layer B: Campaign inputs (recipient, problem, metric, environment, moment, function, industry, operational details)
Layer C: Variation variables (auto-assigned: scene archetype, people visibility, metric surface, camera style, mood, tone)

## Visual Brief fields (output of research + bridge)

1. {CORE_PROBLEM}: Lived experience of the problem. Concrete, not jargon.
2. {KEY_METRIC}: Most uncomfortable number. Always specific (%, ratio, currency, count). Never "low" or "declining."
3. {ENVIRONMENT}: Specific setting where the problem is painfully visible. Not "at their desk."
4. {MOMENT}: Exact instant the problem becomes undeniable.
5. {FORMAT_RECOMMENDATION}: Strongest format with reasoning.
6. {OPERATIONAL_DETAILS}: 1-3 insider details that make the output feel observed.
7. {PROBLEM_LABEL}: 2-4 word noun phrase condensed from core problem.

## Quality criteria (review-agent checks these)

1. Could this have been made for any company? If yes: FAIL
2. Is the key metric readable? If no: FAIL
3. Would the recipient show this to a colleague? If uncertain: iterate
4. Does it match the selected format's visual rules? If no: FAIL
5. Are there AI artefacts, weird text, or uncanny elements? If yes: FAIL
6. The prestige object test: Is this something the recipient could plausibly want to keep and display on their desk or wall? Not a clever brochure. An artefact they'd lean against their monitor or hang up. If it feels disposable: FAIL

## Gotchas (add to this list as failure patterns emerge)

- When a gate passes something it shouldn't, first ask whether the gate ever SAW
  it. The copy-text builders (newspaper_copy_text, crossword_copy_text,
  email_copy_text) define P4b's field of vision; any recipient-visible text zone
  they omit is ungated by construction. A fabricated edition-line city shipped
  through exactly this blind spot, and stat_source and the email's custom
  postscript had the same gap. When an engine gains a new rendered text field,
  add it to the copy-text builder in the same commit.

Production-format lessons now live as rules inside the prompt templates
(runner/templates/) and their Notion sources; the gotchas below mostly concern
the image-gen pipeline (parked formats).

- AI image generators often produce UNREADABLE TEXT in the key metric area. Always verify metric legibility before approving. If text is garbled, regenerate with explicit instruction: "The text [exact metric] must be clearly legible."
- The research prompt sometimes produces ABSTRACT PROBLEMS like "pipeline challenges" instead of concrete moments. If {CORE_PROBLEM} reads like a category label rather than a lived experience, push back: "Rewrite as the specific moment this problem becomes visible to [recipient title]."
- Recipe Card format requires DRY WIT, not broad humour. The copy-agent tends to make ingredients too jokey. Calibrate: "A Recipe for Pipeline Collapse" is good. "A Yummy Recipe for Sales Disaster LOL" is bad.
- The format-agent sometimes defaults to Dramatic Poster regardless of inputs. If seeing Dramatic Poster recommended more than 40% of the time, the format selection prompt needs reweighting.
- Board Memo format is TEXT ONLY. Do not send it through image generation. The copy-agent output IS the artefact.
- When research finds LIMITED PUBLIC DATA on a company, the Visual Brief fields will be thin. Flag these for human enrichment rather than generating a weak artefact. A mediocre artefact is worse than no artefact.

## Copy rules (apply to all text output)

- No em dashes
- British English
- No exclamation marks in professional copy
- No soft filler phrases
- Never lead with "AI" in positioning
- Lead with the answer, no preamble
- The sender profile (proof points, what they sell) is a HUMAN-OWNED input: written
  or verbatim-approved by the sender, never drafted by the pipeline. Copy may quote
  it, never extend it, and never attribute its results to a specific piece or format
- Piece content uses only the recipient's public professional footprint (the
  self-published test). Personal facts gathered for delivery never become content

## Key commands (chain runner)

python runner/sentrada_runner.py ...
  generate      one piece: gate -> brief checkpoint -> copy -> gates -> render -> QC -> P7 -> card
  batch-brief   phase 1 over a manifest: research gate + brief per recipient, review sheet + approvals file
  batch-build   phase 2: build every APPROVEd piece (isolated subprocess per piece)
  qc            re-run Prompts 6 + 6B against a final image
  followup      re-run Prompt 7 for a piece
  ship-check    print-readiness gate; run before staging anything for print
  gate-probe    regression-test the copy gates: plants known violations in a
                clean piece's data and asserts P4b + the lint catch them; run
                after any edit to prompt4b, house_rules or a copy-text builder
                (--lint-only for the free deterministic subset)
  birch-csv     shipping CSV for the print supplier (holds addresses; never commit)
  outcome       record what happened to a sent piece (replied/meeting/no_response/...)
  calibration   compare 6B predictions against recorded outcomes

Human-run prompts (not runner-invoked): Prompt 0 (sender onboarding, produces the
config.json sender block), Prompt 1 (research, in the Sentrada Claude Project).

Legacy slash commands (/generate-artefact, /research-company, /review-output,
/write-followup, /log-campaign) belong to the image-gen pipeline (parked formats).

## Project structure

runner/            # THE PRODUCTION PIPELINE: sentrada_runner.py, templates/
                   # (prompt templates mirrored from Notion), config.json (sender
                   # profile + engine paths; committed), sender.*.json (parked
                   # sender profiles for swapping between clients; committed so
                   # they survive container resets), pieces/ [GITIGNORED]
newspaper/         # Newspaper layout engine (Pango/Cairo + upscaled template)
crossword/         # Crossword engine + grid generator + upscaled template
email/             # The Email engine (procedural Gmail chrome)
card/              # Companion-card engine (A6) + bundled fonts and wordmark
research/          # Per-recipient research + batch manifests + Birch CSVs [GITIGNORED]
.claude/
  agents/          # Subagents (image-gen pipeline; parked formats)
  commands/        # Slash commands (image-gen pipeline; parked formats)
  skills/          # Format modules and pipeline skills
  hooks/           # PostToolUse and Stop hooks
  settings.json    # Permissions, model config
.mcp.json          # MCP server connections (Notion, Apollo)
/pipeline/         # Image-gen pipeline campaign data [GITIGNORED]
CLAUDE.md          # This file

## Git tracking

Committed (core IP):
  runner/ (sentrada_runner.py, templates/, config.json — keep it secret-free —
  and outcomes.json, the cross-session outcome ledger)
  newspaper/, crossword/, email/, card/ (the layout engines + their templates/assets)
  .claude/ (agents, commands, skills, hooks, settings)
  .mcp.json
  CLAUDE.md
  package.json, .env.example
  /src/ (platform code when built)

Gitignored (generated/secrets):
  runner/pieces/ (per-piece output), research/ (personal data + addresses)
  *-birch.csv, birch-*/ (shipping CSVs and staged print folders hold addresses)
  /pipeline/ (generated output per campaign)
  .env (API keys and secrets)
  node_modules/
  deliverables/ (local staging area for print files; never committed to code branches)

## Print-ready deliverables (the `deliverables` branch)

Birch (the print supplier) needs the lossless PNGs, not JPGs, every run. They
live on a dedicated orphan branch named `deliverables`, never on code branches.
Code branches stay free of the large binaries; Birch gets one stable link.

Standing process for each run:
0. Run `python runner/sentrada_runner.py ship-check --manifest <batch.json>` (or
   `--all`) first. It holds any piece whose render is newer than its vision QC
   (re-rendered after QC, e.g. a manual subtitle fix or DPI re-render), is
   missing QC, is P6 FAIL, or is 6B WOULD BIN, and exits non-zero. Clear every
   hold (re-run `qc` against the delivered PNG) before staging anything.
1. Stage that run's final PNGs into a local deliverables/batch-YYYY-MM-DD/ folder:
   the artefact (runner/pieces/<slug>/<slug>.png) and the companion card
   (runner/pieces/<slug>/<slug>-card.png, from step 8b), plus a companion-cards.md.
2. Push them to the `deliverables` branch under batch-YYYY-MM-DD/ as a chain of
   small commits (one file per commit). The git proxy rejects a single pack over
   ~100MB with HTTP 413, so push file by file; ~90MB packs go through fine.
3. Link Birch to github.com/joetrajectory/sentrada-one-pager/tree/deliverables/batch-YYYY-MM-DD
4. JPEGs are only ever for in-chat mobile previews, never for print.
5. Generate the shipping CSV: `python runner/sentrada_runner.py birch-csv
   --manifest <batch.json>` (columns code,recipient,company,delivery_address,
   notes,file_stem). Delivery addresses come from the structured DELIVERY block
   (DELIVERY_STATUS / DELIVERY_ADDRESS / DELIVERY_NOTES) the research agent emits
   at the end of each research.md, never scraped from prose. BLOCKED pieces are
   left blank, CONFIRM_FIRST pieces are flagged in notes. A manifest "delivery":
   {"address": "...", "notes": ""} block overrides the research (human
   correction). The CSV is written beside the manifest and is gitignored.
   Send it to Birch DIRECTLY. NEVER commit it or push it to the deliverables
   branch: it holds delivery addresses (same reason research/ is gitignored).
   Use --stage-pngs to also copy each artefact and companion card to
   <file_stem>.png / <file_stem>-card.png (code-named to match the CSV) for Birch.

A dedicated separate repo is not possible from the remote container: the GitHub
App is scoped to this one repo (cannot create or push to another) and the git
proxy only routes this repo's path. The orphan branch is the substitute.

## External connections

Notion: Sentrada workspace (campaign log, research outputs, client records)
Apollo: Contact data and company signals
Image gen (parked formats only): OpenAI ChatGPT image generation (primary), Nana Banana 2 (backup)
Print: TBD supplier API
Shipping: TBD tracking API

## Full documentation

Prompt Architecture v3: Notion workspace > Prompt Architecture v3: Modular Format System
Research prompts: Notion workspace > Prompt 1: Research Agent, Prompt 2: Brief Agent, Prompt 3: Format Agent
Automation plan: Notion workspace > Claude-Native Automation Plan
Brand and positioning: Notion workspace > Brand

## Brand context

Name: Sentrada (sen-TRAH-da)
Domain: sentrada.io
Redirect: nobodyresponds.com > sentrada.io
Palette: Charcoal, Lisbon Clay (terracotta), Lisbon Stone, White
Positioning: "Physical outreach built for one company at a time."
Pain line: "Nobody responds to your cold outbound. We fix that."
