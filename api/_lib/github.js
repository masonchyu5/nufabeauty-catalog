const API = process.env.GH_API_URL || "https://api.github.com";

export const IMAGES_DIR = "pages/chemical-upc-v3";
export const CSV_PATH = "items_chemical_master.csv";

function repoSlug() {
  const repo = process.env.GH_REPO;
  if (!repo) throw new Error("GH_REPO is not configured");
  return repo;
}

async function gh(path, options = {}) {
  const token = process.env.GH_TOKEN;
  if (!token) throw new Error("GH_TOKEN is not configured");
  const res = await fetch(`${API}/repos/${repoSlug()}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(
      `GitHub ${options.method || "GET"} ${path}: ${res.status} ${body.message || ""}`.trim()
    );
    err.status = res.status;
    throw err;
  }
  return body;
}

export async function createBlob(base64Content) {
  const body = await gh("/git/blobs", {
    method: "POST",
    body: JSON.stringify({ content: base64Content, encoding: "base64" }),
  });
  return body.sha;
}

export async function getMainHead() {
  const ref = await gh("/git/ref/heads/main");
  return ref.object.sha;
}

async function getCommitTreeSha(commitSha) {
  const commit = await gh(`/git/commits/${commitSha}`);
  return commit.tree.sha;
}

export async function listRepoImageNames() {
  let treeSha = await getCommitTreeSha(await getMainHead());
  for (const segment of IMAGES_DIR.split("/")) {
    const tree = await gh(`/git/trees/${treeSha}`);
    const entry = (tree.tree || []).find((e) => e.path === segment && e.type === "tree");
    if (!entry) return [];
    treeSha = entry.sha;
  }
  const tree = await gh(`/git/trees/${treeSha}`);
  return (tree.tree || []).filter((e) => e.type === "blob").map((e) => e.path);
}

export async function getCsvContent() {
  const body = await gh(`/contents/${encodeURIComponent(CSV_PATH)}?ref=main`);
  return Buffer.from(body.content, "base64").toString("utf8");
}

export async function commitToMain({ csv, images, message }) {
  const parent = await getMainHead();
  const baseTree = await getCommitTreeSha(parent);

  const entries = images.map(({ path, sha }) => ({
    path,
    mode: "100644",
    type: "blob",
    sha,
  }));
  if (csv != null) {
    entries.push({ path: CSV_PATH, mode: "100644", type: "blob", content: csv });
  }

  const tree = await gh("/git/trees", {
    method: "POST",
    body: JSON.stringify({ base_tree: baseTree, tree: entries }),
  });
  const commit = await gh("/git/commits", {
    method: "POST",
    body: JSON.stringify({ message, tree: tree.sha, parents: [parent] }),
  });

  try {
    await gh("/git/refs/heads/main", {
      method: "PATCH",
      body: JSON.stringify({ sha: commit.sha, force: false }),
    });
  } catch (err) {
    // A non-fast-forward here means main moved since we read it.
    if (err.status === 422 || err.status === 409) {
      const conflict = new Error(
        "Someone else just published — reload the page and try again."
      );
      conflict.status = 409;
      throw conflict;
    }
    throw err;
  }
  return commit.sha;
}

export async function workflowRunForCommit(headSha) {
  const body = await gh(
    `/actions/runs?head_sha=${encodeURIComponent(headSha)}&per_page=5`
  );
  const runs = body.workflow_runs || [];
  return runs.length ? runs[0] : null;
}
