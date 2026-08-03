const API = process.env.GH_API_URL || "https://api.github.com";

export const IMAGES_DIR = "source/chemical/master-images";
// Display copies build_catalog.py derives from the masters. Deleting a master
// has to take its derivative too, or the repo keeps the bytes of every photo
// ever removed -- nothing in the build ever unlinks from this directory.
export const NORMALIZED_DIR = "images/chemical";
export const CSV_PATH = "source/chemical/items.csv";

const IMAGE_EXT = /\.(jpe?g|png|webp)$/i;

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

async function listDirBlobs(dir) {
  let treeSha = await getCommitTreeSha(await getMainHead());
  for (const segment of dir.split("/")) {
    const tree = await gh(`/git/trees/${treeSha}`);
    const entry = (tree.tree || []).find((e) => e.path === segment && e.type === "tree");
    if (!entry) return [];
    treeSha = entry.sha;
  }
  const tree = await gh(`/git/trees/${treeSha}`);
  return (tree.tree || []).filter((e) => e.type === "blob").map((e) => e.path);
}

// Only actual photos: the directory also holds a stray helper script, and
// listing it as a deletable "image" would be confusing.
export async function listRepoImageNames() {
  return (await listDirBlobs(IMAGES_DIR)).filter((name) => IMAGE_EXT.test(name));
}

export async function listNormalizedNames() {
  return listDirBlobs(NORMALIZED_DIR);
}

export async function getCsvContent() {
  const body = await gh(`/contents/${encodeURIComponent(CSV_PATH)}?ref=main`);
  return Buffer.from(body.content, "base64").toString("utf8");
}

export async function commitToMain({ csv, images, deletions = [], message }) {
  const parent = await getMainHead();
  const baseTree = await getCommitTreeSha(parent);

  const entries = images.map(({ path, sha }) => ({
    path,
    mode: "100644",
    type: "blob",
    sha,
  }));
  // A null sha removes the path from the new tree. GitHub rejects the whole
  // request if the path is not in base_tree, so callers must only pass paths
  // they have confirmed exist.
  for (const path of deletions) {
    entries.push({ path, mode: "100644", type: "blob", sha: null });
  }
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
