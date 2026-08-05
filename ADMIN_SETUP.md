# Admin page — one-time setup

The admin page lives at `https://<your-domain>/admin.html`. Staff log in with a
shared password, pick a catalog with the **Chemical / General** switch in the
header (the page turns green for Chemical, maroon for General), drop in a
replacement CSV and/or product photos (individual files or a whole folder),
review the validation report, and hit **Publish**. Publishing makes one git
commit; a GitHub Action rebuilds that catalog and Vercel redeploys. If the
build fails, nothing changes on the live site.

Before the page works, do the two steps below once. They cover both catalogs —
General needs nothing extra.

## 1. Create a GitHub token

1. Go to <https://github.com/settings/personal-access-tokens/new> (fine-grained
   tokens, not classic).
2. **Resource owner:** `masonchyu5`. **Repository access:** *Only select
   repositories* → `masonchyu5/nufabeauty-catalog`.
3. **Repository permissions:**
   - *Contents* → **Read and write** (required — this is how publishes commit)
   - *Actions* → **Read-only** (optional — lets the admin page show
     "Building… / Published ✓" after a publish; without it the page just says
     "check back in a few minutes")
4. Set an expiration and generate. **Write the expiry date on a calendar** —
   when the token expires, publishing stops with a "GitHub upload failed"
   error until you make a new token and update the env var.

## 2. Set Vercel environment variables

Vercel dashboard → the project → *Settings* → *Environment Variables*
(Production environment):

| Name | Value |
|---|---|
| `ADMIN_PASSWORD` | The shared login password. Pick a long one. |
| `SESSION_SECRET` | Random string for signing login cookies. Generate with `openssl rand -hex 32`. |
| `GH_TOKEN` | The token from step 1. |
| `GH_REPO` | `masonchyu5/nufabeauty-catalog` |

Redeploy once after saving (env vars only apply to new deployments).

## Verifying it works

1. `https://<domain>/api/ping` → `{"ok":true}` (functions deploy at all).
2. `https://<domain>/source/chemical/items.csv` → **404** (`.vercelignore`
   keeps the wholesale price lists and raw photos off the public site).
3. Log in at `/admin.html`, drop one photo, wait for "✓ uploaded", Publish.
   Confirm one new commit on `main`, the catalog's build Action runs, and the
   live catalog updates a few minutes later.

## Day-to-day notes

- **The catalog switch decides everything.** Chemical edits go to
  `source/chemical/…`, General edits to `source/general/…`; the two never mix
  in one publish. Switching clears anything staged but not yet published (the
  page asks first); already-uploaded photos are remembered, so re-adding the
  same files later is instant.
- **CSV upload fully replaces** that catalog's `items.csv` — there is no
  row-level merge. "Download current CSV" always gives the live version of
  the *selected* catalog to edit from (saved as `chemical-items.csv` or
  `general-items.csv` so the two can't be mixed up on your desktop).
- Because the replace is wholesale, step 4 shows a **"Changes vs live" panel**:
  how many products the uploaded file changes, adds and removes, and the exact
  field-level before/after for each. Read it before publishing — a spreadsheet
  that silently dropped rows shows up here as a large "removed" count, which
  nothing else catches. Rows are matched by SKU, so reordering the file is
  correctly reported as no change.
- **Chemical columns:** `sku, name, upc, item_order, brand, brand_abbrev,
  brand_order, unit_price, qty_display`. A file still carrying retired columns
  (`image_path`, `category`, `verified`, or the General layout columns) still
  publishes — they are ignored with a warning — but re-download to get the
  current columns.
- **General columns:** `sku, name, unit_price, qty_display, bullets, upc,
  section, section_order, group_title, group_order, item_order` — and unlike
  Chemical, the General build requires this header **exactly** (same names,
  same order, nothing extra), every SKU unique. The admin page checks the
  same rules before letting you publish. Blank `unit_price` (JOY PRODUCTS),
  blank `bullets`, and blank `upc` are normal — those rows simply render
  without that piece. `bullets` is one cell per product, with bullets
  separated by `|`.
- **Photos are matched to products by filename**: a product shows the photo
  whose name matches its SKU, so `CH110612.jpg` is the photo for SKU
  `CH110612` and `3ANN5909.webp` for `3ANN5909`. Uploading is the whole job —
  there is no path to type into the CSV. Case does not matter.
- A different extension is a **different file**, so re-uploading `CH1.jpg` as
  `CH1.webp` leaves both in the repo. When a SKU has several, the build picks
  by extension — Chemical: **jpg › webp › png › jpeg**; General:
  **webp › jpg › png › jpeg**. The loser is dead weight; step 3 labels it
  "not shown" so you can find and delete it. Re-uploading with the *same*
  filename replaces the file outright.
- **Step 3 deletes photos.** Search or tick "Only ones not shown", select, and
  publish. Deleting removes both the master in
  `source/<catalog>/master-images/` and the display copy in
  `images/<catalog>/`, in the same commit as any CSV or upload changes. A
  display copy is only removed once no remaining master still derives it.
- Photos land in `source/<catalog>/master-images/` — these are the **master
  copies**, and the display images the catalog shows (900px JPEG for
  Chemical, ≤900px WebP for General) are derived from them by the build.
  Originals upload byte-identical when 4 MB or under; bigger photos are
  shrunk in the browser only as much as needed to fit the upload limit.
- If how display images are produced ever changes (size, trimming, quality),
  bump `NORMALIZATION_VERSION` in that catalog's build script — the next
  build then regenerates every display image from its master copy.
- Uploads are throttled (~1/sec) to stay under GitHub's rate limits. A few
  hundred images per session is the practical ceiling; a full re-import of
  1,400+ photos is still faster done from a laptop with `git push`.
- Finished uploads are remembered in the browser for 24h, so a closed tab or
  lost connection resumes instead of restarting.
- Login sessions last 8 hours; 10 wrong passwords from one address locks
  login for 15 minutes.
- **Undoing a publish:** every publish is a plain git commit on `main`
  (`Admin publish (chemical|general): …`), so nothing is ever lost — any
  publish can be reverted from git history (`git revert <sha>` and push, or
  re-publish the previous CSV/photos through the admin), and the catalog
  rebuilds to the earlier state.
