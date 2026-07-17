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
    case "SET": {
      // SET key val [EX seconds]. A bare SET clears any TTL (real Redis
      // semantics); SET ... EX arms it atomically in the same command.
      kv.set(rest[0], String(rest[1]));
      const exAt = rest.findIndex((a) => String(a).toUpperCase() === "EX");
      if (exAt !== -1) ttl.set(rest[0], Date.now() + Number(rest[exAt + 1]) * 1000);
      else ttl.delete(rest[0]);
      return "OK";
    }
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

// Load the REAL vercel.json so the harness reproduces cleanUrls / rewrite /
// trailingSlash behaviour instead of hardcoding a forgiving version. This is
// what makes the probe able to catch a rewrite-destination regression (a
// destination that still carries .html would 308 away and drop the token).
const vercel = JSON.parse(fs.readFileSync(path.join(REPO, "vercel.json"), "utf8"));
const CLEAN_URLS = vercel.cleanUrls === true;
const TRAILING_SLASH = vercel.trailingSlash === true;
const REWRITES = vercel.rewrites || [];

function sourceToRegex(src) {
  // Vercel path-to-regexp subset: :param matches one path segment.
  return new RegExp("^" + src.replace(/:[A-Za-z0-9_]+/g, "([^/]+)") + "$");
}

function vercelShim(req, res, handler) {
  const u = new URL(req.url, "http://localhost");
  req.query = Object.fromEntries(u.searchParams);
  // Vercel always sets x-real-ip to the true client IP; it is not client
  // spoofable. Mimic that so the rate limiter's IP source is exercised as it
  // is in production (a spoofed x-forwarded-for must NOT create a new bucket).
  if (!req.headers["x-real-ip"]) {
    req.headers["x-real-ip"] = (req.socket && req.socket.remoteAddress) || "127.0.0.1";
  }
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

function redirect(res, location) {
  res.statusCode = 308;
  res.setHeader("Location", location);
  res.end();
}

const appServer = http.createServer((req, res) => {
  const u = new URL(req.url, "http://localhost");
  const apiPath = u.pathname.replace(/\/$/, "");
  if (apiHandlers[apiPath]) return vercelShim(req, res, apiHandlers[apiPath]);

  let pathname = u.pathname;

  // trailingSlash: false — strip a trailing slash (never the root) with a 308.
  if (!TRAILING_SLASH && pathname.length > 1 && pathname.endsWith("/")) {
    return redirect(res, pathname.slice(0, -1) + u.search);
  }
  // cleanUrls: a request that carries .html 308-redirects to the clean path.
  if (CLEAN_URLS && pathname.endsWith(".html")) {
    return redirect(res, pathname.slice(0, -5) + u.search);
  }

  // Apply the first matching rewrite (internal; the browser URL is unchanged).
  let served = pathname;
  for (const rw of REWRITES) {
    if (sourceToRegex(rw.source).test(pathname)) { served = rw.destination; break; }
  }
  // A rewrite DESTINATION that still ends in .html is itself subject to the
  // cleanUrls redirect — and the redirect goes to the destination path, losing
  // the original token. This is exactly the production failure the mock used to
  // hide; now it 308s here too, so the probe catches it.
  if (CLEAN_URLS && served.endsWith(".html")) {
    return redirect(res, served.slice(0, -5));
  }

  // Resolve the served path to a file on disk (cleanUrls: /for -> /for.html).
  let file = served;
  if (file === "/") file = "/index.html";
  else if (CLEAN_URLS && !path.extname(file) && fs.existsSync(path.join(REPO, file + ".html"))) {
    file = file + ".html";
  }

  // noindex/no-store for the token page, matching the vercel.json header rules.
  if (pathname === "/for" || pathname.startsWith("/for/")) {
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
