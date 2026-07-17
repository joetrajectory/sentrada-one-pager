// POST /api/submit
// Body: { token, address_type: "office"|"other", line1, line2, city, postcode, country }
//
// Stores the address against the token record and notifies the sender that an
// address arrived. The notification NEVER contains the address, only the piece
// id: the address lives in the store until it is pulled/purged by the runner,
// and nothing escapes the delete-on-delivery promise.

"use strict";

const { getRecord, setRecord, TOKEN_RE, isExpired, noindex, clean,
  withinRateLimit } = require("./_lib/store.js");

// Same-day notification via Resend (https://resend.com). Optional: if the key
// is missing or the send fails, the submission still lands in the store and
// the runner's `capture` poll picks it up. The endpoint override exists so the
// flow can be tested against a local mock.
async function notify(pieceId) {
  const key = process.env.RESEND_API_KEY;
  if (!key) return "skipped (no RESEND_API_KEY)";
  const to = process.env.NOTIFY_EMAIL || "joe@sentrada.io";
  const from = process.env.NOTIFY_FROM || "Sentrada <onboarding@resend.dev>";
  const resp = await fetch(process.env.RESEND_API_URL || "https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      from,
      to: [to],
      subject: `Sentrada: address received for ${pieceId}`,
      text: `An address arrived for piece ${pieceId}.\n\n` +
        `It is held in the capture store, not in this email.\n` +
        `Pull it when you stage the shipment:\n\n` +
        `  python runner/sentrada_runner.py address --piece ${pieceId}\n\n` +
        `Mark the piece delivered once it is signed for and the address is deleted everywhere:\n\n` +
        `  python runner/sentrada_runner.py delivered --piece ${pieceId}\n`,
    }),
  });
  if (!resp.ok) throw new Error(`Resend ${resp.status}: ${await resp.text()}`);
  return "sent";
}

module.exports = async (req, res) => {
  noindex(res);
  if (req.method !== "POST") return res.status(405).json({ error: "method not allowed" });
  const body = req.body || {};
  const token = String(body.token || "");
  if (!TOKEN_RE.test(token)) return res.status(400).json({ error: "expired" });

  const addressType = body.address_type === "office" ? "office" : body.address_type === "other" ? "other" : "";
  const address = {
    line1: clean(body.line1),
    line2: clean(body.line2),
    city: clean(body.city, 100),
    postcode: clean(body.postcode, 30),
    country: clean(body.country, 100),
  };
  if (!addressType || !address.line1 || !address.city || !address.postcode || !address.country) {
    return res.status(400).json({ error: "missing fields" });
  }

  try {
    if (!(await withinRateLimit(req, "submit", 10))) {
      return res.status(429).json({ error: "slow down" });
    }
    const record = await getRecord(token);
    if (!record || isExpired(record)) return res.status(400).json({ error: "expired" });
    if (record.state === "submitted") return res.status(200).json({ ok: true });

    record.state = "submitted";
    record.submitted_at = new Date().toISOString();
    record.address_type = addressType;
    record.address = address;
    // setRecord writes the value and the 90-day TTL as one atomic command, so
    // the address can never be stored without an expiry and the self-delete
    // clock restarts from submission even if `delivered` is never run.
    await setRecord(token, record);

    let notified = "failed";
    try {
      notified = await notify(record.piece_id);
    } catch (err) {
      // Never fail the recipient's submission over the notification; the
      // runner's capture poll is the fallback path.
      console.error("notification failed:", err.message);
    }
    console.log(`submission stored for ${record.piece_id}, notification ${notified}`);
    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error("submit failed:", err.message);
    return res.status(500).json({ error: "temporarily unavailable" });
  }
};
