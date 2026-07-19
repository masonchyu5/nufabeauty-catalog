# Plan: Web admin for catalog editing

Implement a password-protected `/admin` page on the deployed Vercel site that lets
authorized staff upload a replacement CSV plus product images, and publish the
rebuilt catalog — with no local Python, git, or laptop involved.

---

## 1. How the repo works today (read this first)

Static site, zero-config Vercel deploy. There is **no `vercel.json`, no build step**;
Vercel serves the repo root as-is.

`build_catalog.py` runs **manually on the maintainer's laptop** and writes its output
into the repo root, which is then committed by hand:

| Input | Required | Notes |
|---|---|---|
| `items_chemical_master.csv` | yes | 1,612 rows. Only `category == "Chemical"` rows with non-empty `brand`, `brand_abbrev`, `brand_order` are used (`build_catalog.py:213`) |
| `bullets.csv` | no | `sku,order,text`; absent → no bullets |
| `templates/{index,chemical,base,_page_chemical}.html` | yes | Jinja2 |
| `pages/chemical-upc-v3/*.jpg` | per-row | Located via the CSV's `image_path` column |

| Output | Notes |
|---|---|
| `index.html`, `chemical.html` | Written to repo root (`SITE_DIR`, `build_catalog.py:41`) |
| `images/products-normalized/*.jpg` | 900px squares via Pillow (`build_catalog.py:259`) |

`assets/styles.css` is **not** read by the script — templates only emit a `<link>` to it,
so CSS edits need no rebuild.

Dead code, leave alone: `build_general_pages()`, `build_sku_location_index()`,
`templates/general.html`, `templates/_page_general.html`, `items_general_master.csv`.

Barcodes are generated as inline SVG from the `upc` column — no files involved.

---

## 2. Architecture

Vercel serverless functions cannot write to the repo. So the admin page **pushes to
GitHub via the API**, and everything downstream behaves exactly as it does today.

```
browser  ──►  /api/upload-image  ──►  GitHub blob (dangling, not committed)
   │                                       │  repeat per image, collect SHAs
   │                                       ▼
   └──────►  /api/publish  ──────►  ONE commit (CSV + all image blobs)
                                           │
                                           ▼
                                  GitHub Action: build_catalog.py
                                  commits chemical.html + normalized images
                                           │
                                           ▼
                                     Vercel deploys
```

**Why blobs:** each image is its own small request (dodges the ~4.5MB function body
limit) and nothing is visible on the live site until the single final commit. Partial
failure leaves orphaned blobs, which GitHub garbage-collects. The site cannot end up
half-updated.

---

## 3. Decisions already made

Implement these as stated; flag the user only if a blocker appears.

- **One shared password**, stored as a Vercel env var. Named accounts can come later.
- **CSV upload fully replaces** `items_chemical_master.csv`. No row-level merge.
- **Images stay in the git repo** (`pages/chemical-upc-v3/`). Vercel Blob is a possible
  future migration if bulk uploads become routine — keep the upload step behind a small
  interface so it can be swapped.
- **No preview/staging deploy.** Publish goes straight to production.
- **Bulk ceiling:** throttle + resume, targeting a few hundred images per session.
  Full 1,400-image re-imports remain a laptop `git push`.

---

## 4. Phases

Each phase must be verified before starting the next.

### Phase 0 — De-risk Vercel functions (do this first)

Adding an `api/` directory to a project Vercel currently treats as pure static may
require a minimal `package.json` and/or `vercel.json`. Before writing anything real:

1. Add `api/ping.ts` returning `{ ok: true }`.
2. Deploy. Confirm `https://<domain>/api/ping` responds **and** that `index.html`,
   `chemical.html`, `assets/`, and `images/` still serve correctly.

Do not proceed until static serving is confirmed intact.

### Phase 1 — Automate the build

`.github/workflows/build.yml`, on push to `main`, `paths:` filtered to
`items_chemical_master.csv`, `bullets.csv`, `templates/**`,
`pages/chemical-upc-v3/**`, `build_catalog.py`, `requirements-catalog.txt`.

Steps: checkout → Python → `pip install -r requirements-catalog.txt` →
`python build_catalog.py` → commit `index.html`, `chemical.html`,
`images/products-normalized/**`.

**Loop prevention:** the paths filter deliberately excludes the files the Action itself
commits, so its own commit cannot retrigger it. Do not use `[skip ci]`.

**Also fix in this phase — the incremental-image check.** `normalized_product_image()`
(`build_catalog.py:269`) decides whether to re-process an image by comparing mtimes.
Git checkouts assign fresh mtimes, so in CI this is unreliable and may re-normalize all
1,400 images on every publish. Replace it with a content-hash manifest
(`images/products-normalized/.manifest.json`, mapping source path → source SHA-256),
committed alongside the images. Since the normalized images are already in the repo,
this makes CI builds naturally incremental with no caching infrastructure.

**Verify:** hand-edit one price in the CSV, push, confirm the Action rebuilds, the site
updates, and only the changed images were re-processed.

### Phase 2 — Auth

- `api/_auth.ts` — HMAC-signed session cookie helper: `issue()` and `requireSession()`.
- `api/login.ts` — timing-safe compare against `ADMIN_PASSWORD`; on success set cookie
  `HttpOnly; Secure; SameSite=Lax`, ~8h expiry. Rate-limit failed attempts.
- `api/logout.ts` — clear cookie.

**Verify:** cookie issued on correct password, rejected on wrong; `/api/ping` guarded
by `requireSession()` returns 401 when logged out.

### Phase 3 — Image upload

`api/upload-image.ts`. Accepts JSON `{ filename, data }` where `data` is base64.
Creates a GitHub blob (`POST /repos/{repo}/git/blobs`), returns `{ path, sha }`.
**Commits nothing.**

Validation, all mandatory:
- Session required.
- Strip any directory component — keep the basename only.
- Filename must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` and contain no `..`.
- Extension in `{.jpg, .jpeg, .png, .webp}`.
- Verify magic bytes actually match the extension.
- Reject > 4MB.
- Destination directory `pages/chemical-upc-v3` is **hardcoded server-side**. The client
  never supplies a path.

**Verify:** upload one image, confirm the blob SHA exists via the GitHub API and that
`main` is unchanged. Confirm `../../.github/workflows/x.yml` is rejected.

### Phase 4 — CSV validation

`api/validate-csv.ts`. Parses the uploaded CSV and returns a report without writing
anything: row count, in-scope count, missing/renamed headers, malformed `unit_price`
values, and `image_path` entries with no corresponding file in the repo or in the
current upload batch. Returns errors (block publish) separately from warnings.

**Verify:** a good CSV passes; a CSV with a renamed column or a bad price is rejected
with the offending row numbers.

### Phase 5 — Publish

`api/publish.ts`. Body: `{ csv, images: [{path, sha}] }`. Sequence:

1. Re-run Phase 4 validation server-side. Abort on any error.
2. `GET /git/ref/heads/main` → parent commit SHA.
3. `GET /git/commits/{parent}` → base tree SHA.
4. `POST /git/trees` with `base_tree` set, entries = CSV (inline `content`) + one entry
   per image blob SHA, mode `100644`.
5. `POST /git/commits` with the new tree and `[parent]`.
6. `PATCH /git/refs/heads/main`.

Return the commit SHA. A stale `parent` makes step 6 fail — surface that as
"someone else just published, reload" rather than force-updating the ref.

**Verify:** publish a CSV plus 2 images; confirm exactly one commit, that it contains
all three files, and that the Action then rebuilds.

### Phase 6 — Admin UI

`admin.html` at repo root. Sections: **Product data** (Download current CSV / drop
zone / live validation summary) → **Product images** (drop zone, progress bar, per-file
status list) → **Review** (change counts, warnings) → **Publish**.

- Drag-and-drop must accept a **dropped folder**, via `DataTransferItem.webkitGetAsEntry()`
  recursion — selecting hundreds of files individually is not viable.
- Uploads are the **master copies** (`pages/chemical-upc-v3/`), from which the build
  derives the normalized display images — and the derivation may change later. So
  originals upload byte-identical when ≤4MB; only larger photos are re-encoded
  client-side, at descending sizes (2400px → 900px), just enough to fit the limit.
- Uploads begin on drop. 3 concurrent workers, throttled to stay under GitHub's
  secondary rate limit on content-creating requests (pace to roughly 1/sec; the exact
  ceiling is undocumented — measure and back off on HTTP 403 with `Retry-After`).
- **Resumable:** record completed uploads in `localStorage` keyed by file content hash,
  so a closed tab or dropped connection resumes instead of restarting.
- Cross-check filenames against CSV `image_path` values in the browser and show
  per-file warnings *before* publish.
- **Publish** stays disabled until the CSV validates and every upload has settled.

**Verify:** end-to-end with ~30 images including one deliberately-unreferenced file;
confirm the warning appears and the live site updates.

### Phase 7 — Status feedback

After publish, poll GitHub's Actions API for the run triggered by that commit SHA and
show `Building… → Published ✓ / Failed ✗` with a link to the run log on failure.

Note for the UI copy: if the build fails, **nothing is committed and the live site stays
on the last working version.** A bad upload cannot take the catalog down.

### Phase 8 — Close the exposure

Vercel serves the entire repo, so `https://<domain>/items_chemical_master.csv` is
currently a public download of the wholesale price list — as are `bullets.csv`,
`build_catalog.py`, and all 1,414 raw source photos under `pages/`.

Add a `vercel.json` that blocks those paths from being served, or restructure so only
generated output sits in the served directory. Confirm `admin.html` and `/api/*` still
work afterward.

---

## 5. Security requirements (non-negotiable)

- **Path traversal is the critical one.** A file written to `.github/workflows/` executes
  code in CI holding the repo token. Basename-only, charset allowlist, extension
  allowlist, magic-byte check, hardcoded destination directory.
- `GH_TOKEN`: fine-grained PAT, scoped to this repo alone, `contents: write` only.
  Server-side only — it must never reach the browser. Note that fine-grained PATs expire;
  record the expiry date somewhere the maintainer will see it.
- Every `/api/*` route except `login` calls `requireSession()` first.
- Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, signed, bounded expiry.
- Password comparison must be timing-safe; login must be rate-limited.

## 6. Environment variables (Vercel dashboard)

| Name | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Shared admin login |
| `SESSION_SECRET` | HMAC key for signing session cookies |
| `GH_TOKEN` | Fine-grained PAT, contents:write, this repo only |
| `GH_REPO` | `masonchyu5/nufabeauty-catalog` |

## 7. Out of scope

Row-level CSV merging; per-user accounts; preview deploys; in-place editing of
individual product cards; migrating images to Vercel Blob; the dormant general-catalog
pipeline.

---

*This file lives in the repo root and is therefore publicly served until Phase 8 lands.
It contains no secrets, but move or block it if that matters.*
