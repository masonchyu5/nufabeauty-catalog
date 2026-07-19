import { requireSession } from "./_lib/auth.js";
import { readJsonBody } from "./_lib/body.js";
import { validateCsv } from "./_lib/csv.js";
import { commitToMain, listRepoImageNames, IMAGES_DIR } from "./_lib/github.js";
import { sanitizeImageFilename } from "./_lib/images.js";

const MAX_IMAGES_PER_PUBLISH = 1000;

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

  const csv = typeof body?.csv === "string" && body.csv.length ? body.csv : null;
  const rawImages = Array.isArray(body?.images) ? body.images : [];
  if (!csv && !rawImages.length) {
    return res.status(400).json({ error: "Nothing to publish: no CSV and no images." });
  }
  if (rawImages.length > MAX_IMAGES_PER_PUBLISH) {
    return res.status(400).json({
      error: `Too many images in one publish (max ${MAX_IMAGES_PER_PUBLISH}). Split the batch.`,
    });
  }

  const images = [];
  const seen = new Set();
  for (const entry of rawImages) {
    const clean = sanitizeImageFilename(entry?.filename);
    const sha = typeof entry?.sha === "string" ? entry.sha : "";
    if (!clean || !/^[0-9a-f]{40}$/.test(sha)) {
      return res.status(400).json({ error: `Invalid image entry: ${JSON.stringify(entry?.filename)}` });
    }
    if (seen.has(clean.base)) {
      return res.status(400).json({ error: `Duplicate image filename: ${clean.base}` });
    }
    seen.add(clean.base);
    images.push({ path: `${IMAGES_DIR}/${clean.base}`, sha });
  }

  let report = null;
  if (csv) {
    try {
      const repoImages = new Set(await listRepoImageNames());
      report = validateCsv(csv, { repoImages, batchImages: seen });
    } catch (err) {
      return res.status(500).json({ error: `Validation failed: ${err.message}` });
    }
    if (!report.ok) {
      return res.status(400).json({ error: "CSV failed validation", report });
    }
  }

  const parts = [];
  if (csv) parts.push("CSV");
  if (images.length) parts.push(`${images.length} image(s)`);
  const message = `Admin publish: ${parts.join(" + ")}`;

  try {
    const commit = await commitToMain({ csv, images, message });
    res.status(200).json({ commit, report });
  } catch (err) {
    res.status(err.status === 409 ? 409 : 502).json({ error: err.message });
  }
}
