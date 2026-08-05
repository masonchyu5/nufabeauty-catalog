import { requireSession } from "./_lib/auth.js";
import { resolveCatalog } from "./_lib/catalogs.js";
import { getCsvContent } from "./_lib/github.js";

export default async function handler(req, res) {
  if (!requireSession(req, res)) return;
  const catalog = resolveCatalog(req.query?.catalog);
  if (!catalog) return res.status(400).json({ error: "Unknown catalog" });
  try {
    const csv = await getCsvContent(catalog);
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    // Both catalogs' repo files are named items.csv, so prefix the download —
    // two edited copies on one desktop must not be mistakable for each other.
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${catalog.key}-items.csv"`
    );
    res.status(200).send(csv);
  } catch (err) {
    res.status(err.status === 401 || err.status === 403 ? 502 : 500).json({
      error: `Could not fetch the current CSV from GitHub: ${err.message}`,
    });
  }
}
