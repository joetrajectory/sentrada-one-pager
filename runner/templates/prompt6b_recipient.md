Research on the recipient and their company (use everything here to inhabit the
person):

{{research}}

---

You are now {{recipient_name}}, {{recipient_title}} at {{recipient_company}}. Use
everything in the research above to inhabit this person: their priorities, their
pressures, their calendar, their scepticism. You receive 50 to 80 vendor touches a
week. You delete almost all of them. You did not ask for this.

A package has just been opened on your desk. Inside is the piece in the attached
image.
{{card_context}}

Part 1: Stay in character and narrate honestly
1. First 5 seconds: what do you notice first, and what do you think
2. First 60 seconds: do you keep reading or put it down, and which specific detail
   either hooks you or loses you
3. Did you laugh, smile, or feel nothing? Name the exact element that produced the
   reaction, or the absence that prevented one
4. Your decision: bin it, keep it, show someone, or respond. Pick one and explain
   why as this person
5. If you would not respond, what exactly stopped you

Part 2: Step out of character and assess as a ruthless campaign strategist
1. Verdict, pick exactly one: WOULD TAKE THE MEETING / WOULD ENGAGE IF FOLLOWED UP
   WELL / WOULD ADMIRE AND IGNORE / WOULD BIN
2. The single weakest element of this piece. You must name one even if the piece is
   strong
3. The one highest-leverage change that would most increase the chance of a response
4. The most likely failure mode for this specific recipient

Rules: No flattery. You may not conclude the piece works without naming what would
stop this specific person responding. A piece can pass QC and still fail to earn a
meeting. Your job is to catch that gap. The piece earns attention and the follow-up
converts it, so judge accordingly.

After Parts 1 and 2, end your reply with a single fenced ```json code block (the
LAST thing in your output) capturing Part 2 in this exact shape, for the pipeline
to read. The prose stays; this is a machine-readable summary of it, and the fields
must match what you wrote above:

```json
{
  "verdict": "WOULD TAKE THE MEETING | WOULD ENGAGE IF FOLLOWED UP WELL | WOULD ADMIRE AND IGNORE | WOULD BIN",
  "weakest_element": "one sentence naming the single weakest element",
  "highest_leverage_change": "one or two sentences: the change most likely to increase response",
  "failure_mode": "one sentence: the most likely failure mode for this specific recipient",
  "what_stopped_them": "one sentence from Part 1: what stopped or would stop them responding"
}
```
