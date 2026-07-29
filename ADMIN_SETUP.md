# Admin page — one-time setup

The admin page lives at `https://<your-domain>/admin.html`. Staff log in with a
shared password, drop in a replacement CSV and/or product photos (individual
files or a whole folder), review the validation report, and hit **Publish**.
Publishing makes one git commit; a GitHub Action rebuilds the catalog and
Vercel redeploys. If the build fails, nothing changes on the live site.

Before the page works, do the two steps below once.

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
2. `https://<domain>/items_chemical_master.csv` → **404** (`.vercelignore`
   keeps the wholesale price list and raw photos off the public site).
3. Log in at `/admin.html`, drop one photo, wait for "✓ uploaded", Publish.
   Confirm one new commit on `main`, the *Build catalog* Action runs, and the
   live catalog updates a few minutes later.

## Day-to-day notes

- **CSV upload fully replaces** `items_chemical_master.csv` — there is no
  row-level merge. "Download current CSV" on the admin page always gives the
  live version to edit from.
- Because the replace is wholesale, step 3 shows a **"Changes vs live" panel**:
  how many products the uploaded file changes, adds and removes, and the exact
  field-level before/after for each. Read it before publishing — a spreadsheet
  that silently dropped rows shows up here as a large "removed" count, which
  nothing else catches. Rows are matched by SKU, so reordering the file is
  correctly reported as no change.
- The CSV columns are `sku, name, upc, item_order, brand, brand_abbrev,
  brand_order, unit_price, qty_display`. A file still carrying the retired
  `image_path`, `category`, `verified` or General-catalog columns (`section`,
  `section_order`, `group_title`, `group_order`) still publishes — they are
  ignored with a warning — but re-download to get the current columns.
- **Photos are matched to products by filename**: a product shows the photo
  whose name matches its SKU, so `CH110612.jpg` is the photo for SKU
  `CH110612`. Uploading is the whole job — there is no path to type into the
  CSV. Case does not matter.
- A different extension is a **different file**, so re-uploading `CH1.jpg` as
  `CH1.webp` leaves both in the repo. When a SKU has several, the build picks
  by extension: **jpg › webp › png › jpeg**. The loser is dead weight; step 3
  labels it "not shown" so you can find and delete it. Re-uploading with the
  *same* filename replaces the file outright.
- **Step 3 deletes photos.** Search or tick "Only ones not shown", select, and
  publish. Deleting removes both the master in `pages/chemical-upc-v3/` and the
  display copy in `images/products-normalized/`, in the same commit as any CSV
  or upload changes. A photo is only removed from the display copy directory
  once no remaining master still derives it.
- Photos land in `pages/chemical-upc-v3/` — these are the **master copies**,
  and the 900px display images the catalog shows are derived from them by the
  build. Originals upload byte-identical when 4 MB or under; bigger photos are
  shrunk in the browser only as much as needed to fit the upload limit. A file
  with the same name as an existing one replaces it; the CSV's `image_path`
  column decides which photo each product shows.
- If how display images are produced ever changes (size, trimming, quality),
  bump `NORMALIZATION_VERSION` in `build_catalog.py` — the next build then
  regenerates every display image from its master copy.
- Uploads are throttled (~1/sec) to stay under GitHub's rate limits. A few
  hundred images per session is the practical ceiling; a full re-import of all
  1,400+ photos is still faster done from a laptop with `git push`.
- Finished uploads are remembered in the browser for 24h, so a closed tab or
  lost connection resumes instead of restarting.
- Login sessions last 8 hours; 10 wrong passwords from one address locks
  login for 15 minutes.
