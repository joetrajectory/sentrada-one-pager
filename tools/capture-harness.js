// Local stand-in for the deployed address-capture stack, used by the runner's
// `capture-probe` command (and by hand for development). It runs the REAL
// serverless functions from ../api against a mock Upstash Redis and a mock
// Resend, so the code under test is byte-for-byte what Vercel deploys; only
// the infrastructure is faked. Never deployed: tools/ is outside api/, so
// Vercel does not route it.
//
//   node tools/capture-harness.js
//
// Ports (override via env): APP_PORT 8788, REDIS_PORT 8790, RESEND_PORT 8791.
// The runner secret is CAPTURE_HARNESS_SECRET (default "probe-secret").
// The Resend mock records every email and serves them back at GET /_emails,
// so a probe can assert the notification never contains an address.

"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");

const REPO = path.dirname(__dirname);
const APP_PORT = Number(process.env.APP_PORT || 8788);
const REDIS_PORT = Number(process.env.REDIS_PORT || 8790);
const RESEND_PORT = Number(process.env.RESEND_PORT || 8791);
const SECRET = process.env.CAPTURE_HARNESS_SECRET || "probe-secret";

// The api modules read their configuration from the environment at call time,
// so wire the mocks in before requiring them.
process.env.KV_REST_API_URL = `http://127.0.0.1:${REDIS_PORT}`;
process.env.KV_REST_API_TOKEN = "harness-kv-token";
process.env.RUNNER_SECRET = SECRET;
process.env.RESEND_API_KEY = "harness-resend-key";
process.env.RESEND_API_URL = `http://127.0.0.1:${RESEND_PORT}/emails`;
process.env.NOTIFY_EMAIL = "probe@example.test";

// ---- mock Upstash Redis ------------------------------------------------------
// Supports the command subset the store uses: SET GET DEL KEYS SCAN MGET INCR
// EXPIRE. Expiry is tracked so the probe can assert the TTL safety net is set,
// but keys are only evicted lazily on read (fine for a probe's lifetime).
const kv = new Map();     // key -> value
const ttl = new Map();    // key -> epoch ms deadline

function alive(key) {
  const deadline = ttl.get(key);
  if (deadline !== undefined && Date.now() > deadline) {
    kv.delete(key);
    ttl.delete(key);
  }
  return kv.has(key);
}

function command(parts) {
  const [cmd, ...rest] = parts;
  switch (String(cmd).toUpperCase()) {
    case "SET": kv.set(rest[0], String(rest[1])); ttl.delete(rest[0]); return "OK";
    case "GET": return alive(rest[0]) ? kv.get(rest[0]) : null;
    case "DEL": ttl.delete(rest[0]); return kv.delete(rest[0]) ? 1 : 0;
    case "KEYS": {
      const prefix = String(rest[0]).replace(/\*$/, "");
      return [...kv.keys()].filter((k) => k.startsWith(prefix) && alive(k));
    }
    case "SCAN": {
      // Single-pass cursor: everything in one batch, cursor "0" ends the loop.
      const match = rest[rest.indexOf("MATCH") + 1] || "*";
      const prefix = String(match).replace(/\*$/, "");
      return ["0", [...kv.keys()].filter((k) => k.startsWith(prefix) && alive(k))];
    }
    case "MGET": return rest.map((k) => (alive(k) ? kv.get(k) : null));
    case "INCR": {
      const next = (alive(rest[0]) ? parseInt(kv.get(rest[0]), 10) : 0) + 1;
      kv.set(rest[0], String(next));
      return next;
    }
    case "EXPIRE": {
      if (!alive(rest[0])) return 0;
      ttl.set(rest[0], Date.now() + Number(rest[1]) * 1000);
      return 1;
    }
    default: throw new Error("mock redis: unsupported command " + cmd);
  }
}

const redisServer = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    res.setHeader("Content-Type", "application/json");
    // The probe can inspect the raw store at GET /_dump (never in production;
    // this whole server exists only on localhost during a probe).
    if (req.method === "GET" && req.url === "/_dump") {
      return res.end(JSON.stringify({
        keys: [...kv.keys()].filter(alive),
        ttl_keys: [...ttl.keys()],
      }));
    }
    try {
      res.end(JSON.stringify({ result: command(JSON.parse(body)) }));
    } catch (e) {
      res.statusCode = 400;
      res.end(JSON.stringify({ error: e.message }));
    }
  });
});

// ---- mock Resend -------------------------------------------------------------
const emails = [];
const resendServer = http.createServer((req, res) => {
  res.setHeader("Content-Type", "application/json");
  if (req.method === "GET" && req.url === "/_emails") {
    return res.end(JSON.stringify(emails));
  }
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    emails.push(JSON.parse(body));
    res.end(JSON.stringify({ id: "mock-email-" + emails.length }));
  });
});

// ---- app server: static files + the real /api functions ---------------------
const MIME = { ".html": "text/html; charset=utf-8", ".css": "text/css",
  ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png",
  ".webp": "image/webp", ".jpg": "image/jpeg" };

const apiHandlers = {
  "/api/token": require(path.join(REPO, "api/token.js")),
  "/api/submit": require(path.join(REPO, "api/submit.js")),
  "/api/runner": require(path.join(REPO, "api/runner.js")),
};

function vercelShim(req, res, handler) {
  const u = new URL(req.url, "http://localhost");
  req.query = Object.fromEntries(u.searchParams);
  let raw = "";
  req.on("data", (c) => (raw += c));
  req.on("end", () => {
    try { req.body = raw ? JSON.parse(raw) : {}; } catch { req.body = {}; }
    res.status = (code) => { res.statusCode = code; return res; };
    res.json = (obj) => { res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify(obj)); return res; };
    Promise.resolve(handler(req, res)).catch((e) => {
      res.statusCode = 500;
      res.end(JSON.stringify({ error: e.message }));
    });
  });
}

const appServer = http.createServer((req, res) => {
  const u = new URL(req.url, "http://localhost");
  const apiPath = u.pathname.replace(/\/$/, "");
  if (apiHandlers[apiPath]) return vercelShim(req, res, apiHandlers[apiPath]);

  // Mirror the vercel.json rewrite and noindex headers for /for/<token>.
  let file = u.pathname;
  if (/^\/for\/[^/]+$/.test(file) || file === "/for") file = "/for.html";
  if (file === "/") file = "/index.html";
  if (file.startsWith("/for")) {
    res.setHeader("X-Robots-Tag", "noindex, nofollow");
    res.setHeader("Cache-Control", "no-store");
  }
  const full = path.join(REPO, file);
  if (!full.startsWith(REPO) || !fs.existsSync(full) || fs.statSync(full).isDirectory()) {
    res.writeHead(404);
    return res.end("not found");
  }
  res.setHeader("Content-Type", MIME[path.extname(full)] || "application/octet-stream");
  fs.createReadStream(full).pipe(res);
});

redisServer.listen(REDIS_PORT, "127.0.0.1");
resendServer.listen(RESEND_PORT, "127.0.0.1");
appServer.listen(APP_PORT, "127.0.0.1", () => {
  // The probe waits for this exact line before it starts.
  console.log(`capture-harness ready on http://127.0.0.1:${APP_PORT}`);
});
