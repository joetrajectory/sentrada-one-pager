# Handover notes for the integration session

For whoever wires `p7b-reply-handler/` into the chain runner's main. The build session is archived; everything needed is in this folder plus these notes.

## Integration steps

1. Copy `runner/reply.py` and the three `runner/templates/prompt7b_*.md` files into the runner repo's matching locations.
2. Register a `reply` subcommand in `sentrada_runner.py` that calls `reply.main()` (or leave it invocable as `python runner/reply.py`). When `sentrada_runner.call_model` is importable, reply.py uses it automatically and the built-in model layer is dead code.
3. Add model config keys if desired: `p7b_classify` (default claude-sonnet-5), `p7b_draft` (default claude-fable-5), `p7b_gate` (default claude-opus-4-8). The built-in fallback degrades draft/gate calls to Opus with a stderr note when the configured model is unavailable.
4. Delete this branch of the public one-pager repo once copied. The templates should not stay public.

## Verify against real runner conventions (assumed, not confirmed)

The build session never saw the runner repo. These were inferred from the Notion specs and must be checked on first contact:

- Piece-folder file names: `research.md`, `brief.md|json`, `copy.md`/`data.json`, `factcheck.md`, `qc_package.md`/`qc_recipient.md`, `followup.md`, `meta.json`. Loaders are tolerant (first match wins) but check nothing is missed.
- Template placeholder syntax: these templates use `{{name}}`. If the runner's templates use a different convention, align them.
- Touch-sent tracking keys assumed: `touch1_sent_date` / `touch1_sent` (etc.) in meta.json; `--touches-sent 1,2` overrides.
- Open-loop record read from `meta.json` key `open_loop` with fields answer, clue, clue_number, direction, metric, question, tier, tier_A_number (per the 5 July implementation record). `open_loop_fallback` respected.
- Sender profile read from config `sender` block: name, company, what_they_sell, proof_points, measurement_capabilities, booking_link.
- `house_rules.md` is injected from `runner/templates/`; this folder deliberately does not ship one to avoid overwriting the real file.

## Genuinely unfinished

- **Problem-language branch untested live.** Validated in template and code only; no real converting reply existed to replay. Watch the first one.
- **First real-piece run.** Live validation used research stand-ins thinner than real piece folders; run one real piece end to end after wiring and review the gate's behaviour on rich research.
- **Record keeping beyond the piece folder.** first-reply-date and reply-language land in meta.json and `replies/` per the brief; nothing writes to the campaign log / Notion tracking table yet. Wire that when the stats work lands.
- **LinkedIn channel is length-rules only.** No LinkedIn-specific validation case existed; all six historical replies were email.
- **Validation harness** (`validation/run_validation.py`) works and is how the six campaign cases were replayed; real case folders must stay out of public repos (third-party correspondence).

## Design decisions that should survive integration

- Missing `research.md` refuses to run (a reply that cannot be fact-checked must not be drafted).
- Deterministic lint runs before the model gate (em dash, exclamation, subject line, the word "gift", generic openers, length caps 130 words email / 60 LinkedIn).
- One gate failure regenerates with violations fed back; a second HALTs showing both drafts. Same shape as the chain's 4b.
- The gate refusing true-but-unsourced claims is correct behaviour (see VALIDATION.md, the Vitrue lesson). Enrich sources, never loosen the gate.
- Auto-replies draft nothing and do not touch first-reply stats. Rejections can output NO SEND RECOMMENDED.

---

## Integration verification (17 July 2026, on main)

Every "assumed, not confirmed" item above was checked against the real runner
during integration:

- **Piece-folder file names: CONFIRMED.** The runner writes research.md,
  brief.md/brief.json, copy.md + data.json, factcheck.md, qc_review.md,
  qc_recipient.md, qc_package.md, followup.md, meta.json. The tolerant loaders
  match them all.
- **Placeholder syntax: CONFIRMED.** The runner's templates use {{name}};
  no alignment needed. reply.py keeps its own fill (missing -> "(none)") and
  its own template loader, so the runner's {{house_rules}} include never
  double-expands.
- **Touch-sent keys: NOT WRITTEN BY THE RUNNER.** Nothing in
  sentrada_runner.py writes touch1_sent_date/touch1_sent; touch sends are
  tracked in the shared client doc (pipeline step 12). So --touches-sent is
  the authoritative input; the meta keys are honoured if an operator adds them
  by hand, but absent an override reply.py will assume no touches sent.
- **Open-loop record: CONFIRMED EXACT.** meta["open_loop"] carries answer,
  clue, clue_number, direction, metric, question, tier, tier_A_number;
  open_loop_fallback is a reason string, truthy-checked, compatible.
- **Sender block: MISMATCH FOUND AND FIXED.** The real config names the
  sender "sender_name", not "name"; format_sender_facts read "name" and
  silently dropped the sender's own name from the sender facts. Fixed
  ("sender_name" first, "name" kept as fallback).
- **Model layer: replaced at integration.** reply.py now calls the runner's
  cli_text directly (retries + usage accounting + subscription auth); the
  built-in API/CLI fallback layer described above no longer exists. The
  unavailable-model degradation survives as a single opus fallback around
  cli_text. Defaults: p7b_classify sonnet, p7b_draft opus, p7b_gate opus.
