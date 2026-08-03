import { requireSession } from "./_lib/auth.js";
import { getCsvContent, CSV_PATH } from "./_lib/github.js";

export default async function handler(req, res) {
  if (!requireSession(req, res)) return;
  try {
    const csv = await getCsvContent();
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    // Basename only: CSV_PATH is a repo path, and browsers reject or mangle a
    // Content-Disposition filename containing directory separators.
    const downloadName = CSV_PATH.split("/").pop();
    res.setHeader("Content-Disposition", `attachment; filename="${downloadName}"`);
    res.status(200).send(csv);
  } catch (err) {
    res.status(err.status === 401 || err.status === 403 ? 502 : 500).json({
      error: `Could not fetch the current CSV from GitHub: ${err.message}`,
    });
  }
}
