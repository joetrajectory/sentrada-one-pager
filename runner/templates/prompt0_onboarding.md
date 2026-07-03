<!-- HUMAN-RUN PROMPT. The runner never invokes this. Run it in a Claude chat when
onboarding a new sender (a client, or Sentrada itself), paste in the raw material,
and the output is the "sender" block for runner/config.json. Canonical copy lives
in Notion (Prompt 0: Sender Onboarding); keep the two in sync. -->

You are onboarding a new sender onto Sentrada, a physical outreach platform that
builds deep-researched, one-of-one printed pieces and delivers them to senior B2B
buyers' desks. Your job is to turn raw material about the sender into the exact
sender profile the pipeline consumes, and to get every claim in it verbatim-approved
by the sender before it is used.

The profile is a HUMAN-OWNED input. The pipeline quotes it; it never extends it.
Anything in it will be repeated on printed pieces and in follow-up emails under the
sender's name, so a wrong or inflated claim here becomes a printed lie later. When
in doubt, leave it out.

**Raw material (paste whatever exists):**
[CALL TRANSCRIPT / MEETING NOTES / WEBSITE COPY / DECK / EMAIL THREAD]

**Sender basics:**
Sender name (the person who signs): [FULL NAME]
Company: [COMPANY]
Email: [EMAIL]
Phone for the companion card: [PHONE]
Booking link: [URL]

---

## Extract these five things

1. **What they sell**, in plain language, from the BUYER's side of the table. Not
   the category, not the feature list: what a buyer gets and why it matters. One
   to three sentences. Test: would a sceptical CRO recognise their own problem in
   this description?

2. **The 2-3 problems they solve**, written from the buyer's perspective. These
   drive the research and brief for every piece, so they must be problems a
   specific recipient can visibly HAVE (evidence findable in public: hiring
   patterns, stated strategy, published metrics), not vague pains. Each one:
   a sentence or two, concrete, evidence-findable.

3. **Proof points.** The strictest section. For each candidate proof:
   - It must be TRUE, specific, and the sender must be able to stand behind it in
     a live meeting ("you say here you saved X £4m — tell me about that").
   - Record it in the sender's own words. No rounding up, no "pieces like these
     achieved..." framing, no attributing results to a format or artefact.
   - Date every proof point (an "as of" month and year, or the campaign/deal
     date). Recent, dated campaign results are quoted as primary proof by the
     follow-up prompts; undated career name-drops age silently and keep getting
     quoted long after better proof exists. Order the list newest first.
   - If a claim cannot be verified or the sender hesitates, downgrade it to a
     credibility signal (what they do, who they work with, how long) or drop it.
   - Mark anything aspirational and EXCLUDE it. "First client doubled their
     order" is proof. "We expect to..." is not.

4. **Any hard constraints** the pipeline must respect: geography (e.g. UK-only),
   sectors to avoid, tone requirements, whether the sender writes their own
   companion cards (founder-led sends often do).

5. **Voice notes** (optional): sign-off preferences, phrases they use, phrases
   they would never use.

## Then run the approval loop

Present the draft profile to the sender and require explicit approval:

- Read every proof point back verbatim. The sender must say yes to the exact
  wording, not the gist. Record "verbatim-approved by [name], [date]" against the
  block.
- Confirm the problems list: "if we can show a company visibly has one of these,
  is that a company you want a meeting with?" If no, the problem is wrong.
- Confirm the constraints.

Do not output a final profile until approval is explicit.

## Output

The sender block for runner/config.json, exactly this shape, followed by an
approval line:

```json
{
  "sender": {
    "sender_name": "",
    "company": "",
    "what_they_sell": "",
    "proof_points": "",
    "booking_link": "",
    "problems": [
      "",
      ""
    ],
    "constraints": "",
    "custom_card": false,
    "sender_email": "",
    "card_phone": ""
  }
}
```

"constraints" holds the hard constraints from step 4 as one plain-language line
("UK-only; avoid gambling sector"), or "" if none. "custom_card" is true when the
sender writes their own companion cards (the founder-led default): the pipeline
then skips generated card copy entirely. These live in the JSON, not just in the
approval note, because the pipeline reads them.

APPROVAL: verbatim-approved by [sender name] on [date]. Constraints: [list, or
"none"]. Custom companion cards: [yes/no].
