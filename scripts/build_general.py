"""Render the static General catalog (general.html) from source/general/items.csv.

Layout model: letter-sized print pages with a dynamic layout driven by the
CSV's section/group organization. Each section starts a new page with an
accent-colored banner; groups render as a watermark heading plus a grid of
product cards. Card size and grid density vary per group (hero / standard /
compact / wide) while the product text stack (sku, name, qty+price, bullets,
barcode) keeps constant sizes and relative placement everywhere.

Pagination is deterministic: card heights are estimated from CSV text lengths
only (never from image dimensions, so swapping master images can never reflow
pages) and rows are packed against a per-page height budget.

This script is intentionally independent of scripts/build_catalog.py — the two
catalogs must be editable without risk to each other — so the small shared
helpers (slug, barcode SVG, manifest scheme) are copied, not imported.

Run:
    .venv/bin/python scripts/build_general.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    print("ERROR: jinja2 not installed. Run `pip install -r requirements-catalog.txt`.", file=sys.stderr)
    sys.exit(2)

# Unlike the Chemical build, Pillow is a hard requirement here. Chemical's
# silent copy-fallback would publish full-resolution masters (up to ~3000px)
# straight into the public images/ tree — never acceptable for this catalog.
try:
    from PIL import Image, ImageChops, ImageOps, features
except ImportError:
    print("ERROR: Pillow is required for the General build (no fallback). "
          "Run `pip install -r requirements-catalog.txt` or use .venv/bin/python.", file=sys.stderr)
    sys.exit(2)
if not features.check("webp"):
    print("ERROR: this Pillow build lacks WebP support; cannot read/write General images.", file=sys.stderr)
    sys.exit(2)


# This script lives in scripts/, so the repo root is one level up.
CATALOG_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = CATALOG_DIR / "source" / "general"
ITEMS_CSV = SOURCE_DIR / "items.csv"
MASTER_IMAGES_DIR = SOURCE_DIR / "master-images"
TEMPLATES_DIR = CATALOG_DIR / "templates"
SITE_DIR = CATALOG_DIR
NORMALIZED_IMAGES_DIR = SITE_DIR / "images" / "general"
NORMALIZED_MANIFEST_PATH = NORMALIZED_IMAGES_DIR / ".manifest.json"
OUT_HTML = SITE_DIR / "general.html"

EXPECTED_HEADER = [
    "sku", "name", "unit_price", "qty_display", "bullets", "upc",
    "section", "section_order", "group_title", "group_order", "item_order",
]

# Bump whenever normalize_image() changes how images look (size, trimming,
# quality, ...). A version mismatch discards the manifest so the next build
# regenerates every normalized image from its master.
NORMALIZATION_VERSION = 1
# Masters are volatile — many will be swapped out over time, possibly in other
# formats than the fetcher's lossless WebP. Preference decides deterministically
# when one SKU has masters in several formats.
IMAGE_PREFERENCE = (".webp", ".jpg", ".png", ".jpeg")
MAX_IMAGE_SIDE = 900          # longest side of a normalized image; never upscaled
WEBP_QUALITY = 82
WHITE_TRIM_THRESHOLD = 14

# ---------------------------------------------------------------------------
# Presentation constants (not product data): per-section accent colors and
# default card variants, matching the printed 2025 catalog's look.
# ---------------------------------------------------------------------------

DEFAULT_ACCENT = "#7a1f2b"
SECTION_ACCENTS = {
    "ELECTRICAL & THERMAL": "#c8202a",
    "BARBER": "#1c4f9b",
    "COMB": "#0e7c3f",
    "COLOR ACCESSORY": "#d4581f",
    "BOTTLE": "#5b5ea6",
    "HAIR SCISSOR & BLADE": "#5a6068",
    "FASHION APPAREL": "#8d1d4f",
    "HAIR ACCESSORY": "#b0327a",
    "HAIR ROLLER & ROD": "#0f7f8b",
    "COSMETIC PRODUCT": "#946f2e",
    "MANICURE & PEDICURE": "#7a4b94",
    "HAIR WEAVING TOOLS": "#3f6f42",
    "JOY PRODUCTS": "#e0a11b",
    "MISCELLANEOUS": "#c2185b",
}

# Card variants: how many grid columns a group renders in and how large its
# image boxes are. Sections listed here always use that variant; sections
# absent from the dict get a per-group content heuristic (choose_variant).
SECTION_STYLE = {
    "ELECTRICAL & THERMAL": "hero",
    "HAIR WEAVING TOOLS": "standard",
    "FASHION APPAREL": "wide",
    "MISCELLANEOUS": "wide",
    "HAIR ROLLER & ROD": "compact",
    "BOTTLE": "compact",
    "HAIR ACCESSORY": "standard",
    "HAIR SCISSOR & BLADE": "standard",
    "COLOR ACCESSORY": "standard",
    "COSMETIC PRODUCT": "standard",
    "MANICURE & PEDICURE": "standard",
}
# Hand-tuning escape hatch: (section, group_title) -> variant. Checked first.
GROUP_STYLE: dict[tuple[str, str], str] = {}

# ---------------------------------------------------------------------------
# Height model (px @ 96dpi). The CSS in assets/general.css uses these exact
# dimensions; keep the two in sync or pagination drifts from rendering.
# ---------------------------------------------------------------------------

PAGE_H = 1056                 # 11in
DISCLAIMER_H = 24             # shared .page-disclaimer (7.5pt + padding)
BANNER_H = 53                 # .g-banner 0.55in (section opener)
BANDBAR_H = 18                # .g-bandbar 0.18in (continuation)
FOOTER_H = 42                 # shared .page-foot (content-sized)
BODY_PAD_V = 24               # .g-body padding 0.15in top + 0.10in bottom

BUDGET_OPENER = PAGE_H - DISCLAIMER_H - BANNER_H - FOOTER_H - BODY_PAD_V    # 913
BUDGET_CONT = PAGE_H - DISCLAIMER_H - BANDBAR_H - FOOTER_H - BODY_PAD_V     # 948

# cols/media boxes per variant + estimated characters per line for Arial at
# the stack's font sizes (conservative; content width is ~739px).
VARIANTS = {
    "hero":     {"cols": 2, "media_h": 260, "name_cpl": 61, "bullet_cpl": 66},
    "standard": {"cols": 3, "media_h": 180, "name_cpl": 40, "bullet_cpl": 43},
    "compact":  {"cols": 4, "media_h": 130, "name_cpl": 29, "bullet_cpl": 31},
    "wide":     {"cols": 2, "media_h": 150, "name_cpl": 33, "bullet_cpl": 36},
}

SKU_H = 18                    # 11pt bold line
NAME_LINE_H = 15              # 9pt * 1.25 + margin share
NAME_MARGIN = 2
QTYPRICE_H = 24               # bold line + rule + margins
BULLET_LINE_H = 13.4          # 8pt * 1.22
BULLETS_PAD = 4
BARCODE_H = 54                # 0.5in svg + top padding
NO_BARCODE_H = 8
MEDIA_GAP = 6                 # image box -> text stack
ROW_GAP = 14                  # .g-grid row-gap
GROUP_GAP = 16                # .g-block + .g-block margin
HEADING_H = 30                # .g-heading reserved height
LONG_HEADING_CHARS = 34       # switch watermark heading to the smaller size
TEXT_SAFETY = 1.05            # multiplier on the text stack estimate

STATS = {"normalized": 0, "reused": 0, "placeholder": 0, "pruned": 0, "warnings": 0}


def _int(s: str | None) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 10**9


def _slug(text: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return out or "untitled"


def _digits(text: str | None) -> str:
    return re.sub(r"\D+", "", text or "")


def _price(text: str | None) -> str:
    value = (text or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return ""
    return value


def _accent_fade(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, 0)"


# ---------------------------------------------------------------------------
# Barcodes (UPC-A / EAN-13 module encoding -> inline SVG, print-scannable)
# ---------------------------------------------------------------------------

LEFT_ODD = {
    "0": "0001101", "1": "0011001", "2": "0010011", "3": "0111101", "4": "0100011",
    "5": "0110001", "6": "0101111", "7": "0111011", "8": "0110111", "9": "0001011",
}
LEFT_EVEN = {
    "0": "0100111", "1": "0110011", "2": "0011011", "3": "0100001", "4": "0011101",
    "5": "0111001", "6": "0000101", "7": "0010001", "8": "0001001", "9": "0010111",
}
RIGHT = {
    "0": "1110010", "1": "1100110", "2": "1101100", "3": "1000010", "4": "1011100",
    "5": "1001110", "6": "1010000", "7": "1000100", "8": "1001000", "9": "1110100",
}
EAN13_PARITY = {
    "0": "LLLLLL", "1": "LLGLGG", "2": "LLGGLG", "3": "LLGGGL", "4": "LGLLGG",
    "5": "LGGLLG", "6": "LGGGLL", "7": "LGLGLG", "8": "LGLGGL", "9": "LGGLGL",
}


def barcode_modules(upc: str) -> str | None:
    if not upc.isdigit() or len(upc) not in (12, 13):
        return None

    if len(upc) == 12:
        left = "".join(LEFT_ODD[digit] for digit in upc[:6])
        right = "".join(RIGHT[digit] for digit in upc[6:])
        return "101" + left + "01010" + right + "101"

    first = upc[0]
    parity = EAN13_PARITY[first]
    left_parts = [
        LEFT_ODD[digit] if code_type == "L" else LEFT_EVEN[digit]
        for digit, code_type in zip(upc[1:7], parity)
    ]
    right = "".join(RIGHT[digit] for digit in upc[7:])
    return "101" + "".join(left_parts) + "01010" + right + "101"


def barcode_svg(upc: str) -> str | None:
    modules = barcode_modules(upc)
    if not modules:
        return None

    quiet = 9
    width = len(modules) + quiet * 2
    bar_h = 31
    height = 42
    rects = []
    for i, bit in enumerate(modules):
        if bit == "1":
            rects.append(f'<rect x="{quiet + i}" y="0" width="1" height="{bar_h}"/>')
    return (
        f'<svg class="barcode-svg" viewBox="0 0 {width} {height}" '
        'preserveAspectRatio="none" shape-rendering="crispEdges" role="img" '
        f'aria-label="UPC {upc}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<g fill="#050505">{"".join(rects)}</g>'
        f'<text x="{width / 2:.1f}" y="39.5" text-anchor="middle" '
        'font-family="Arial, sans-serif" font-size="6.5" fill="#111">'
        f'{upc}</text></svg>'
    )


# ---------------------------------------------------------------------------
# Images: masters in source/general/master-images -> images/general/*.webp
# ---------------------------------------------------------------------------

_normalized_manifest: dict[str, str] | None = None
_used_dest_names: set[str] = set()


def _load_normalized_manifest() -> dict[str, str]:
    global _normalized_manifest
    if _normalized_manifest is None:
        try:
            with NORMALIZED_MANIFEST_PATH.open(encoding="utf-8") as f:
                data = json.load(f)
            images = data.get("images") if isinstance(data, dict) else None
            if data.get("normalization") == NORMALIZATION_VERSION and isinstance(images, dict):
                _normalized_manifest = {
                    k: v for k, v in images.items() if isinstance(k, str) and isinstance(v, str)
                }
            else:
                _normalized_manifest = {}
        except (OSError, json.JSONDecodeError, AttributeError):
            _normalized_manifest = {}
    return _normalized_manifest


def _save_normalized_manifest() -> None:
    if _normalized_manifest is None:
        return
    kept = {
        name: digest
        for name, digest in sorted(_normalized_manifest.items())
        if (NORMALIZED_IMAGES_DIR / name).exists()
    }
    NORMALIZED_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    NORMALIZED_MANIFEST_PATH.write_text(
        json.dumps({"normalization": NORMALIZATION_VERSION, "images": kept}, indent=1) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expanded_bbox(bbox: tuple[int, int, int, int], size: tuple[int, int], pad: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    width, height = size
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def normalize_image(src: Path, sku: str) -> str:
    """Trim white borders, downscale to MAX_IMAGE_SIDE, save WebP.

    Aspect ratio is preserved (no square canvas): the cards' fixed media boxes
    letterbox with object-fit, so image dimensions never influence layout.
    Failures are fatal on purpose — a broken master must be fixed or removed,
    never silently published.
    """
    dest_name = f"{_slug(sku)}.webp"
    dest = NORMALIZED_IMAGES_DIR / dest_name
    rel = f"images/general/{dest_name}"
    manifest = _load_normalized_manifest()
    src_hash = _file_sha256(src)
    if dest.exists() and manifest.get(dest_name) == src_hash:
        _used_dest_names.add(dest_name)
        STATS["reused"] += 1
        return rel

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGBA")

            alpha = image.getchannel("A")
            if alpha.getextrema()[0] < 250:
                bbox = alpha.point(lambda x: 255 if x > 5 else 0).getbbox()
            else:
                white = Image.new("RGBA", image.size, (255, 255, 255, 255))
                diff = ImageChops.difference(image, white).convert("L")
                bbox = diff.point(lambda x: 255 if x > WHITE_TRIM_THRESHOLD else 0).getbbox()

            if bbox:
                crop_pad = max(4, int(max(image.size) * 0.006))
                image = image.crop(_expanded_bbox(bbox, image.size, crop_pad))

            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))

            scale = min(1.0, MAX_IMAGE_SIDE / max(background.size))
            if scale < 1.0:
                background = background.resize(
                    (
                        max(1, round(background.width * scale)),
                        max(1, round(background.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            background.save(dest, "WEBP", quality=WEBP_QUALITY, method=6)
    except Exception as exc:
        sys.exit(f"ERROR: could not normalize {src} (SKU {sku}): {exc}")

    manifest[dest_name] = src_hash
    _used_dest_names.add(dest_name)
    STATS["normalized"] += 1
    return rel


_master_index: dict[str, Path] | None = None


def master_index() -> dict[str, Path]:
    """Map casefolded SKU -> master photo path (filename match, like Chemical)."""
    global _master_index
    if _master_index is None:
        best: dict[str, tuple[int, Path]] = {}
        if MASTER_IMAGES_DIR.is_dir():
            for path in sorted(MASTER_IMAGES_DIR.iterdir()):
                if not path.is_file() or path.suffix.lower() not in IMAGE_PREFERENCE:
                    continue
                rank = IMAGE_PREFERENCE.index(path.suffix.lower())
                key = path.stem.casefold()
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, path)
        _master_index = {k: p for k, (_, p) in best.items()}
    return _master_index


def prune_orphans() -> None:
    """Delete derived images whose SKU/master no longer exists.

    images/general is 100% generated output (nothing else writes here), so
    anything not produced by this run is a leftover from a removed or renamed
    product and would otherwise ship to the live site forever.
    """
    if not NORMALIZED_IMAGES_DIR.is_dir():
        return
    for path in sorted(NORMALIZED_IMAGES_DIR.glob("*.webp")):
        if path.name not in _used_dest_names:
            path.unlink()
            STATS["pruned"] += 1
            print(f"  pruned orphan image: {path.name}")


# ---------------------------------------------------------------------------
# CSV -> sections/groups/items
# ---------------------------------------------------------------------------

def load_rows() -> list[dict]:
    if not ITEMS_CSV.is_file():
        sys.exit(f"ERROR: {ITEMS_CSV} not found.")
    with ITEMS_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != EXPECTED_HEADER:
            sys.exit(
                "ERROR: unexpected CSV header in source/general/items.csv.\n"
                f"  expected: {','.join(EXPECTED_HEADER)}\n"
                f"  found:    {','.join(reader.fieldnames or [])}"
            )
        rows = []
        for idx, row in enumerate(reader):
            row["_idx"] = idx
            rows.append(row)
    return rows


def make_item(row: dict) -> dict:
    sku = (row.get("sku") or "").strip()
    upc = _digits(row.get("upc"))
    bullets = [part.strip() for part in (row.get("bullets") or "").split("|") if part.strip()]

    master = master_index().get(sku.casefold())
    if master is not None:
        image_src = normalize_image(master, sku)
    else:
        image_src = None
        STATS["placeholder"] += 1

    return {
        "sku": sku,
        "name": (row.get("name") or "").strip(),
        "unit_price": _price(row.get("unit_price")),
        "qty_upper": (row.get("qty_display") or "").strip().upper(),
        "bullets": bullets,
        "upc": upc,
        "barcode_svg": barcode_svg(upc),
        "image_src": image_src,
    }


def choose_variant(section_name: str, group_title: str, items: list[dict]) -> str:
    override = GROUP_STYLE.get((section_name, group_title))
    if override:
        return override
    style = SECTION_STYLE.get(section_name)
    if style:
        return style

    # Content heuristic for sections without a fixed style (BARBER, COMB, JOY):
    # long spec lists earn big cards, terse accessory rows pack densely.
    avg_bullets = sum(len(it["bullets"]) for it in items) / len(items)
    avg_chars = sum(sum(len(b) for b in it["bullets"]) for it in items) / len(items)
    max_name = max(len(it["name"]) for it in items)
    if avg_bullets >= 4 or avg_chars >= 220:
        return "hero"
    if avg_bullets <= 1 and avg_chars <= 40 and max_name <= 45:
        return "compact"
    return "standard"


def load_sections() -> list[dict]:
    rows = load_rows()
    skipped = 0
    section_map: dict[str, dict] = {}
    for row in rows:
        section_name = (row.get("section") or "").strip()
        if not section_name or not (row.get("sku") or "").strip():
            skipped += 1
            continue
        section = section_map.setdefault(section_name, {
            "name": section_name,
            "order": _int(row.get("section_order")),
            "slug": _slug(section_name),
            "accent": SECTION_ACCENTS.get(section_name, DEFAULT_ACCENT),
            "_rows": [],
        })
        section["_rows"].append(row)
    if skipped:
        STATS["warnings"] += 1
        print(f"  WARNING: skipped {skipped} row(s) with blank sku/section", file=sys.stderr)

    sections = sorted(section_map.values(), key=lambda s: (s["order"], s["name"]))
    for section in sections:
        section["accent_fade"] = _accent_fade(section["accent"])
        # Group identity is (group_order, group_title): MISCELLANEOUS has two
        # distinct untitled groups that must not merge into one.
        group_map: dict[tuple[int, str], dict] = {}
        for row in section.pop("_rows"):
            key = (_int(row.get("group_order")), (row.get("group_title") or "").strip())
            group = group_map.setdefault(key, {
                "title": key[1],
                "order": key[0],
                "first_idx": row["_idx"],
                "_rows": [],
            })
            group["_rows"].append(row)
        groups = sorted(group_map.values(), key=lambda g: (g["order"], g["first_idx"]))
        for group in groups:
            group_rows = sorted(group.pop("_rows"), key=lambda r: (_int(r.get("item_order")), r["_idx"]))
            group["items"] = [make_item(r) for r in group_rows]
            group["variant"] = choose_variant(section["name"], group["title"], group["items"])
        section["groups"] = groups
        section["item_count"] = sum(len(g["items"]) for g in groups)
    return sections


# ---------------------------------------------------------------------------
# Height estimation + pagination
# ---------------------------------------------------------------------------

def _wrap_lines(text: str, chars_per_line: int) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / chars_per_line))


def estimate_card_height(item: dict, variant: str) -> float:
    spec = VARIANTS[variant]
    text_h = SKU_H
    text_h += _wrap_lines(item["name"], spec["name_cpl"]) * NAME_LINE_H + NAME_MARGIN
    text_h += QTYPRICE_H
    if item["bullets"]:
        lines = sum(_wrap_lines(b, spec["bullet_cpl"]) for b in item["bullets"])
        text_h += lines * BULLET_LINE_H + BULLETS_PAD
    text_h += BARCODE_H if item["barcode_svg"] else NO_BARCODE_H
    text_h *= TEXT_SAFETY

    if variant == "wide":
        return max(spec["media_h"], text_h)
    return spec["media_h"] + MEDIA_GAP + text_h


def estimate_row_height(row_items: list[dict], variant: str) -> float:
    return max(estimate_card_height(item, variant) for item in row_items)


def _new_page(section: dict, is_section_start: bool) -> dict:
    return {
        "section": section["name"],
        "section_slug": section["slug"],
        "accent": section["accent"],
        "accent_fade": section["accent_fade"],
        "is_section_start": is_section_start,
        "blocks": [],
    }


def paginate(sections: list[dict]) -> list[dict]:
    pages: list[dict] = []

    def close(page: dict, used: float) -> None:
        budget = BUDGET_OPENER if page["is_section_start"] else BUDGET_CONT
        page["fill_pct"] = round(100 * used / budget, 1)
        pages.append(page)

    for section in sections:
        page = _new_page(section, True)
        used = 0.0
        for group in section["groups"]:
            variant = group["variant"]
            cols = VARIANTS[variant]["cols"]
            items = group["items"]
            rows = [items[i:i + cols] for i in range(0, len(items), cols)]
            heights = [estimate_row_height(r, variant) for r in rows]
            heading_pending = bool(group["title"])
            i = 0
            while i < len(rows):
                budget = BUDGET_OPENER if page["is_section_start"] else BUDGET_CONT
                gap = GROUP_GAP if page["blocks"] else 0
                heading_h = HEADING_H if heading_pending else 0
                need = gap + heading_h + heights[i]
                page_is_empty = not page["blocks"]
                if used + need > budget and not page_is_empty:
                    close(page, used)
                    page = _new_page(section, False)
                    used = 0.0
                    continue
                if used + need > budget:
                    STATS["warnings"] += 1
                    print(
                        f"  WARNING: row taller than an empty page "
                        f"({section['name']} / {group['title'] or '(untitled)'}, "
                        f"row {i + 1}, {need:.0f}px > {budget}px) — it will clip",
                        file=sys.stderr,
                    )
                # Open a block: the heading is priced together with its first
                # row, so a heading can never be orphaned at a page bottom.
                block = {
                    "title": group["title"],
                    "show_heading": heading_pending,
                    "long_heading": len(group["title"]) > LONG_HEADING_CHARS,
                    "variant": variant,
                    "rows": [rows[i]],
                }
                page["blocks"].append(block)
                used += gap + heading_h + heights[i]
                heading_pending = False
                i += 1
                while i < len(rows) and used + ROW_GAP + heights[i] <= budget:
                    used += ROW_GAP + heights[i]
                    block["rows"].append(rows[i])
                    i += 1
        close(page, used)

    for num, page in enumerate(pages, 1):
        page["page_num"] = num
        for block in page["blocks"]:
            # "products", not "items": Jinja's block.items would resolve to
            # dict.items() instead of the key.
            block["products"] = [item for row in block.pop("rows") for item in row]
    return pages


def build_toc(sections: list[dict], pages: list[dict]) -> list[dict]:
    first_page = {}
    for page in pages:
        if page["is_section_start"] and page["section"] not in first_page:
            first_page[page["section"]] = page["page_num"]
    return [
        {
            "name": s["name"],
            "anchor": f"sec-{s['slug']}",
            "accent": s["accent"],
            "item_count": s["item_count"],
            "page_num": first_page.get(s["name"], 1),
        }
        for s in sections
    ]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def main() -> int:
    sections = load_sections()

    skus = [item["sku"] for s in sections for g in s["groups"] for item in g["items"]]
    slugs = [_slug(sku) for sku in skus]
    if len(set(slugs)) != len(slugs):
        seen: dict[str, str] = {}
        for sku, slug in zip(skus, slugs):
            if slug in seen:
                sys.exit(f"ERROR: SKU slug collision: {seen[slug]!r} and {sku!r} both map to {slug!r}")
            seen[slug] = sku

    pages = paginate(sections)
    toc = build_toc(sections, pages)
    prune_orphans()
    _save_normalized_manifest()

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    total_items = len(skus)
    html = env.get_template("general.html").render(
        rel="",
        active="general",
        pages=pages,
        toc=toc,
        total_items=total_items,
        total_pages=len(pages),
    )
    OUT_HTML.write_text(html, encoding="utf-8")

    group_count = sum(len(s["groups"]) for s in sections)
    fullest = sorted(pages, key=lambda p: p["fill_pct"], reverse=True)[:5]
    print("General catalog build")
    print(f"  sections: {len(sections)}   groups: {group_count}   items: {total_items}")
    print(f"  pages: {len(pages)} -> {OUT_HTML.name}")
    print(
        f"  images: {STATS['normalized']} normalized, {STATS['reused']} reused, "
        f"{STATS['placeholder']} placeholder, {STATS['pruned']} pruned"
    )
    print("  fullest pages: " + ", ".join(f"p{p['page_num']} {p['fill_pct']}%" for p in fullest))
    if STATS["warnings"]:
        print(f"  warnings: {STATS['warnings']} (see stderr)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
