// GET /api/token?t=<token>
// Tells the /for/<token> page who to greet and which state to show.
// Never returns the address, and answers "expired" for unknown tokens so the
// endpoint cannot be used to probe which tokens exist.

"use strict";

const { getRecord, TOKEN_RE, publicState, noindex } = require("./_lib/store.js");

module.exports = async (req, res) => {
  noindex(res);
  if (req.method !== "GET") return res.status(405).json({ error: "method not allowed" });
  const token = String(req.query.t || "");
  if (!TOKEN_RE.test(token)) return res.status(200).json({ state: "expired" });
  try {
    const record = await getRecord(token);
    return res.status(200).json(publicState(record));
  } catch (err) {
    console.error("token lookup failed:", err.message);
    return res.status(500).json({ error: "temporarily unavailable" });
  }
};
