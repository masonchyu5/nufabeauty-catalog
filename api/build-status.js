import { requireSession } from "./_lib/auth.js";
import { workflowRunForCommit } from "./_lib/github.js";

export default async function handler(req, res) {
  if (!requireSession(req, res)) return;

  const sha = String(req.query?.sha || "");
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    return res.status(400).json({ error: "Missing or invalid sha" });
  }

  try {
    const run = await workflowRunForCommit(sha);
    if (!run) {
      return res.status(200).json({ status: "pending" });
    }
    res.status(200).json({
      status: run.status,
      conclusion: run.conclusion,
      url: run.html_url,
    });
  } catch (err) {
    // A contents-only PAT cannot read the Actions API; report "unknown" so the
    // UI can link to the Actions page instead of failing.
    if (err.status === 403 || err.status === 404) {
      return res.status(200).json({ status: "unknown" });
    }
    res.status(500).json({ error: `Could not check build status: ${err.message}` });
  }
}
