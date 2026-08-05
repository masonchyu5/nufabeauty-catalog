import { requireSession } from "./_lib/auth.js";
import { resolveCatalog } from "./_lib/catalogs.js";
import { listRepoImageNames } from "./_lib/github.js";

export default async function handler(req, res) {
  if (!requireSession(req, res)) return;
  const catalog = resolveCatalog(req.query?.catalog);
  if (!catalog) return res.status(400).json({ error: "Unknown catalog" });
  try {
    const images = await listRepoImageNames(catalog);
    res.status(200).json({ images });
  } catch (err) {
    res.status(500).json({ error: `Could not list repo images: ${err.message}` });
  }
}
