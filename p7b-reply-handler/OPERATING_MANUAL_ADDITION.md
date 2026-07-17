# Paste into the Sentrada Operating Manual (Notion), at the end

This session could not write to Notion (permission wall on the connector). Paste the section below into the Operating Manual page verbatim; it follows the manual's logged-section convention.

---

## Verify, don't assume: the Vitrue lesson (logged 17 July 2026)

From the P7b reply-handler validation, replaying every campaign one reply through the automated draft-and-gate flow. One reply set a condition: a recommendation from the recipient's own portfolio. The human reply offered Vitrue Health without establishing the portfolio connection. The automated gate, using the same facts, refused to imply the connection because no source on file stated it, and flagged it as the one input needed from the sender. Later confirmed: MMC co-led Vitrue Health's seed round in early 2024 and Simon Menashy was the partner quoted on the announcement. So the human reply met the condition, and the gate was also right to refuse the claim, because it was not in the research. Both things are true, and that is the design working: a claim that happens to be true but cannot be verified from the sources on file is still refused, and the remedy is always to enrich the sender profile or the research, never to loosen the gate. A fact-gate that accepts plausible claims because they are probably true is no gate at all.

Two operational implications:

- Sender profiles should carry a relationship block per campaign (past placements, in-portfolio work, warm contacts), because the strongest replies in campaign one won on privately-held facts that exist in no research doc. The reply handler asks for these via a SENDER INPUT NEEDED placeholder rather than inventing them.
- Reply handling is now automated as P7b in the runner (classify reply language and intent, draft by branch, gate with 4b logic). Reply language is recorded per piece automatically (first-reply-date, reply-language), feeding the leading-indicator tracking this manual already calls for.
