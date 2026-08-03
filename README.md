# Nu Fashion Beauty Supply — product catalog

Static product catalogs generated from CSV data and product photos, deployed to
Vercel at `nufabeauty-catalog.vercel.app`.

Two catalogs live here. **Chemical is complete and live. General has data and
images but is not yet built or published.**

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
  general/                     empty (.gitkeep) — awaits the General build

scripts/
  build_catalog.py             renders the site (Chemical only today)
  fetch_general_images.py      downloads General masters from go-upc
  archive/
    fetch_chemical_images.py   obsolete Chemical fetcher (CloakBrowser)

templates/                     Jinja2
  base.html                    shared page shell
  index.html                   shared home page
  chemical.html                Chemical catalog shell
  _page_chemical.html          Chemical per-page partial
  general.html                 unused — never rendered
  _page_general.html           unused — never rendered

api/                           Vercel serverless functions (admin backend)
assets/                        styles.css + placeholder.svg (both shared)

index.html                     generated
chemical.html                  generated, ~28,600 lines
admin.html                     admin UI
```

---

## Chemical vs General

| | Chemical | General |
|---|---|---|
| **Status** | live | data + images only |
| **Items** | `source/chemical/items.csv` — 1,612 | `source/general/items.csv` — 724 |
| **Masters** | 1,413 | 637 (85 products still have none) |
| **Display images** | 1,410 in `images/chemical/` | none |
| **Generated page** | `chemical.html` | none |
| **Nav link** | yes | no |
| **Builder** | `build_chemical_pages()` — called | `build_general_pages()` — defined but **never called** |
| **Templates** | rendered | present, unused |
| **Admin support** | full | none |
| **Image fetcher** | archived (obsolete) | `scripts/fetch_general_images.py` |

### Why they can't share one code path yet

The two CSVs are shaped differently:

| | Chemical | General |
|---|---|---|
| Grouping | `brand`, `brand_abbrev`, `brand_order` | `section`, `group_title`, + orders |
| Bullets | (none — the old `bullets.csv` was deleted) | inline `bullets` column, `\|`-separated |
| UPC format | leading apostrophe (`'034285106126`) | bare digits |

About 60% of `build_catalog.py` is catalog-agnostic (barcode generation, image
normalization, the rebuild cache). The remaining 40% is layout logic that
differs. The intended fix is one script driven by a per-catalog spec — not two
scripts, which would duplicate the shared 60%.

---

## Build pipeline

```
source/<catalog>/items.csv  ─┐
source/<catalog>/master-images/ ─┤
templates/                   ─┴─►  scripts/build_catalog.py  ─►  <catalog>.html
                                                              └►  images/<catalog>/
```

`python scripts/build_catalog.py` does, in order:

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

### Image normalization

Each master becomes one 900×900 display JPEG:

1. Apply EXIF rotation, convert to RGBA.
2. Trim near-white borders (`WHITE_TRIM_THRESHOLD = 14`).
3. Flatten onto white.
4. Resize with LANCZOS to fit 900×900 minus 28px padding.
5. Centre on a 900×900 white canvas.

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

`.github/workflows/build.yml` triggers on pushes to `main` touching:

```
source/chemical/**   templates/**   scripts/build_catalog.py   requirements-catalog.txt
```

It runs the build and commits `index.html`, `chemical.html`, `images/chemical`.
Trigger paths deliberately exclude everything the workflow itself commits, so it
can't retrigger itself.

Vercel deploys from `main` on every push. Deploys are atomic — no window where
HTML and images disagree.

---

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-catalog.txt

.venv/bin/python scripts/build_catalog.py        # rebuild the site
```

Fetching General product images (requires Bright Data credentials):

```bash
.venv/bin/python scripts/fetch_general_images.py --check      # verify connectivity
.venv/bin/python scripts/fetch_general_images.py --workers 8  # fetch all
```

Images are looked up on go-upc.com by UPC and saved as lossless WebP at original
dimensions, named by SKU. Existing files are skipped, so runs are resumable.
Pass `--api-key`, set `BRIGHTDATA_API_KEY`, or paste at the hidden prompt.

**Pillow is required.** Without it `build_catalog.py` copies images unnormalized
instead of failing, which silently produces a wrong site.

---

## Known gaps

- **General is unbuilt** — `build_general_pages()` is never called, its
  templates are never rendered, and `images/general/` is empty.
- **85 General products have no master image** (637 of 722 rows with a UPC),
  concentrated in MISCELLANEOUS and JOY PRODUCTS. Most are likely genuine gaps
  in go-upc's database rather than fetch failures; not fully diagnosed.
- **`generalcatalogexample.pdf`** is 224 MB in Git LFS and consumes most of a
  free 1 GB LFS quota. It is source material, not deployed.
- **Image delivery is single-size.** Every viewport downloads the same 900×900
  file. Responsive `srcset` variants would cut typical page weight several-fold.
- **`assets/barcode-placeholder.svg` is orphaned** — barcodes are generated
  inline; nothing references it.
- **`ADMIN_UPLOAD_PLAN.md` and `ADMIN_SETUP.md` document pre-reorganization
  paths** and are out of date.
