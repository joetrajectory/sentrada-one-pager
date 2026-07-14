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
const { redis, getRecord, setRecord, TOKEN_RE, noindex, clean, expireKeys } = require("./_lib/store.js");

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
      if (previous) {
        // Never let a re-tease silently destroy an address that arrived on the
        // old token and has not been shipped yet. Pull it and mark the piece
        // delivered (which purges), then tease again.
        const prevRecord = await getRecord(previous);
        if (prevRecord && prevRecord.state === "submitted") {
          return res.status(409).json({
            error: "an address is on file for this piece; run `address` to pull "
              + "it and `delivered` to purge it before re-teasing" });
        }
        await redis("DEL", `tok:${previous}`);
      }
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
      await expireKeys(token, pieceId);
      return res.status(200).json({ ok: true, replaced: Boolean(previous) });
    }

    if (body.action === "list") {
      // SCAN + two MGETs rather than KEYS + a GET per piece: constant round
      // trips however many pieces are live.
      let cursor = "0";
      const keys = [];
      do {
        const [next, batch] = await redis("SCAN", cursor, "MATCH", "piece:*", "COUNT", 200);
        cursor = String(next);
        keys.push(...batch);
      } while (cursor !== "0");
      if (!keys.length) return res.status(200).json({ records: [] });
      const tokens = ((await redis("MGET", ...keys)) || []).filter(Boolean);
      if (!tokens.length) return res.status(200).json({ records: [] });
      const raws = ((await redis("MGET", ...tokens.map((t) => `tok:${t}`))) || []).filter(Boolean);
      const records = raws.map((raw) => {
        const { address, ...safe } = JSON.parse(raw);
        return safe;
      });
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
