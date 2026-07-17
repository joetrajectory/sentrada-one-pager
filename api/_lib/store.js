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

// Hard safety net on top of the delete-on-delivery promise: every store key
// self-deletes after SAFETY_TTL_DAYS even if `delivered` is never run, so no
// address (or stale token) can outlive the flow by more than 90 days.
const SAFETY_TTL_DAYS = 90;
const SAFETY_TTL_SECONDS = SAFETY_TTL_DAYS * 86400;

async function getRecord(token) {
  const raw = await redis("GET", `tok:${token}`);
  return raw ? JSON.parse(raw) : null;
}

// Write the record AND its expiry in one command (SET ... EX). A plain SET
// clears any existing TTL on real Redis, so writing the value and arming the
// backstop as two calls left a window where a transient failure between them
// stranded an address key with no expiry. One atomic command closes it: the
// address can never be TTL-less, and each write (register, submit) restarts
// the 90-day clock.
async function setRecord(token, record, ttlSeconds = SAFETY_TTL_SECONDS) {
  await redis("SET", `tok:${token}`, JSON.stringify(record), "EX", ttlSeconds);
}

// The piece -> token pointer, written with the same atomic TTL.
async function setPointer(pieceId, token, ttlSeconds = SAFETY_TTL_SECONDS) {
  await redis("SET", `piece:${pieceId}`, token, "EX", ttlSeconds);
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

// The caller's real IP. On Vercel `x-real-ip` is set by the platform and
// cannot be spoofed by the client; the leftmost `x-forwarded-for` entry CAN
// be, so it is only a local-dev fallback and we take its LAST hop (closest to
// the edge) rather than the client-controlled first one.
function clientIp(req) {
  const real = String(req.headers["x-real-ip"] || "").trim();
  if (real) return real;
  const fwd = String(req.headers["x-forwarded-for"] || "").split(",").map((s) => s.trim()).filter(Boolean);
  return fwd.length ? fwd[fwd.length - 1] : "unknown";
}

// Small fixed-window rate limit per IP per route. Fail-open: if the store call
// itself fails the request proceeds and fails on the store anyway. The EXPIRE
// is re-issued on every hit, so a single failed EXPIRE can never leave the
// counter key (which names an IP) immortal.
async function withinRateLimit(req, route, limit) {
  try {
    const key = `rl:${route}:${clientIp(req)}:${Math.floor(Date.now() / 60000)}`;
    const count = await redis("INCR", key);
    await redis("EXPIRE", key, 120);
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
  // Strip ASCII control chars (neutralises terminal-escape and header
  // injection into the runner output and the notification email) AND Unicode
  // bidi / zero-width characters, so a submitted address cannot render
  // deceptively in the operator's terminal or the Birch CSV.
  return String(value == null ? "" : value)
    .replace(/[\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, " ")
    .trim()
    .slice(0, max);
}

module.exports = { redis, getRecord, setRecord, setPointer, TOKEN_RE, isExpired,
  publicState, noindex, clean, clientIp, withinRateLimit,
  SAFETY_TTL_DAYS, SAFETY_TTL_SECONDS };
