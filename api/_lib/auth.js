import crypto from "node:crypto";

const COOKIE_NAME = "nufa_session";
const SESSION_HOURS = 8;

function sessionSecret() {
  const secret = process.env.SESSION_SECRET;
  if (!secret) throw new Error("SESSION_SECRET is not configured");
  return secret;
}

function sign(payload) {
  return crypto.createHmac("sha256", sessionSecret()).update(payload).digest("hex");
}

export function issueSessionCookie(res) {
  const expires = Date.now() + SESSION_HOURS * 3600 * 1000;
  const value = `${expires}.${sign(String(expires))}`;
  res.setHeader(
    "Set-Cookie",
    `${COOKIE_NAME}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_HOURS * 3600}`
  );
}

export function clearSessionCookie(res) {
  res.setHeader(
    "Set-Cookie",
    `${COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`
  );
}

export function hasValidSession(req) {
  const cookies = req.headers.cookie || "";
  for (const part of cookies.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() !== COOKIE_NAME) continue;
    const value = part.slice(eq + 1).trim();
    const dot = value.indexOf(".");
    if (dot < 0) return false;
    const expires = value.slice(0, dot);
    const signature = value.slice(dot + 1);
    if (!/^\d+$/.test(expires) || Number(expires) < Date.now()) return false;
    const expected = sign(expires);
    const a = Buffer.from(signature, "utf8");
    const b = Buffer.from(expected, "utf8");
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  }
  return false;
}

export function requireSession(req, res) {
  if (hasValidSession(req)) return true;
  res.status(401).json({ error: "Not logged in" });
  return false;
}

// Hash both sides to equal length so comparison time is independent of input.
export function timingSafeEqualStrings(a, b) {
  const ha = crypto.createHash("sha256").update(a, "utf8").digest();
  const hb = crypto.createHash("sha256").update(b, "utf8").digest();
  return crypto.timingSafeEqual(ha, hb);
}
