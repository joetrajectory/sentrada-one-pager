// Shared helpers for the address-capture API. The _lib directory is not
// deployed as an endpoint (Vercel ignores api/ paths that start with "_").
//
// Storage is Upstash Redis over its REST protocol, called with fetch so the
// functions need no npm dependencies. Works with either the env names the
// Vercel marketplace integration injects (KV_REST_API_*) or Upstash's own
// (UPSTASH_REDIS_REST_*).
//
// Data model (tiny by design; dozens of records, not millions):
//   tok:<token>      JSON record: piece_id, first_name, created_at, expires_at,
//                    state ("active" | "submitted"), and after submission
//                    submitted_at, address_type, address {line1, line2, city,
//                    postcode, country}
//   piece:<piece_id> the piece's current token, so the runner can pull and
//                    purge by piece id without holding the token

"use strict";

function redisEnv() {
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) throw new Error("Redis store is not configured (KV_REST_API_URL / KV_REST_API_TOKEN)");
  return { url, token };
}

// One Redis command via the Upstash REST protocol: POST ["SET", key, value].
async function redis(...command) {
  const { url, token } = redisEnv();
  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  if (!resp.ok) throw new Error(`Redis error ${resp.status}: ${await resp.text()}`);
  const data = await resp.json();
  if (data.error) throw new Error(`Redis error: ${data.error}`);
  return data.result;
}

async function getRecord(token) {
  const raw = await redis("GET", `tok:${token}`);
  return raw ? JSON.parse(raw) : null;
}

async function setRecord(token, record) {
  await redis("SET", `tok:${token}`, JSON.stringify(record));
}

const TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;

function isExpired(record) {
  return !record.expires_at || Date.parse(record.expires_at) < Date.now();
}

// Every state the page can see for a token that is not currently active.
// Unknown and expired are deliberately the same answer: the page never
// confirms whether a guessed token ever existed.
function publicState(record) {
  if (!record || isExpired(record)) return { state: "expired" };
  if (record.state === "submitted") return { state: "submitted", first_name: record.first_name };
  return { state: "active", first_name: record.first_name };
}

// Hard safety net on top of the delete-on-delivery promise: every store key
// self-deletes after SAFETY_TTL_DAYS even if `delivered` is never run, so no
// address (or stale token) can outlive the flow by more than 90 days.
const SAFETY_TTL_DAYS = 90;

async function expireKeys(token, pieceId, days = SAFETY_TTL_DAYS) {
  const seconds = days * 86400;
  await redis("EXPIRE", `tok:${token}`, seconds);
  if (pieceId) await redis("EXPIRE", `piece:${pieceId}`, seconds);
}

// Small fixed-window rate limit per IP per route. Fail-open: if the store
// call itself fails the request proceeds and fails on the store anyway.
async function withinRateLimit(req, route, limit) {
  try {
    const ip = String(req.headers["x-forwarded-for"] || req.headers["x-real-ip"] || "")
      .split(",")[0].trim() || "unknown";
    const key = `rl:${route}:${ip}:${Math.floor(Date.now() / 60000)}`;
    const count = await redis("INCR", key);
    if (count === 1) await redis("EXPIRE", key, 60);
    return count <= limit;
  } catch {
    return true;
  }
}

function noindex(res) {
  res.setHeader("X-Robots-Tag", "noindex, nofollow");
  res.setHeader("Cache-Control", "no-store");
}

function clean(value, max = 200) {
  return String(value == null ? "" : value)
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .trim()
    .slice(0, max);
}

module.exports = { redis, getRecord, setRecord, TOKEN_RE, isExpired, publicState,
  noindex, clean, expireKeys, withinRateLimit, SAFETY_TTL_DAYS };
