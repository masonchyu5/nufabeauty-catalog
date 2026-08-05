# Nu Fashion Beauty Supply — product catalog

Static product catalogs generated from CSV data and product photos, deployed to
Vercel at `nufabeauty-catalog.vercel.app`.

Two catalogs live here. **Both are live: Chemical at `/chemical.html`, General
at `/general.html`.** General is reachable by direct URL only — it is not
linked from the shared nav or landing page, by owner decision.

Binding constraints for any catalog work — product data only from the CSVs,
master images never deleted, consistent fonts across both catalogs, and more —
live in `CLAUDE.md` under **"Catalog generation — HARD RULES"**. Read them
before changing anything.

---

## The one rule

```
source/  scripts/  templates/        →  never deployed
images/  *.html  assets/  api/       →  public
```

`source/` holds inputs: item CSVs and full-resolution master photos.
`images/` holds outputs: web-sized copies the site actually serves.
`.vercelignore` excludes `source` and `scripts` wholesale, so "is this public?"
is answerable from the path alone.

Masters are never served. They exist so display images can be regenerated at any
size without re-sourcing the originals.

---

## Directory map

```
source/                        inputs — private
  chemical/
    items.csv                  1,612 rows × 9 cols
    master-images/             1,413 originals (mostly JPEG, ~161 MB)
  general/
    items.csv                    724 rows × 11 cols
    master-images/               637 originals (lossless WebP, ~180 MB)

images/                        outputs — public
  chemical/                    1,410 JPEG @ 900×900 + .manifest.json
  general/                     637 WebP ≤ 900px, aspect-preserved + .manifest.json

scripts/
  build_catalog.py             renders index.html + the Chemical catalog
  build_general.py             renders the General catalog (standalone script)
  fetch_general_images.py      downloads General masters from go-upc
  archive/
    fetch_chemical_images.py   obsolete Chemical fetcher (CloakBrowser)

templates/                     Jinja2
  base.html                    shared page shell
  index.html                   shared home page
  chemical.html                Chemical catalog shell
  _page_chemical.html          Chemical per-page partial
  general.html                 General catalog shell (section TOC + page stack)
  _page_general.html           General per-page partial

api/                           Vercel serverless functions (admin backend)
assets/                        styles.css (shared) + general.css (General only)
                               + placeholder.svg (shared)

index.html                     generated
chemical.html                  generated, ~28,600 lines
general.html                   generated, 144 print pages
admin.html                     admin UI
```

---

## Chemical vs General

| | Chemical | General |
|---|---|---|
| **Status** | live at `/chemical.html` | live at `/general.html` (standalone URL) |
| **Items** | `source/chemical/items.csv` — 1,612 | `source/general/items.csv` — 724 |
| **Masters** | 1,413 | 637 (87 products have none → placeholder) |
| **Display images** | 1,410 JPEG 900×900 | 637 WebP ≤ 900px, aspect-preserved |
| **Generated page** | `chemical.html` — 98 pages, fixed 4×5 grid | `general.html` — 144 pages, dynamic layout |
| **Nav link** | yes | no (owner decision) |
| **Builder** | `scripts/build_catalog.py` | `scripts/build_general.py` |
| **CI workflow** | `build.yml` | `build-general.yml` |
| **Admin support** | full | none |
| **Image fetcher** | archived (obsolete) | `scripts/fetch_general_images.py` |

### Why there are two build scripts (by design)

The two CSVs are shaped differently:

| | Chemical | General |
|---|---|---|
| Grouping | `brand`, `brand_abbrev`, `brand_order` | `section`/`group_title` + three order columns |
| Bullets | (none — the old `bullets.csv` was deleted) | inline `bullets` column, `\|`-separated |
| UPC format | leading apostrophe (`'034285106126`) | bare digits |

An earlier plan was one script driven by a per-catalog spec. The owner decided
otherwise when General was built: the catalogs must be editable **without any
risk to each other**, so `build_general.py` is fully standalone and the small
shared helpers (slug, barcode SVG, the manifest scheme) are copied, not
imported. The layout logic was never shareable anyway — Chemical fills a fixed
4×5 grid; General packs variable-height cards against a height model. Do not
"deduplicate" this; see the hard rules in `CLAUDE.md`.

---

## Build pipeline

```
source/<catalog>/items.csv      ─┐
source/<catalog>/master-images/ ─┤   scripts/build_catalog.py  ─►  index.html + chemical.html + images/chemical/
templates/                      ─┴─►
                                     scripts/build_general.py  ─►  general.html + images/general/
```

### Chemical build — `build_catalog.py` does, in order:

1. **Load** `source/chemical/items.csv`.
2. **Filter** — `in_scope()` drops rows missing `brand`, `brand_abbrev` or
   `brand_order`, so incomplete rows can't break page grouping.
3. **Index masters** — `master_image_index()` matches a product to its photo by
   **filename stem == SKU**. There is no path column in the CSV. If a SKU has
   several masters (a re-upload in another format leaves the old file behind),
   `IMAGE_PREFERENCE` picks the winner: `.jpg` > `.webp` > `.png` > `.jpeg`.
4. **Normalize images** into `images/<catalog>/` (see below).
5. **Generate barcodes** — EAN-13/UPC-A rendered as inline SVG from the `upc`
   column. No external library.
6. **Paginate** — `build_chemical_pages()` groups by brand into fixed
   letter-sized print pages, each a brand card plus a 4×5 item grid.
7. **Render** via Jinja2 → `index.html` and `chemical.html`.
8. **Save the manifest** for incremental rebuilds.

### General build — `build_general.py` does, in order:

1. **Load + validate** `source/general/items.csv` — the 11-column header must
   match exactly; the build hard-fails on drift.
2. **Tree the rows** — sections by `section_order`, groups by
   (`group_order`, `group_title`) — the *pair* is the group identity, so blank
   titles are real, distinct untitled groups — items by `item_order`; blank
   orders sort last.
3. **Index masters** by SKU filename (`.webp` > `.jpg` > `.png` > `.jpeg`).
4. **Normalize images** into `images/general/` (see below); outputs whose SKU
   or master disappeared are pruned.
5. **Pick each group's card variant** — `hero` (2-col, big image), `standard`
   (3-col), `compact` (4-col), `wide` (2-col, image left) — from the
   `SECTION_STYLE` map plus content heuristics; `GROUP_STYLE` overrides
   single groups.
6. **Paginate deterministically** — card heights are estimated from CSV text
   only (never image dimensions, so image swaps can never reflow pages);
   each section starts a new page with an accent banner; a group heading is
   priced with its first row so it can never orphan; groups split at row
   boundaries onto slim-banner continuation pages. The model's constants
   mirror exact dimensions in `assets/general.css` — **change them together**.
7. **Barcodes** — same inline UPC-A/EAN-13 SVG approach, rendered 1.4in wide
   so a printed page scans.
8. **Render** `templates/general.html` (+ per-page partial, + screen-only
   section TOC) → `general.html`; save the manifest; print a summary with
   page count and the fullest pages' fill percentages.

Unlike Chemical, **General has no silent fallback**: missing Pillow, missing
WebP support, or any single image failure aborts the build.

### Image normalization

Chemical — each master becomes one 900×900 display JPEG:

1. Apply EXIF rotation, convert to RGBA.
2. Trim near-white borders (`WHITE_TRIM_THRESHOLD = 14`).
3. Flatten onto white.
4. Resize with LANCZOS to fit 900×900 minus 28px padding.
5. Centre on a 900×900 white canvas.

General — same EXIF/trim/flatten, but **no square canvas**: aspect ratio is
preserved, the longest side is capped at 900px (never upscaled), and output is
WebP quality 82. Cards letterbox the image inside fixed boxes, so image
dimensions never influence layout.

Masters are never modified.

### Incremental rebuilds

`images/<catalog>/.manifest.json` maps each display filename to the **SHA-256 of
the master it came from**. On rebuild, unchanged masters are skipped. Content
hashes are used rather than mtimes because CI checkouts assign fresh mtimes.

Bump `NORMALIZATION_VERSION` whenever the normalization *look* changes (size,
padding, trimming, quality); a version mismatch discards the manifest and
regenerates everything.

---

## Admin app

Lets a non-developer update the Chemical catalog from a browser — **no local
setup, no git**.

```
admin.html  ─►  api/*.js  ─►  GitHub API  ─►  commit to main
                                              └─►  Action rebuilds
                                                   └─►  Vercel deploys
```

| Route | Purpose |
|---|---|
| `login` / `logout` / `session` / `ping` | password auth via signed cookie |
| `csv` | download the current `items.csv` |
| `validate-csv` | check an uploaded CSV before publishing |
| `upload-image` | stage one photo as a git blob |
| `repo-images` | list existing masters |
| `publish` | commit CSV + images + deletions in **one** commit |
| `build-status` | poll the resulting Action run |

`api/_lib/github.js` holds the only path constants:

```js
IMAGES_DIR     = "source/chemical/master-images"
NORMALIZED_DIR = "images/chemical"
CSV_PATH       = "source/chemical/items.csv"
```

Publishing writes a single commit via the git tree API, so a partial failure
can't leave the repo half-updated. Deleting a master also deletes its derived
display copy — otherwise the repo would keep the bytes of every photo ever
removed. A concurrent publish gets a 409 rather than clobbering.

**The admin is Chemical-only.** Supporting General means parameterizing those
three constants.

Configuration lives in `ADMIN_SETUP.md` (four Vercel environment variables).

---

## CI/CD

`.github/workflows/build.yml` (Chemical) triggers on pushes to `main` touching:

```
source/chemical/**   templates/**   scripts/build_catalog.py   requirements-catalog.txt
```

It runs the build and commits `index.html`, `chemical.html`, `images/chemical`.

`.github/workflows/build-general.yml` (General) triggers on:

```
source/general/**   templates/general.html   templates/_page_general.html
templates/base.html   scripts/build_general.py   requirements-catalog.txt
```

It commits only `general.html`, `images/general`.

Both workflows' trigger paths deliberately exclude everything they themselves
commit, so neither can retrigger itself — keep that property. Their committed
paths are disjoint, so if one push fires both, the loser's `git pull --rebase`
retry always resolves. A General-template push also fires the Chemical
workflow (its `templates/**` glob): that run rebuilds byte-identical output
and exits at "nothing to commit" — expected noise, not a bug.

Vercel deploys from `main` on every push. Deploys are atomic — no window where
HTML and images disagree.

---

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-catalog.txt

.venv/bin/python scripts/build_catalog.py        # rebuild index + Chemical
.venv/bin/python scripts/build_general.py        # rebuild General
```

Fetching General product images (requires Bright Data credentials):

```bash
.venv/bin/python scripts/fetch_general_images.py --check      # verify connectivity
.venv/bin/python scripts/fetch_general_images.py --workers 8  # fetch all
```

Images are looked up on go-upc.com by UPC and saved as lossless WebP at original
dimensions, named by SKU. Existing files are skipped, so runs are resumable.
Pass `--api-key`, set `BRIGHTDATA_API_KEY`, or paste at the hidden prompt.

**Pillow is required — always build with `.venv/bin/python`, never bare
`python3`.** Without Pillow, `build_catalog.py` copies images unnormalized
instead of failing, which silently produces a wrong site; `build_general.py`
refuses to run instead.

---

## Known gaps

- **87 General products have no master image** (637 of 724 SKUs do), rendering
  the placeholder, concentrated in MISCELLANEOUS and JOY PRODUCTS. Most are
  likely genuine gaps in go-upc's database rather than fetch failures; not
  fully diagnosed. Masters are also expected to be swapped over time — the
  build re-derives changed images by content hash automatically.
- **General is not linked from the shared nav or landing page** — owner
  decision. Integrating it means editing `templates/base.html` /
  `templates/index.html`, which regenerates the Chemical shell too; do not do
  this without the owner asking.
- **The admin app is Chemical-only.** General CSV/image updates go through
  git. Supporting General means parameterizing the three constants in
  `api/_lib/github.js` (and note `api/_lib/csv.js` currently treats General's
  layout columns as retired headers).
- **Dead code is left in place on purpose**: `build_general_pages()` /
  `build_sku_location_index()` in `build_catalog.py` and the unused
  `.page-banner*` / `.banner-abbrev` / `.bc-placeholder` rules in
  `assets/styles.css` predate the real General build. Removing them means
  editing Chemical-owned files, which General work must not do.
- **`generalcatalogexample.pdf`** is 224 MB in Git LFS and consumes most of a
  free 1 GB LFS quota. It is source material, not deployed.
- **Image delivery is single-size.** Every viewport downloads the same file
  (900×900 JPEG for Chemical, ≤900px WebP for General). Responsive `srcset`
  variants would cut typical page weight several-fold.
- **`assets/barcode-placeholder.svg` is orphaned** — barcodes are generated
  inline; nothing references it.
- **`ADMIN_UPLOAD_PLAN.md` and `ADMIN_SETUP.md` document pre-reorganization
  paths** and are out of date.
