# Sentrada v0.1

Physical outreach built for one company at a time.

## What this project is

Sentrada generates AI-crafted, hyper-personalised physical artefacts for B2B sales teams. Each artefact is researched, designed, printed, and delivered to one person's desk. The recipient opens it, sees something made specifically about their situation, and books a meeting.

## Setup

npm install
cp .env.example .env

## Environment variables

Required:
ANTHROPIC_API_KEY=        # Claude API key (Opus + Sonnet access)
IMAGE_GEN_API_KEY=        # Midjourney/Flux API key

Required for MCP connections:
NOTION_MCP_TOKEN=         # Notion integration token (campaign log, research outputs)
APOLLO_API_KEY=           # Apollo.io API key (contact data, company signals)

Future (not required for Sprint 1):
PRINT_SUPPLIER_API_KEY=   # Print-on-demand supplier API
SHIPPING_API_KEY=         # Shipping/tracking API
HUBSPOT_API_KEY=          # For triggering follow-up sequences in client's outbound tool

## Verify setup

/research-company test-company
# Expected: Research output with all 7 Visual Brief fields populated
# If any field is missing or generic, the research prompt needs iteration

## Validate output quality

A good Visual Brief has:
- A core problem you can picture as a scene (not "pipeline challenges" but "the Monday morning forecast review where the numbers don't match")
- A metric that makes you wince (not "low" but "42% of deals marked commit slipped")
- An environment specific enough to name the room (not "in a meeting" but "quarterly board review with external investors present")
- A moment that answers "what was said that caused the silence?" (not "they noticed an issue" but "the CFO asked why the forecast changed and nobody had an answer")
- At least 2 operational details that someone inside the company would recognise

If any of these are missing, the research is too thin. Flag for manual enrichment rather than proceeding.

## Run the pipeline

/generate-artefact [company-name] [recipient-name] [recipient-title] [company-url]
# Chains all subagents automatically: research > bridge > format > copy > prompt > image > review

/generate-batch [input-file.json]
# Processes multiple companies sequentially, running the full Account Deep Research prompt on each

## Pipeline steps

1. INTAKE: Sender provides recipient details and problem description
2. RESEARCH: research-agent runs Prompt 1 (company research, evidence assessment, operational details). In manual testing: use Claude Research mode (toggle on) in the Sentrada Project for maximum depth. In automated pipeline: Claude API with web search tool.
3. BRIDGE: brief-agent runs Prompt 2 (problem-evidence matching, scoring, Visual Brief with 7 fields)
4. FORMAT: format-agent runs Prompt 3 (format recommendation from library of 4)
5. COPY: copy-agent writes text content (if text-heavy format)
6. PROMPT: prompt-agent assembles master image gen prompt from Layer A + B + C
7. IMAGE: Image generation API produces the visual artefact
8. REVIEW: review-agent assesses quality (pass/fail with retry logic, max 3 attempts)
8b. CARD: copy-agent generates companion card copy, then card/card.py (the A6 companion-card engine) renders it to a print-ready PNG at runner/pieces/{slug}/{slug}-card.png. Skipped if sender provided custom copy. Overflow past the 150-word cap is refused, not squeezed.
9. SENDER REVIEW: Sender sees artefact + companion card, approves or requests changes
10. PRINT: Approved artefact + companion card sent to print supplier
11. SHIP: Printed artefact + card packaged in premium box and shipped with tracking
12. FOLLOWUP: followup-agent generates personalised follow-up copy on delivery confirmation

Data flows via JSON files in /pipeline/{campaign-id}/. Each step reads the previous step's output file.

## Model selection

Use Opus for: research-agent, review-agent, format-agent (complex reasoning)
Use Sonnet for: brief-agent, copy-agent, prompt-agent, followup-agent (structured tasks)

## Format library (4 launch formats, v8.0 - all A2 on foam core)

Authority tier: Newspaper Front Page (validated)
Humour/Recognition tier: Board Game (their industry as a custom board game, photorealistic, most keepable)
Humour/Warmth tier: Claymation Scene (stop-motion Aardman style, most versatile, signature Sentrada aesthetic)
Impact tier: Postcard from the Future (validated for image gen, positive frame)

All formats printed at A2 (594 x 420mm) on foam core (5-6mm, rigid, self-standing).
Retired: Cartoon (too close to Heinecke's signature format, replaced by Claymation and Board Game)
Parked: Horoscope (visually stunning, conversion mechanism uncertain)
Future candidates: Magazine Cover, Vintage Ad Parody, Book Cover, Vintage Travel Poster

Each format has a Layer A module in .claude/skills/{format-name}/SKILL.md

## Adding a new format

1. Create .claude/skills/{format-name}/SKILL.md with the Layer A module (visual rules, title treatment, layout, aspect ratio)
2. Add the format to format-agent.md's scoring list with "when to recommend" guidance
3. Update the format library section above (increment count)
4. Add the format to the variable applicability matrix (which Layer C variables apply)
5. Bump format library version number

## Three-layer prompt architecture (v3.0)

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

## Key commands

/generate-artefact [company] - Full automated pipeline
/generate-batch [file] - Batch process multiple companies
/research-company [company] - Research only (for testing/debugging)
/review-output [image] - Quality review only
/write-followup [artefact] - Generate follow-up copy
/log-campaign [details] - Log to Notion

## Project structure

.claude/
  agents/          # Subagents (research, brief, format, copy, prompt, review, followup)
  commands/        # Slash commands (generate-artefact, generate-batch, etc.)
  skills/          # Format modules and pipeline skills
  hooks/           # PostToolUse and Stop hooks
  settings.json    # Permissions, model config
.mcp.json          # MCP server connections (Notion, Apollo)
/pipeline/         # Per-campaign data (JSON files per step, images, logs) [GITIGNORED]
CLAUDE.md          # This file

## Git tracking

Committed (core IP):
  .claude/ (agents, commands, skills, hooks, settings)
  .mcp.json
  CLAUDE.md
  package.json, .env.example
  /src/ (platform code when built)

Gitignored (generated/secrets):
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
   Use --stage-pngs to also copy each PNG to <file_stem>.png for Birch.

A dedicated separate repo is not possible from the remote container: the GitHub
App is scoped to this one repo (cannot create or push to another) and the git
proxy only routes this repo's path. The orphan branch is the substitute.

## External connections

Notion: Sentrada workspace (campaign log, research outputs, client records)
Apollo: Contact data and company signals
Image gen: OpenAI ChatGPT image generation (primary), Nana Banana 2 (backup)
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
