import { requireSession } from "./_lib/auth.js";
import { readJsonBody } from "./_lib/body.js";
import { validateCsv, normalizedNameFor } from "./_lib/csv.js";
import {
  commitToMain,
  listRepoImageNames,
  listNormalizedNames,
  IMAGES_DIR,
  NORMALIZED_DIR,
} from "./_lib/github.js";
import { sanitizeImageFilename } from "./_lib/images.js";

const MAX_IMAGES_PER_PUBLISH = 1000;
const MAX_DELETES_PER_PUBLISH = 1000;

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
  const rawDeletes = Array.isArray(body?.deleteImages) ? body.deleteImages : [];
  if (!csv && !rawImages.length && !rawDeletes.length) {
    return res.status(400).json({ error: "Nothing to publish: no CSV, no images, no deletions." });
  }
  if (rawImages.length > MAX_IMAGES_PER_PUBLISH) {
    return res.status(400).json({
      error: `Too many images in one publish (max ${MAX_IMAGES_PER_PUBLISH}). Split the batch.`,
    });
  }
  if (rawDeletes.length > MAX_DELETES_PER_PUBLISH) {
    return res.status(400).json({
      error: `Too many deletions in one publish (max ${MAX_DELETES_PER_PUBLISH}). Split the batch.`,
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

  let repoImages = null;
  if (rawDeletes.length || csv) {
    try {
      repoImages = new Set(await listRepoImageNames());
    } catch (err) {
      return res.status(502).json({ error: `Could not list the catalog's photos: ${err.message}` });
    }
  }

  const deletions = [];
  const deletedNames = new Set();
  if (rawDeletes.length) {
    const wanted = new Set();
    for (const raw of rawDeletes) {
      const clean = sanitizeImageFilename(raw);
      if (!clean) {
        return res.status(400).json({ error: `Invalid filename to delete: ${JSON.stringify(raw)}` });
      }
      if (seen.has(clean.base)) {
        return res.status(400).json({
          error: `${clean.base} is being uploaded and deleted in the same publish. Drop it from one of the two.`,
        });
      }
      wanted.add(clean.base);
    }

    const absent = [...wanted].filter((name) => !repoImages.has(name));
    if (absent.length) {
      return res.status(409).json({
        error:
          `These photos are no longer in the catalog: ${absent.slice(0, 5).join(", ")}` +
          `${absent.length > 5 ? ` (+${absent.length - 5} more)` : ""}. ` +
          "Someone may have deleted them already — reload the page and try again.",
      });
    }
    for (const name of wanted) {
      deletions.push(`${IMAGES_DIR}/${name}`);
      deletedNames.add(name);
    }

    // Several masters can share one display copy (CH1.jpg and CH1.webp both
    // derive ch1.jpg), so only remove it once nothing left still maps to it.
    const stillUsed = new Set();
    for (const name of repoImages) {
      if (!wanted.has(name)) stillUsed.add(normalizedNameFor(name));
    }
    for (const name of seen) stillUsed.add(normalizedNameFor(name));

    let normalized;
    try {
      normalized = new Set(await listNormalizedNames());
    } catch {
      normalized = new Set(); // no derivative listing: skip the cleanup, never fail the publish
    }
    const derived = new Set();
    for (const name of wanted) {
      const norm = normalizedNameFor(name);
      if (!stillUsed.has(norm) && normalized.has(norm)) derived.add(norm);
    }
    for (const norm of derived) deletions.push(`${NORMALIZED_DIR}/${norm}`);
  }

  let report = null;
  if (csv) {
    // A photo deleted in this same publish must not count as available.
    const afterDelete = new Set([...repoImages].filter((name) => !deletedNames.has(name)));
    try {
      report = validateCsv(csv, { repoImages: afterDelete, batchImages: seen });
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
  if (deletedNames.size) parts.push(`deleted ${deletedNames.size} image(s)`);
  const message = `Admin publish: ${parts.join(" + ")}`;

  try {
    const commit = await commitToMain({ csv, images, deletions, message });
    res.status(200).json({ commit, report, deleted: deletedNames.size });
  } catch (err) {
    res.status(err.status === 409 ? 409 : 502).json({ error: err.message });
  }
}
