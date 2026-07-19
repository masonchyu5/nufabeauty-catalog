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
