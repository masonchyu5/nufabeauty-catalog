import { requireSession } from "./_lib/auth.js";
import { readJsonBody } from "./_lib/body.js";
import { validateCsv } from "./_lib/csv.js";
import { listRepoImageNames } from "./_lib/github.js";

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST only" });
  }
  if (!requireSession(req, res)) return;

  let body;
  try {
    body = await readJsonBody(req);
  } catch {
    return res.status(400).json({ error: "Invalid request body" });
  }
  if (typeof body?.csv !== "string" || !body.csv.length) {
    return res.status(400).json({ error: "Missing csv text" });
  }
  const batchImages = new Set(
    Array.isArray(body.batchImages) ? body.batchImages.filter((n) => typeof n === "string") : []
  );

  try {
    const repoImages = new Set(await listRepoImageNames());
    const report = validateCsv(body.csv, { repoImages, batchImages });
    res.status(200).json(report);
  } catch (err) {
    res.status(500).json({ error: `Validation failed: ${err.message}` });
  }
}
