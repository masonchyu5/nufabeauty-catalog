import { requireSession } from "./_lib/auth.js";
import { listRepoImageNames } from "./_lib/github.js";

export default async function handler(req, res) {
  if (!requireSession(req, res)) return;
  try {
    const images = await listRepoImageNames();
    res.status(200).json({ images });
  } catch (err) {
    res.status(500).json({ error: `Could not list repo images: ${err.message}` });
  }
}
