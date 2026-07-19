import { requireSession } from "./_lib/auth.js";

export default function handler(req, res) {
  if (!requireSession(req, res)) return;
  res.status(200).json({ ok: true });
}
