# Prompt 7b (classify): inbound reply classification

You classify an inbound reply to a one-of-one physical outreach piece. The piece was researched, printed and delivered to a named senior executive's desk. The reply below arrived on {{channel}}. Classify it; do not draft a response.

## Piece context

Recipient: {{recipient}}
Format: {{format}}

Brief (the tension the piece named):
{{brief}}

## The reply (or thread, latest message last)

{{reply_text}}

## Classify the LATEST message only

**Reply language**, exactly one:
- "gift": praise for the object and its craft ("very clever", "eye-catching", "love it", "going up on our walls", "appreciate the effort"). The piece landed as an object.
- "problem": engagement with the tension the piece named ("how did you know", the recipient naming their own tension back, questions about the problem or the offering). The piece landed as a mirror.
- "mixed": both, clearly present.
- "none": neither (administrative, flat, or automated).

**Intent**, exactly one:
- "interested": wants to talk, asks what the sender offers, moves towards a meeting.
- "question": asks something specific that needs answering before anything else.
- "objection": engages but raises a condition, concern or pushback (including "the way in is X" style deflections that state a condition the sender could meet).
- "polite_deflection": acknowledges warmly but declines or goes passive, with no condition to meet.
- "rejection": a flat no ("No thank you"). Distinct from polite deflection: there is nothing to work with.
- "referral": routes to, or arrives from, a different person than the piece's recipient. Includes a colleague/EA replying on the recipient's behalf.
- "auto_reply": out of office or any automated response. Extract the return date if stated.

Rules:
- Judge language and intent from the latest message. Use earlier messages in the thread only as context.
- A reply can praise the object AND deflect; language and intent are independent axes.
- For "referral": extract the new person's name, role if stated, and whether they are the new decision path or just a router.
- For "auto_reply": this is not a real reply. Extract return_date if present.
- Quote the exact phrases that drove your call in "evidence".

## Output

A single JSON block, nothing after it:

```json
{
  "reply_language": "gift|problem|mixed|none",
  "intent": "interested|question|objection|polite_deflection|rejection|referral|auto_reply",
  "evidence": ["exact phrase", "exact phrase"],
  "referral_name": null,
  "referral_role": null,
  "return_date": null,
  "condition_to_meet": null,
  "notes": "one sentence"
}
```

"condition_to_meet": for objections only, the specific thing the recipient said would change their answer (e.g. "a portfolio company recommendation"). Otherwise null.
