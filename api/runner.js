// POST /api/runner — the chain runner's private side of address capture.
// Auth: Authorization: Bearer <RUNNER_SECRET>. Never called by the page.
//
// Actions (body: { action, ... }):
//   register  { token, piece_id, first_name, ttl_days? }
//             Store a new token for a piece (called by `tease`). Replaces and
//             invalidates any earlier token for the same piece.
//   list      {}
//             Every record WITHOUT addresses: piece_id, state, dates, type.
//             Powers the `capture` status poll.
//   pull      { piece_id }
//             The full record including the address, printed by `address`.
//   purge     { piece_id }
//             Delete the token record and address entirely (called by
//             `delivered`). This is the deletion the page promises.

"use strict";

const crypto = require("crypto");
const { redis, getRecord, setRecord, TOKEN_RE, noindex, clean } = require("./_lib/store.js");

function authorised(req) {
  const secret = process.env.RUNNER_SECRET || "";
  const header = String(req.headers.authorization || "");
  const provided = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!secret || !provided || provided.length !== secret.length) return false;
  return crypto.timingSafeEqual(Buffer.from(provided), Buffer.from(secret));
}

async function tokenForPiece(pieceId) {
  return pieceId ? await redis("GET", `piece:${pieceId}`) : null;
}

module.exports = async (req, res) => {
  noindex(res);
  if (req.method !== "POST") return res.status(405).json({ error: "method not allowed" });
  if (!authorised(req)) return res.status(401).json({ error: "unauthorised" });

  const body = req.body || {};
  const pieceId = clean(body.piece_id, 120);

  try {
    if (body.action === "register") {
      const token = String(body.token || "");
      const firstName = clean(body.first_name, 80);
      if (!TOKEN_RE.test(token) || !pieceId || !firstName) {
        return res.status(400).json({ error: "register needs token, piece_id, first_name" });
      }
      const previous = await tokenForPiece(pieceId);
      if (previous) await redis("DEL", `tok:${previous}`);
      const ttlDays = Math.min(Math.max(parseInt(body.ttl_days, 10) || 30, 1), 90);
      const now = new Date();
      await setRecord(token, {
        piece_id: pieceId,
        first_name: firstName,
        state: "active",
        created_at: now.toISOString(),
        expires_at: new Date(now.getTime() + ttlDays * 86400000).toISOString(),
      });
      await redis("SET", `piece:${pieceId}`, token);
      return res.status(200).json({ ok: true, replaced: Boolean(previous) });
    }

    if (body.action === "list") {
      const keys = (await redis("KEYS", "piece:*")) || [];
      const records = [];
      for (const key of keys) {
        const token = await redis("GET", key);
        const record = token ? await getRecord(token) : null;
        if (!record) continue;
        const { address, ...safe } = record;
        records.push(safe);
      }
      return res.status(200).json({ records });
    }

    if (body.action === "pull") {
      if (!pieceId) return res.status(400).json({ error: "pull needs piece_id" });
      const token = await tokenForPiece(pieceId);
      const record = token ? await getRecord(token) : null;
      if (!record) return res.status(404).json({ error: "no record for that piece" });
      return res.status(200).json({ record });
    }

    if (body.action === "purge") {
      if (!pieceId) return res.status(400).json({ error: "purge needs piece_id" });
      const token = await tokenForPiece(pieceId);
      let hadAddress = false;
      if (token) {
        const record = await getRecord(token);
        hadAddress = Boolean(record && record.address);
        await redis("DEL", `tok:${token}`);
      }
      await redis("DEL", `piece:${pieceId}`);
      return res.status(200).json({ ok: true, purged: Boolean(token), had_address: hadAddress });
    }

    return res.status(400).json({ error: "unknown action" });
  } catch (err) {
    console.error("runner action failed:", err.message);
    return res.status(500).json({ error: "temporarily unavailable" });
  }
};
