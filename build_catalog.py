"""Render the static page-by-page Chemical catalog from items_chemical_master.csv.

Layout model: each print page is a fixed letter-sized sheet with a vertical
brand card and a 4x5 grid of item cards. Rows missing Chemical brand metadata
are skipped so incomplete repair rows do not break page grouping.

Run:
    python build_catalog.py
"""

from __future__ import annotations

import csv
import glob
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    print("ERROR: jinja2 not installed. Run `pip install -r requirements-catalog.txt`.", file=sys.stderr)
    sys.exit(2)

try:
    from PIL import Image, ImageChops, ImageOps
except ImportError:  # Product images fall back to raw copies when Pillow is unavailable.
    Image = None
    ImageChops = None
    ImageOps = None


CATALOG_DIR = Path(__file__).resolve().parent
ITEMS_CSV = CATALOG_DIR / "items_chemical_master.csv"
BULLETS_CSV = CATALOG_DIR / "bullets.csv"
PAGES_DIR = CATALOG_DIR / "pages"
TEMPLATES_DIR = CATALOG_DIR / "templates"
SITE_DIR = CATALOG_DIR
SITE_IMAGES_DIR = SITE_DIR / "images"
NORMALIZED_IMAGES_DIR = SITE_IMAGES_DIR / "products-normalized"

CHEMICAL_LIMIT: int | None = None

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
NORMALIZED_IMAGE_SIZE = 900
NORMALIZED_IMAGE_PADDING = 28
WHITE_TRIM_THRESHOLD = 14

LEFT_ODD = {
    "0": "0001101",
    "1": "0011001",
    "2": "0010011",
    "3": "0111101",
    "4": "0100011",
    "5": "0110001",
    "6": "0101111",
    "7": "0111011",
    "8": "0110111",
    "9": "0001011",
}
LEFT_EVEN = {
    "0": "0100111",
    "1": "0110011",
    "2": "0011011",
    "3": "0100001",
    "4": "0011101",
    "5": "0111001",
    "6": "0000101",
    "7": "0010001",
    "8": "0001001",
    "9": "0010111",
}
RIGHT = {
    "0": "1110010",
    "1": "1100110",
    "2": "1101100",
    "3": "1000010",
    "4": "1011100",
    "5": "1001110",
    "6": "1010000",
    "7": "1000100",
    "8": "1001000",
    "9": "1110100",
}
EAN13_PARITY = {
    "0": "LLLLLL",
    "1": "LLGLGG",
    "2": "LLGGLG",
    "3": "LLGGGL",
    "4": "LGLLGG",
    "5": "LGGLLG",
    "6": "LGGGLL",
    "7": "LGLGLG",
    "8": "LGLGGL",
    "9": "LGGLGL",
}


def _int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 10**9


def _slug(text: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return out or "untitled"


def _unique_anchor(text: str, used: dict[str, int], prefix: str = "brand") -> str:
    base = f"{prefix}-{_slug(text)}"
    used[base] += 1
    if used[base] == 1:
        return base
    return f"{base}-{used[base]}"


def _digits(text: str | None) -> str:
    return re.sub(r"\D+", "", text or "")


def _price(text: str | None) -> str:
    value = (text or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return ""
    return value


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


def load_items() -> list[dict[str, str]]:
    with ITEMS_CSV.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_bullets() -> dict[str, list[str]]:
    by_sku: dict[str, list[tuple[int, str]]] = defaultdict(list)
    if not BULLETS_CSV.exists():
        return {}
    with BULLETS_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sku = (row.get("sku") or "").strip()
            text = (row.get("text") or "").strip()
            if not sku or not text:
                continue
            by_sku[sku].append((_int(row.get("order", "")), text))
    return {sku: [t for _, t in sorted(lst)] for sku, lst in by_sku.items()}


def build_sku_location_index() -> dict[str, tuple[int, int]]:
    index: dict[str, tuple[int, int]] = {}
    for path in sorted(glob.glob(str(PAGES_DIR / "page_*.json"))):
        page_num = int(Path(path).stem.split("_")[1])
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for item in data.get("items", []) or []:
            sku = (item.get("sku") or "").strip()
            order = item.get("item_order")
            if sku and order is not None and sku not in index:
                index[sku] = (page_num, int(order))
    return index


def has_chemical_brand_metadata(row: dict[str, str]) -> bool:
    return all((row.get(col) or "").strip() for col in ("brand", "brand_abbrev", "brand_order"))


def in_scope(rows: list[dict[str, str]]) -> tuple[list[dict], dict[str, int]]:
    chemical_all = [r for r in rows if (r.get("category") or "").strip() == "Chemical"]
    chemical = [r for r in chemical_all if has_chemical_brand_metadata(r)]
    chemical.sort(key=lambda r: (_int(r["brand_order"]), _int(r["item_order"])))
    if CHEMICAL_LIMIT is not None:
        chemical = chemical[:CHEMICAL_LIMIT]
    stats = {
        "chemical_all": len(chemical_all),
        "skipped_missing_brand": len(chemical_all) - len(chemical),
    }
    return chemical, stats


def _copy(src_rel: str, dest_name: str) -> str | None:
    src = CATALOG_DIR / src_rel
    if not src.exists():
        return None
    dest = SITE_IMAGES_DIR / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
        shutil.copy2(src, dest)
    return f"images/{dest_name}"


def _image_dest_name(sku: str, src_rel: str) -> str:
    suffix = Path(src_rel).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    return f"{_slug(sku)}{suffix}"


def _expanded_bbox(bbox: tuple[int, int, int, int], size: tuple[int, int], pad: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    width, height = size
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def normalized_product_image(src_rel: str, sku: str) -> str | None:
    """Create a trimmed, square display image without changing the source file."""
    src = CATALOG_DIR / src_rel
    if not src.exists():
        return None
    if Image is None or ImageChops is None or ImageOps is None:
        return _copy(src_rel, _image_dest_name(sku, src_rel))

    dest_name = f"{_slug(sku)}.jpg"
    dest = NORMALIZED_IMAGES_DIR / dest_name
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return f"images/products-normalized/{dest_name}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGBA")

            alpha = image.getchannel("A")
            if alpha.getextrema()[0] < 250:
                alpha_bbox = alpha.point(lambda x: 255 if x > 5 else 0).getbbox()
                bbox = alpha_bbox
            else:
                white = Image.new("RGBA", image.size, (255, 255, 255, 255))
                diff = ImageChops.difference(image, white).convert("L")
                mask = diff.point(lambda x: 255 if x > WHITE_TRIM_THRESHOLD else 0)
                bbox = mask.getbbox()

            if bbox:
                crop_pad = max(4, int(max(image.size) * 0.006))
                image = image.crop(_expanded_bbox(bbox, image.size, crop_pad))

            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))

            max_content = NORMALIZED_IMAGE_SIZE - NORMALIZED_IMAGE_PADDING * 2
            scale = min(max_content / background.width, max_content / background.height)
            resized = background.resize(
                (
                    max(1, round(background.width * scale)),
                    max(1, round(background.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            canvas = Image.new("RGB", (NORMALIZED_IMAGE_SIZE, NORMALIZED_IMAGE_SIZE), (255, 255, 255))
            x = (NORMALIZED_IMAGE_SIZE - resized.width) // 2
            y = (NORMALIZED_IMAGE_SIZE - resized.height) // 2
            canvas.paste(resized, (x, y))
            canvas.save(dest, "JPEG", quality=92, optimize=True)
    except Exception as exc:
        print(f"  WARNING: could not normalize {src_rel}: {exc}", file=sys.stderr)
        return _copy(src_rel, _image_dest_name(sku, src_rel))

    return f"images/products-normalized/{dest_name}"


def make_product(row: dict, bullets: dict) -> dict:
    sku = row["sku"]
    upc = _digits(row.get("upc"))

    image_src = None
    rel = (row.get("image_path") or "").strip()
    if rel:
        image_src = normalized_product_image(rel, sku)

    return {
        "sku": sku,
        "name": (row.get("name") or "").strip(),
        "unit_price": _price(row.get("unit_price")),
        "qty_display": (row.get("qty_display") or "").strip(),
        "upc": upc,
        "brand": (row.get("brand") or "").strip(),
        "brand_abbrev": (row.get("brand_abbrev") or "").strip(),
        "image_src": image_src,
        "barcode_src": None,
        "barcode_svg": barcode_svg(upc),
        "bullets": bullets.get(sku, []),
    }


def build_general_pages(rows: list[dict], bullets: dict, sku_loc: dict) -> list[dict]:
    """General catalog: one print page per source PDF page, group_title sub-sections,
    3-column item grid with bullets and barcodes. Item layout mirrors the original
    print catalog.
    """
    pages_by_src: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    sections_by_src: dict[int, str] = {}

    for row in rows:
        loc = sku_loc.get(row["sku"])
        if not loc:
            continue
        src_page, order = loc
        sections_by_src.setdefault(src_page, (row.get("section") or "").strip())
        pages_by_src[src_page].append((order, row))

    ordered_src_pages = sorted(pages_by_src.keys())
    pages: list[dict] = []
    for i, src in enumerate(ordered_src_pages, start=1):
        items = sorted(pages_by_src[src], key=lambda t: t[0])

        groups: list[dict] = []
        seen_groups: dict[str, dict] = {}
        for _, row in items:
            grp_name = (row.get("group_title") or "").strip() or "â€”"
            grp = seen_groups.get(grp_name)
            if grp is None:
                grp = {"name": grp_name, "products": []}
                seen_groups[grp_name] = grp
                groups.append(grp)
            grp["products"].append(make_product(row, bullets))

        pages.append({
            "page_num": i,
            "source_page_num": src,
            "category": "General",
            "banner": (sections_by_src.get(src) or "GENERAL").upper(),
            "groups": groups,
        })
    return pages


def build_chemical_pages(
    rows: list[dict],
    bullets: dict,
) -> tuple[list[dict], list[dict]]:
    """Chemical catalog: fixed letter-size pages filled with reusable card slots.

    The builder consumes item rows in brand/item order. A full-name brand card is
    inserted once before each brand and always starts in the leftmost column.
    """
    SLOTS_PER_PAGE = 20
    COLUMNS = 4

    brand_groups: list[dict] = []
    used_anchors: dict[str, int] = defaultdict(int)
    current_key = None
    for row in rows:
        product = make_product(row, bullets)
        product_brand = (product.get("brand") or "").strip()
        product_abbrev = (product.get("brand_abbrev") or "").strip()
        brand_name = product_brand or product_abbrev or "UNBRANDED"
        brand_key = (product_brand or product_abbrev or "UNBRANDED").casefold()

        if brand_key != current_key:
            brand_groups.append({
                "brand": brand_name,
                "banner": brand_name.upper(),
                "brand_abbrev": product_abbrev,
                "anchor": _unique_anchor(f"{brand_name}-{product_abbrev}", used_anchors),
                "products": [],
            })
            current_key = brand_key

        brand_groups[-1]["products"].append(product)

    all_slots: list[dict] = []

    def pad_to_next_row() -> None:
        remainder = len(all_slots) % COLUMNS
        if remainder:
            all_slots.extend({"type": "empty"} for _ in range(COLUMNS - remainder))

    def pad_to_next_page() -> None:
        remainder = len(all_slots) % SLOTS_PER_PAGE
        if remainder:
            all_slots.extend({"type": "empty"} for _ in range(SLOTS_PER_PAGE - remainder))

    previous_group_slots = 0
    for group in brand_groups:
        group_slots = 1 + len(group["products"])

        if all_slots:
            pad_to_next_row()
            if (
                previous_group_slots > SLOTS_PER_PAGE
                and group_slots > SLOTS_PER_PAGE
                and len(all_slots) % SLOTS_PER_PAGE != 0
            ):
                pad_to_next_page()

        all_slots.append({
            "type": "brand",
            "brand": group["brand"],
            "banner": group["banner"],
            "brand_abbrev": group["brand_abbrev"],
            "anchor": group["anchor"],
            "item_count": len(group["products"]),
        })
        for product in group["products"]:
            all_slots.append({"type": "product", "item": product})
        previous_group_slots = group_slots

    pages: list[dict] = []
    brand_toc: list[dict] = []
    for i in range(0, len(all_slots), SLOTS_PER_PAGE):
        slots = all_slots[i:i + SLOTS_PER_PAGE]
        products = [slot["item"] for slot in slots if slot.get("type") == "product"]
        page_num = len(pages) + 1
        for slot in slots:
            if slot.get("type") == "brand":
                brand_toc.append({
                    "brand": slot["brand"],
                    "brand_abbrev": slot["brand_abbrev"],
                    "anchor": slot["anchor"],
                    "item_count": slot["item_count"],
                    "page_num": page_num,
                })
        pages.append({
            "page_num": page_num,
            "category": "Chemical",
            "source_page_num": "",
            "products": products,
            "slots": slots,
        })
    return pages, brand_toc


def main() -> int:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    SITE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    rows = load_items()
    bullets = load_bullets()
    chemical_rows, stats = in_scope(rows)
    chemical_pages, brand_toc = build_chemical_pages(chemical_rows, bullets)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    common = {"rel": ""}

    (SITE_DIR / "index.html").write_text(
        env.get_template("index.html").render(active="home", **common),
        encoding="utf-8",
    )
    (SITE_DIR / "chemical.html").write_text(
        env.get_template("chemical.html").render(
            active="chemical",
            pages=chemical_pages,
            brand_toc=brand_toc,
            total_items=len(chemical_rows),
            **common,
        ),
        encoding="utf-8",
    )

    products = [
        slot["item"]
        for page in chemical_pages
        for slot in page.get("slots", [])
        if slot.get("type") == "product"
    ]
    product_imgs = sum(1 for item in products if item.get("image_src"))
    barcode_imgs = sum(1 for item in products if item.get("barcode_svg"))
    total = len(chemical_rows)
    print(f"Wrote catalog site to {SITE_DIR}")
    print(f"  Chemical: {len(chemical_pages)} print page(s), {len(chemical_rows)} item(s)")
    print(f"  Skipped:  {stats['skipped_missing_brand']} Chemical row(s) missing brand metadata")
    print(f"  Images:   {product_imgs}/{total} product, {barcode_imgs}/{total} barcode")
    if Image is None:
        print("  WARNING: Pillow is not installed; product images were copied without normalization.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
