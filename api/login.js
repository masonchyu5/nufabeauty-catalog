import { issueSessionCookie, timingSafeEqualStrings } from "./_lib/auth.js";
import { readJsonBody } from "./_lib/body.js";

// Best-effort limiter: state is per warm function instance, so a determined
// attacker across instances is only slowed, not stopped. Acceptable for a
// single shared password behind HTTPS.
const failures = new Map();
const WINDOW_MS = 15 * 60 * 1000;
const MAX_FAILURES = 10;

function clientIp(req) {
  const fwd = String(req.headers["x-forwarded-for"] || "");
  return fwd.split(",")[0].trim() || "unknown";
}

function isLockedOut(ip) {
  const entry = failures.get(ip);
  if (!entry || Date.now() > entry.resetAt) return false;
  return entry.count >= MAX_FAILURES;
}

function recordFailure(ip) {
  const entry = failures.get(ip);
  if (!entry || Date.now() > entry.resetAt) {
    failures.set(ip, { count: 1, resetAt: Date.now() + WINDOW_MS });
  } else {
    entry.count++;
  }
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST only" });
  }
  const password = process.env.ADMIN_PASSWORD;
  if (!password) {
    return res.status(500).json({ error: "ADMIN_PASSWORD is not configured" });
  }
  const ip = clientIp(req);
  if (isLockedOut(ip)) {
    return res.status(429).json({ error: "Too many failed attempts. Try again in 15 minutes." });
  }

  let supplied = "";
  try {
    const body = await readJsonBody(req);
    if (typeof body?.password === "string") supplied = body.password;
  } catch {
    return res.status(400).json({ error: "Invalid request body" });
  }

  if (!supplied || !timingSafeEqualStrings(supplied, password)) {
    recordFailure(ip);
    await new Promise((resolve) => setTimeout(resolve, 300));
    return res.status(401).json({ error: "Wrong password" });
  }

  failures.delete(ip);
  issueSessionCookie(res);
  res.status(200).json({ ok: true });
}
