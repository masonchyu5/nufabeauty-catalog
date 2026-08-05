# CLUADE.md

## Environment

I work from a Windows 11 client laptop and remotely access a Windows 11 host laptop over Tailscale + OpenSSH.

Client laptop:
- Used for keyboard, browser, downloads, and launching SSH.
- Client paths look like `C:\Users\mason\...`.
- Do not assume files on the client are visible to host-side tools.

Host laptop:
- `ssh g16-host` opens Host Windows.
- `ssh g16-wsl` opens Host WSL Ubuntu at `/home/mason`.
- Active development happens in Host WSL, not Host Windows.

Host WSL:
- Project files should live under `/home/mason/work/<project>`.
- Avoid working under `/mnt/c/...` except for temporary file transfer.
- Run Codex/Claude/Python/git from the project directory in Host WSL.

## Workflow

For persistent work:

```bash
cd ~/work/<project>
tmux new -A -s <project>
```

Run long-lived tools inside tmux:

```bash
codex
claude
python ...
```

Detach without stopping work:

```text
Ctrl-b d
```

Use Git for versioned project files. Use tar/scp only for moving large untracked files between client and host.

## Important

Host WSL is the source of truth for active project work. The client is mainly for remote access, downloads, and viewing files. Do not confuse Client Windows `C:\Users\mason\...`, Host Windows `C:\Users\mason\...`, and Host WSL `/home/mason/...`.

## Catalog generation — HARD RULES (binding for all design/code work)

Full pipeline documentation lives in README.md. These are the constraints that
must never be broken, in any session, for any reason.

### 1. Product data comes from the CSVs and nowhere else

- The only product data sources are `source/chemical/items.csv` and
  `source/general/items.csv`. Every SKU, name, price, quantity, UPC, bullet,
  brand, section, group, and ordering shown on a page comes from those files.
- Never invent, reword, reorder, or "fix" product data in templates, CSS, or
  build scripts. No hardcoded product names, prices, taglines, or extra
  bullets — if it is not in the CSV, it does not render.
- Presentation constants in code (section accent colors, card variants, fonts,
  spacing) are fine: they control HOW data is shown, never WHAT is shown.
- To change product content: edit the CSV and rebuild. To change looks: edit
  templates / CSS / build scripts and rebuild. Never cross the two.
- Header rows are contracts. Chemical:
  `sku,name,upc,item_order,brand,brand_abbrev,brand_order,unit_price,qty_display`.
  General:
  `sku,name,unit_price,qty_display,bullets,upc,section,section_order,group_title,group_order,item_order`
  (General's build validates the header exactly and hard-fails on drift).
- How the CSV drives layout — Chemical: rows group by brand
  (`brand_order`, then `item_order`) into a fixed 4×5 grid, 20 slots/page.
  General: `section_order` → new page + accent banner per section;
  (`group_order`, `group_title`) → watermark-headed group blocks (group
  identity is the PAIR — blank titles are real, distinct groups);
  `item_order` orders cards; `bullets` is one field, ` | `-separated.
- Blank fields are features, not bugs: blank `unit_price` renders qty only
  (all JOY PRODUCTS rows), blank `bullets` renders no list, blank `upc`
  renders no barcode, missing master image renders the placeholder. Do not
  "fill in" or hide these rows.

### 2. Master images are originals — never delete, move, edit, or rename

- `source/chemical/master-images/` and `source/general/master-images/` hold
  the only originals. A product's photo is the master whose filename equals
  its SKU (there is no image-path column in the CSV).
- Masters are volatile by design: the owner swaps them over time. Replacing a
  file under the same SKU name is the supported workflow; builds detect the
  change by content hash and re-derive only what changed.
- Never write into master-images from build code, never re-encode a master in
  place, never delete one because it "looks unused". SKUs without masters are
  expected (87 in General) and render the placeholder on purpose.
- `images/chemical/` and `images/general/` are 100% derived output: safe to
  regenerate, never hand-edited, never a source of anything.

### 3. Generated files are never hand-edited

`index.html`, `chemical.html`, `general.html`, `images/chemical/**`,
`images/general/**`, and both `.manifest.json` files are build outputs. Edit
the inputs and rebuild — a hand edit is silently destroyed by the next CI run.

### 4. Builds run through the venv (or CI) — never bare python3

- Local: `.venv/bin/python scripts/build_catalog.py` (Chemical) and
  `.venv/bin/python scripts/build_general.py` (General).
- Bare `python3` has no Pillow: the Chemical build then SILENTLY publishes
  raw masters and wrong image paths (known past incident). The General build
  hard-fails instead — preserve that hard-fail behavior in any edit.
- CI: `.github/workflows/build.yml` (Chemical) and `build-general.yml`
  (General). Each workflow's trigger paths exclude its own outputs
  (self-retrigger protection) and each commits ONLY its own catalog's
  outputs. Keep both properties.

### 5. The catalogs stay decoupled in code, consistent in look

- Chemical and General have separate build scripts, templates, and CSS.
  Shared helpers were deliberately COPIED, not imported — keep it that way.
- Work on one catalog must not change the other's files or output. Proof
  required before pushing: rebuild the OTHER catalog and confirm
  `git diff` shows it byte-identical.
- Typography is shared and mandatory: print pages use Arial
  (`.chemical-page` and `.general-page` both set it); site chrome uses
  DM Serif Display + Inter Tight from `templates/base.html` +
  `assets/styles.css`. Never introduce a font to one catalog only.
- Shared page chrome (`.print-page`, `.page-disclaimer`, `.page-foot`,
  the `@media print` rules in `assets/styles.css`) is reused read-only by
  both catalogs. A change there changes both — only do it intentionally.

### 6. Barcodes stay real

Barcodes are genuine UPC-A/EAN-13 module encodings generated from the CSV
`upc` column by the pure-python `barcode_svg()` in each build script, sized
print-scannable (1.4in wide in General, quiet zones intact). Never replace
them with decorative images, text-only UPCs, or shrink below scannable size.

### 7. General layout invariants

- The product text stack — bold SKU → name → `QTY  $PRICE` with thin rule →
  round black `•` bullets → barcode — keeps constant font sizes and order in
  every card variant. Variants (hero/standard/compact/wide) change ONLY grid
  columns and image box size/position.
- Pagination in `build_general.py` estimates heights from CSV text only,
  never from image dimensions, so image swaps can never reflow pages. Keep
  that property.
- The height-model constants in `build_general.py` (VARIANTS + layout
  constants) mirror exact dimensions in `assets/general.css`. Change them
  together or pagination drifts from what renders.
- Images must never overlap text: `.g-media img` fills its fixed, clipped box
  (`width/height: 100%` + `object-fit: scale-down`). Any media-box change
  must preserve "an image cannot escape its box".

### 8. Deployment contract

- The repo root is the Vercel site; `.vercelignore` keeps `source/`,
  `scripts/`, `templates/`, and docs out of the deploy. Masters must never
  become publicly reachable.
- General is linked from the shared topbar and landing page (owner-approved
  2026-08-05). Edits to `templates/base.html` / `templates/index.html` are
  shared: they regenerate the shells of BOTH catalogs, so rebuild both and
  verify the diffs contain only what you intended.
- The admin app (`admin.html` + `api/`) is Chemical-only and hardcoded to
  Chemical paths. General work must not touch it.

### 9. Pre-push verification (always)

1. Rebuild the changed catalog twice with `.venv/bin/python` — the second
   run must be a no-op (`git status` unchanged, manifest hits only).
2. Rebuild the untouched catalog — its outputs must be byte-identical.
3. Check the build summary against the output (page count, placeholder
   count, barcode count).
4. If layout or CSS changed, screenshot-check pages in a real browser
   (headless is fine): no image/text overlap, footers intact, page breaks
   clean in print preview.
