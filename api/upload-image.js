import { requireSession } from "./_lib/auth.js";
import { readRawBody } from "./_lib/body.js";
import { createBlob, IMAGES_DIR } from "./_lib/github.js";
import { MAX_IMAGE_BYTES, sanitizeImageFilename, sniffImageKind } from "./_lib/images.js";

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST only" });
  }
  if (!requireSession(req, res)) return;

  const clean = sanitizeImageFilename(req.query?.filename);
  if (!clean) {
    return res.status(400).json({
      error:
        "Bad filename. Use letters, digits, dot, dash, underscore only, ending in .jpg, .jpeg, .png, or .webp.",
    });
  }

  let data;
  try {
    data = await readRawBody(req, MAX_IMAGE_BYTES);
  } catch (err) {
    return res.status(err.status || 500).json({
      error: err.status === 413 ? "Image is larger than 4MB." : err.message,
    });
  }
  if (!data.length) {
    return res.status(400).json({ error: "Empty file." });
  }

  const kind = sniffImageKind(data);
  if (kind !== clean.kind) {
    return res.status(400).json({
      error: `File content does not match the ${clean.base.split(".").pop()} extension.`,
    });
  }

  try {
    const sha = await createBlob(data.toString("base64"));
    res.status(200).json({ path: `${IMAGES_DIR}/${clean.base}`, filename: clean.base, sha });
  } catch (err) {
    res.status(502).json({ error: `GitHub upload failed: ${err.message}` });
  }
}
