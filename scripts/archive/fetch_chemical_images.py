"""
Fetch chemical-catalog product images from go-upc.com using CloakBrowser.

Runs are scoped to ONE brand per invocation (default: brand_order == 1, i.e.
Africa's Best / ABE). Images that already exist on disk are skipped unless
--overwrite is passed. Always writes JPEG to pages/chemical-upc-v3/<SKU>.jpg.
The default CSV is items_chemical_master.csv.

Usage:
  python fetch_images.py                            # default: first brand
  python fetch_images.py --list-brands              # show all brands + counts
  python fetch_images.py --brand ABE                # by abbreviation
  python fetch_images.py --brand "Africa's Best"    # by full name
  python fetch_images.py --first 3 --headed         # smoke test, watch browser
  python fetch_images.py --sku AP41005              # single SKU (ignores --brand)
  python fetch_images.py --overwrite                # re-download existing files
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

SCRIPT_DIR = Path(__file__).parent
CATALOG_DIR = SCRIPT_DIR.parent.parent
DEFAULT_CSV = CATALOG_DIR / "items_chemical_master.csv"
OUTPUT_DIR = SCRIPT_DIR
PROFILE_DIR = SCRIPT_DIR / ".cloak_profile"
DEFAULT_METADATA_CSV = CATALOG_DIR / "go_upc_chemical_product_data.csv"

GO_UPC_SEARCH = "https://go-upc.com/search?q={upc}"
NOT_FOUND_TEXT = "Sorry, we were not able to find a product for"
GO_UPC_IMAGE_RE = re.compile(r"https://go-upc\.s3\.amazonaws\.com/images/\d+\.[a-zA-Z]+")
SCRIPT_JSONLD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
MIN_IMAGE_BYTES = 5_000
JPEG_QUALITY = 90
RATE_LIMIT_BACKOFFS = (60, 180, 600)  # seconds to wait on consecutive 429s before giving up on an item
WEB_UNLOCKER_ENDPOINT = "https://api.brightdata.com/request"
DEFAULT_WEB_UNLOCKER_ZONE = "nufaminer"

METADATA_FIELDS = [
    "catalog_sku",
    "catalog_upc",
    "catalog_name",
    "catalog_brand",
    "catalog_brand_abbrev",
    "catalog_brand_order",
    "catalog_item_order",
    "go_upc_url",
    "found",
    "error",
    "image_url",
    "product_name",
    "ean",
    "upc",
    "brand",
    "description",
    "ingredients",
    "package_quantity",
    "net_weight",
    "size",
    "product_dimension",
    "country_of_registration",
    "color",
    "category",
    "department",
    "commodity",
    "manufacturer",
    "height",
    "width",
    "length",
    "fields_json",
    "fetched_at",
]


def import_cloakbrowser():
    try:
        from cloakbrowser import launch_persistent_context
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: cloakbrowser\n"
            "Install with:  python -m pip install cloakbrowser playwright pillow"
        ) from exc
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: playwright\n"
            "Install with:  python -m pip install cloakbrowser playwright pillow"
        ) from exc
    return launch_persistent_context, PlaywrightTimeoutError


def import_pillow():
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: pillow\n"
            "Install with:  python -m pip install cloakbrowser playwright pillow"
        ) from exc
    return Image


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def clean_upc(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def chemical_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if row.get("category", "").lower() != "chemical":
            continue
        upc = clean_upc(row.get("upc", ""))
        if len(upc) not in (12, 13):
            continue
        row = dict(row)
        row["upc"] = upc
        out.append(row)
    return out


def brand_summary(rows: list[dict]) -> list[dict]:
    by_abbrev: dict[str, dict] = {}
    for r in rows:
        key = r.get("brand_abbrev") or r.get("brand") or "?"
        entry = by_abbrev.setdefault(
            key,
            {"brand": r.get("brand", ""), "abbrev": key, "order": r.get("brand_order", ""), "count": 0},
        )
        entry["count"] += 1
    def order_key(e: dict) -> tuple:
        try:
            return (0, int(e["order"]))
        except (ValueError, TypeError):
            return (1, e["abbrev"].lower())
    return sorted(by_abbrev.values(), key=order_key)


def resolve_brand(rows: list[dict], brand_arg: Optional[str]) -> tuple[str, str, str]:
    summary = brand_summary(rows)
    if not summary:
        sys.exit("No chemical items with UPCs found in the CSV.")
    if brand_arg is None:
        first = summary[0]
        return first["brand"], first["abbrev"], first["order"]
    needle = brand_arg.strip().lower()
    matches = [e for e in summary if e["abbrev"].lower() == needle or e["brand"].lower() == needle]
    if not matches:
        print(f"No brand matches {brand_arg!r}.\n\nAvailable brands:", file=sys.stderr)
        print_brand_table(summary, stream=sys.stderr)
        sys.exit(2)
    m = matches[0]
    return m["brand"], m["abbrev"], m["order"]


def print_brand_table(summary: list[dict], stream=sys.stdout) -> None:
    width_abbrev = max(6, max(len(e["abbrev"]) for e in summary))
    width_brand = max(5, max(len(e["brand"]) for e in summary))
    print(f"  {'order':>5}  {'abbrev':<{width_abbrev}}  {'brand':<{width_brand}}  count", file=stream)
    print(f"  {'-'*5}  {'-'*width_abbrev}  {'-'*width_brand}  -----", file=stream)
    for e in summary:
        print(
            f"  {str(e['order']):>5}  {e['abbrev']:<{width_abbrev}}  {e['brand']:<{width_brand}}  {e['count']}",
            file=stream,
        )


def select_items(
    rows: list[dict],
    brand_abbrev: Optional[str],
    skus: Optional[list[str]],
    all_items: bool = False,
) -> list[dict]:
    if all_items:
        items = list(rows)
    elif skus:
        wanted = {s.strip() for s in skus}
        items = [r for r in rows if r["sku"] in wanted]
    else:
        items = [r for r in rows if r.get("brand_abbrev") == brand_abbrev]
    def item_key(r: dict) -> tuple:
        try:
            brand_order = int(r.get("brand_order", "") or 0)
        except (ValueError, TypeError):
            brand_order = 10**9
        try:
            item_order = int(r.get("item_order", "") or 0)
        except (ValueError, TypeError):
            item_order = 10**9
        return (brand_order, item_order, r["sku"])
    items.sort(key=item_key)
    return items


def existing_file(sku: str) -> Optional[Path]:
    p = OUTPUT_DIR / f"{sku}.jpg"
    return p if p.exists() else None


def start_context(launch_persistent_context, *, headed: bool, profile_dir: Path, proxy: Optional[str]):
    """Launch CloakBrowser with a persistent profile and return a BrowserContext."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = dict(user_data_dir=str(profile_dir), headless=not headed, humanize=True)
    if proxy:
        kwargs["proxy"] = proxy
        kwargs["args"] = ["--ignore-certificate-errors"]
        kwargs["ignore_https_errors"] = True
    try:
        ctx = launch_persistent_context(**kwargs)
    except TypeError:
        kwargs.pop("humanize", None)
        ctx = launch_persistent_context(**kwargs)
    proxy_note = f", proxy={_redact_proxy(proxy)}" if proxy else ""
    print(f"[cloak] launch_persistent_context(user_data_dir={profile_dir!s}, headless={not headed}{proxy_note})")
    return ctx


def _redact_proxy(proxy: str) -> str:
    """Redact credentials in a proxy URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        u = urlparse(proxy)
        if u.username or u.password:
            netloc = f"***:***@{u.hostname}"
            if u.port:
                netloc += f":{u.port}"
            return urlunparse((u.scheme, netloc, u.path, u.params, u.query, u.fragment))
    except Exception:
        pass
    return "<set>"


def get_page(ctx):
    """Return a Page from a BrowserContext, reusing an existing page if any."""
    if hasattr(ctx, "pages") and ctx.pages:
        return ctx.pages[0]
    return ctx.new_page()


def _request_html_with_backoff(page, url: str, PlaywrightTimeoutError) -> tuple[Optional[str], Optional[str]]:
    """Fetch HTML via page.request.get with retry-on-429 backoff. Returns (body, error_reason)."""
    for attempt in range(len(RATE_LIMIT_BACKOFFS) + 1):
        try:
            resp = page.request.get(url, timeout=30_000)
        except PlaywrightTimeoutError:
            return None, "page load timeout"
        except Exception as e:
            msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
            return None, f"request error: {msg}"

        status = resp.status
        if status == 429:
            failure_kind = "rate limited (HTTP 429)"
        elif status >= 500:
            failure_kind = f"upstream HTTP {status}"
        elif not resp.ok:
            return None, f"page http {status}"
        else:
            try:
                return resp.text(), None
            except Exception as e:
                msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
                return None, f"page read error: {msg}"

        if attempt >= len(RATE_LIMIT_BACKOFFS):
            return None, failure_kind
        wait = RATE_LIMIT_BACKOFFS[attempt]
        print(f"    [{failure_kind}] sleeping {wait}s before retry {attempt + 1}/{len(RATE_LIMIT_BACKOFFS)}...")
        time.sleep(wait)
    return None, "exceeded retry budget"


def fetch_image_url(page, upc: str, PlaywrightTimeoutError) -> tuple[Optional[str], Optional[str]]:
    """Fetch the go-upc product page HTML and extract the S3 image URL."""
    body, err = _request_html_with_backoff(page, GO_UPC_SEARCH.format(upc=upc), PlaywrightTimeoutError)
    if err is not None:
        return None, err

    if NOT_FOUND_TEXT in body:
        return None, "not found on go-upc"

    m = GO_UPC_IMAGE_RE.search(body)
    if not m:
        return None, "no product image"

    return m.group(0), None


def download_image_bytes(page, image_url: str) -> tuple[Optional[bytes], Optional[str]]:
    try:
        resp = page.request.get(image_url, timeout=20_000)
    except Exception as e:
        msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        return None, f"image fetch error: {msg}"
    if not resp.ok:
        return None, f"image http {resp.status}"
    try:
        return resp.body(), None
    except Exception as e:
        msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        return None, f"image read error: {msg}"


def convert_to_jpeg(raw_bytes: bytes, Image) -> tuple[Optional[bytes], Optional[str]]:
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue(), None
    except Exception as e:
        return None, f"image decode failed: {type(e).__name__}"


def save_jpeg(sku: str, data: bytes) -> tuple[Optional[Path], Optional[str]]:
    dest = OUTPUT_DIR / f"{sku}.jpg"
    dest.write_bytes(data)
    if dest.stat().st_size < MIN_IMAGE_BYTES:
        dest.unlink()
        return None, "image too small"
    return dest, None


def _clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_tags(fragment: str | None) -> str:
    if not fragment:
        return ""
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return _clean_text(fragment)


def _tag_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for name, value in re.findall(r"([a-zA-Z_:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag):
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        attrs[name.lower()] = html.unescape(value)
    return attrs


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (key or "").lower()).strip()


def _set_field(fields: dict[str, str], key: str, value: str) -> None:
    key = _clean_text(key).rstrip(":")
    value = _clean_text(value)
    if not key or not value:
        return
    if key in fields:
        if value not in fields[key].split(" | "):
            fields[key] = f"{fields[key]} | {value}"
    else:
        fields[key] = value


def _extract_meta(body: str, *wanted: str) -> str:
    wanted_norm = {_norm_key(w) for w in wanted}
    for m in re.finditer(r"<meta\b[^>]*>", body, flags=re.IGNORECASE):
        attrs = _tag_attrs(m.group(0))
        key = attrs.get("property") or attrs.get("name") or ""
        if _norm_key(key) in wanted_norm:
            return _clean_text(attrs.get("content", ""))
    return ""


def _extract_first_tag_text(body: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", body, flags=re.IGNORECASE | re.DOTALL)
    return _strip_tags(m.group(1)) if m else ""


def _extract_detail_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}

    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) >= 2:
            key = _strip_tags(cells[0])
            value = _strip_tags(" ".join(cells[1:]))
            _set_field(fields, key, value)

    for key, value in re.findall(
        r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        _set_field(fields, _strip_tags(key), _strip_tags(value))

    for key, value in re.findall(
        r"<h[2-4]\b[^>]*>(.*?)</h[2-4]>\s*<span\b[^>]*>(.*?)</span>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        _set_field(fields, _strip_tags(key), _strip_tags(value))

    for item in re.findall(r"<li\b[^>]*>(.*?)</li>", body, flags=re.IGNORECASE | re.DOTALL):
        label = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*metadata-label[^\"']*[\"'][^>]*>(.*?)</span>",
            item,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not label:
            continue
        key = _strip_tags(label.group(1))
        value_html = item[: label.start()] + item[label.end() :]
        value = _strip_tags(value_html)
        _set_field(fields, key, value)

    return fields


def _jsonld_products(body: str) -> list[dict]:
    products: list[dict] = []

    def walk(obj) -> None:
        if isinstance(obj, dict):
            type_value = obj.get("@type")
            type_names = type_value if isinstance(type_value, list) else [type_value]
            if any(str(t).lower() == "product" for t in type_names if t is not None):
                products.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for script in SCRIPT_JSONLD_RE.findall(body):
        try:
            data = json.loads(html.unescape(script).strip())
        except json.JSONDecodeError:
            continue
        walk(data)
    return products


def _jsonld_brand(product: dict) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        return _clean_text(brand.get("name", ""))
    if isinstance(brand, list):
        names = [_jsonld_brand({"brand": item}) for item in brand]
        return " | ".join(name for name in names if name)
    return _clean_text(str(brand or ""))


def _jsonld_image(product: dict) -> str:
    image = product.get("image")
    if isinstance(image, list):
        return _clean_text(str(image[0])) if image else ""
    if isinstance(image, dict):
        return _clean_text(str(image.get("url") or image.get("contentUrl") or ""))
    return _clean_text(str(image or ""))


def _pick(fields: dict[str, str], *aliases: str) -> str:
    norm_to_value = {_norm_key(key): value for key, value in fields.items()}
    for alias in aliases:
        value = norm_to_value.get(_norm_key(alias))
        if value:
            return value
    for alias in aliases:
        wanted = _norm_key(alias)
        for key, value in norm_to_value.items():
            if wanted and re.search(rf"(^| ){re.escape(wanted)}( |$)", key):
                return value
    return ""


def parse_go_upc_product(body: str, upc: str) -> dict[str, str]:
    fields = _extract_detail_fields(body)
    products = _jsonld_products(body)
    product = products[0] if products else {}

    if product:
        _set_field(fields, "jsonld_name", str(product.get("name") or ""))
        _set_field(fields, "jsonld_brand", _jsonld_brand(product))
        _set_field(fields, "jsonld_description", str(product.get("description") or ""))
        for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "sku", "mpn", "category"):
            _set_field(fields, f"jsonld_{key}", str(product.get(key) or ""))

    h1 = _extract_first_tag_text(body, "h1")
    title = _extract_first_tag_text(body, "title")
    meta_description = _extract_meta(body, "description", "og:description")
    meta_image = _extract_meta(body, "og:image", "twitter:image")
    meta_title = _extract_meta(body, "og:title", "twitter:title")
    if title:
        _set_field(fields, "html_title", title)
    if h1:
        _set_field(fields, "html_h1", h1)
    if meta_title:
        _set_field(fields, "meta_title", meta_title)
    if meta_description:
        _set_field(fields, "meta_description", meta_description)

    image_match = GO_UPC_IMAGE_RE.search(body)
    image_url = image_match.group(0) if image_match else (_jsonld_image(product) or meta_image)

    product_name = (
        _pick(fields, "product name", "name")
        or _pick(fields, "jsonld name")
        or h1
        or meta_title.replace(" - Go-UPC.com", "").strip()
    )
    description = _pick(fields, "description") or _pick(fields, "jsonld description") or meta_description
    ingredients = _pick(fields, "ingredients")
    brand = _pick(fields, "brand") or _pick(fields, "jsonld brand")
    ean = _pick(fields, "ean", "ean 13", "gtin13", "jsonld gtin13")
    upc_value = _pick(fields, "upc", "upc a", "gtin12", "jsonld gtin12") or upc
    category = _pick(fields, "category", "jsonld category")
    department = _pick(fields, "department")
    commodity = _pick(fields, "commodity")
    manufacturer = _pick(fields, "manufacturer", "mfg", "manufacturer name", "company")
    package_quantity = _pick(
        fields,
        "package quantity",
        "package qty",
        "pack quantity",
        "quantity",
        "count",
        "number of items",
        "item package quantity",
    )
    net_weight = _pick(fields, "net weight", "weight", "item weight", "package weight")
    size = _pick(fields, "size")
    product_dimension = _pick(fields, "product dimension", "dimensions", "product dimensions")
    country_of_registration = _pick(fields, "country of registration")
    color = _pick(fields, "color")
    height = _pick(fields, "height")
    width = _pick(fields, "width")
    length = _pick(fields, "length")

    return {
        "go_upc_url": GO_UPC_SEARCH.format(upc=upc),
        "image_url": image_url,
        "product_name": product_name,
        "ean": ean,
        "upc": upc_value,
        "brand": brand,
        "description": description,
        "ingredients": ingredients,
        "package_quantity": package_quantity,
        "net_weight": net_weight,
        "size": size,
        "product_dimension": product_dimension,
        "country_of_registration": country_of_registration,
        "color": color,
        "category": category,
        "department": department,
        "commodity": commodity,
        "manufacturer": manufacturer,
        "height": height,
        "width": width,
        "length": length,
        "fields_json": json.dumps(fields, ensure_ascii=True, sort_keys=True),
    }


def metadata_key(item: dict) -> tuple[str, str]:
    return (item.get("sku", ""), item.get("upc", ""))


def load_metadata_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {
            (row.get("catalog_sku", ""), row.get("catalog_upc", ""))
            for row in csv.DictReader(f)
        }


def append_metadata_row(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_FIELDS, lineterminator="\n", extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def upsert_metadata_row(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = (row.get("catalog_sku", ""), row.get("catalog_upc", ""))
    rows: list[dict[str, str]] = []
    replaced = False
    if path.exists() and path.stat().st_size:
        with path.open(newline="", encoding="utf-8-sig") as f:
            for existing in csv.DictReader(f):
                existing_key = (existing.get("catalog_sku", ""), existing.get("catalog_upc", ""))
                if existing_key == key:
                    rows.append(row)
                    replaced = True
                else:
                    rows.append(existing)
    if not replaced:
        rows.append(row)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def write_metadata_row(path: Path, row: dict[str, str], *, replace: bool) -> None:
    if replace:
        upsert_metadata_row(path, row)
    else:
        append_metadata_row(path, row)


def build_metadata_row(item: dict, product_info: Optional[dict[str, str]], error: Optional[str]) -> dict[str, str]:
    info = product_info or {}
    return {
        "catalog_sku": item.get("sku", ""),
        "catalog_upc": item.get("upc", ""),
        "catalog_name": item.get("name", ""),
        "catalog_brand": item.get("brand", ""),
        "catalog_brand_abbrev": item.get("brand_abbrev", ""),
        "catalog_brand_order": item.get("brand_order", ""),
        "catalog_item_order": item.get("item_order", ""),
        "go_upc_url": info.get("go_upc_url") or GO_UPC_SEARCH.format(upc=item.get("upc", "")),
        "found": "yes" if product_info and not error else "no",
        "error": error or "",
        "image_url": info.get("image_url", ""),
        "product_name": info.get("product_name", ""),
        "ean": info.get("ean", ""),
        "upc": info.get("upc", ""),
        "brand": info.get("brand", ""),
        "description": info.get("description", ""),
        "ingredients": info.get("ingredients", ""),
        "package_quantity": info.get("package_quantity", ""),
        "net_weight": info.get("net_weight", ""),
        "size": info.get("size", ""),
        "product_dimension": info.get("product_dimension", ""),
        "country_of_registration": info.get("country_of_registration", ""),
        "color": info.get("color", ""),
        "category": info.get("category", ""),
        "department": info.get("department", ""),
        "commodity": info.get("commodity", ""),
        "manufacturer": info.get("manufacturer", ""),
        "height": info.get("height", ""),
        "width": info.get("width", ""),
        "length": info.get("length", ""),
        "fields_json": info.get("fields_json", ""),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------- Bright Data Web Unlocker API mode ----------------

def import_requests():
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: requests\n"
            "Install with:  python -m pip install requests"
        ) from exc
    return requests


def fetch_image_url_via_api(requests_mod, api_key: str, zone: str, upc: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch go-upc HTML through Web Unlocker API and extract S3 image URL."""
    target = GO_UPC_SEARCH.format(upc=upc)
    try:
        resp = requests_mod.post(
            WEB_UNLOCKER_ENDPOINT,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json={"zone": zone, "url": target, "format": "raw"},
            timeout=90,
        )
    except Exception as e:
        return None, f"unlocker request error: {type(e).__name__}"

    if resp.status_code == 401 or resp.status_code == 403:
        return None, f"unlocker auth rejected (HTTP {resp.status_code}) — check API key"
    if not resp.ok:
        body = resp.text[:200] if resp.text else ""
        return None, f"unlocker HTTP {resp.status_code}: {body}"

    body = resp.text
    if NOT_FOUND_TEXT in body:
        return None, "not found on go-upc"

    m = GO_UPC_IMAGE_RE.search(body)
    if not m:
        return None, "no product image"
    return m.group(0), None


def fetch_html_via_api(requests_mod, api_key: str, zone: str, upc: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch go-upc HTML through Bright Data Web Unlocker API."""
    target = GO_UPC_SEARCH.format(upc=upc)
    try:
        resp = requests_mod.post(
            WEB_UNLOCKER_ENDPOINT,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json={"zone": zone, "url": target, "format": "raw"},
            timeout=90,
        )
    except Exception as e:
        return None, f"unlocker request error: {type(e).__name__}"

    if resp.status_code == 401 or resp.status_code == 403:
        return None, f"unlocker auth rejected (HTTP {resp.status_code}) - check API key"
    if not resp.ok:
        body = resp.text[:200] if resp.text else ""
        return None, f"unlocker HTTP {resp.status_code}: {body}"

    body = resp.text
    if NOT_FOUND_TEXT in body:
        return None, "not found on go-upc"
    return body, None


def fetch_product_info_via_api(requests_mod, api_key: str, zone: str, upc: str) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Fetch and parse all useful Go-UPC product data through Bright Data."""
    body, err = fetch_html_via_api(requests_mod, api_key, zone, upc)
    if err is not None or body is None:
        return None, err
    return parse_go_upc_product(body, upc), None


def fetch_image_url_via_api(requests_mod, api_key: str, zone: str, upc: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch go-upc HTML through Web Unlocker API and extract S3 image URL."""
    product_info, err = fetch_product_info_via_api(requests_mod, api_key, zone, upc)
    if err is not None:
        return None, err
    image_url = (product_info or {}).get("image_url", "")
    if not image_url:
        return None, "no product image"
    return image_url, None


def download_image_bytes_direct(requests_mod, image_url: str) -> tuple[Optional[bytes], Optional[str]]:
    """S3-hosted product images are public, so we fetch them directly without going through Web Unlocker."""
    try:
        resp = requests_mod.get(image_url, timeout=30)
    except Exception as e:
        return None, f"image fetch error: {type(e).__name__}"
    if not resp.ok:
        return None, f"image http {resp.status_code}"
    return resp.content, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch chemical product images and Go-UPC metadata via Bright Data")
    parser.add_argument("--brand", help="Brand abbrev or full name (case-insensitive). Default: brand_order == 1.")
    parser.add_argument("--sku", action="append", dest="skus", metavar="SKU", help="Specific SKU(s) to fetch (repeatable). Ignores --brand.")
    parser.add_argument("--all", action="store_true", help="Process every Chemical row with a valid UPC.")
    parser.add_argument("--first", type=int, metavar="N", help="Process only first N items in the selected brand.")
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Inter-request delay. Defaults to 0 for Bright Data metadata-only runs, otherwise 2.0.",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Random extra delay. Defaults to 0 for Bright Data metadata-only runs, otherwise 0.8.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Re-download images that already exist.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--limit-failures", type=int, default=0, metavar="N", help="Abort after N consecutive failures (0 = off).")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help=f"Input CSV (default: {DEFAULT_CSV.name})")
    parser.add_argument("--proxy", default=None, metavar="URL", help="Proxy URL (e.g. http://user:pass@host:port). Overrides GO_UPC_PROXY env var.")
    parser.add_argument("--api-key", default=None, metavar="KEY", help="Bright Data Web Unlocker API key. Overrides BRIGHTDATA_API_KEY env var.")
    parser.add_argument("--zone", default=None, metavar="NAME", help=f"Bright Data zone name for Web Unlocker (default {DEFAULT_WEB_UNLOCKER_ZONE}).")
    parser.add_argument("--metadata", action="store_true", help="Collect Go-UPC product data into a CSV while processing items.")
    parser.add_argument("--metadata-only", action="store_true", help="Collect Go-UPC product data without downloading product images.")
    parser.add_argument("--metadata-csv", type=Path, default=DEFAULT_METADATA_CSV, help=f"Metadata output CSV (default: {DEFAULT_METADATA_CSV.name})")
    parser.add_argument("--metadata-overwrite", action="store_true", help="Fetch metadata even when the SKU/UPC already exists in the metadata CSV.")
    parser.add_argument("--list-brands", action="store_true", help="List all chemical brands and exit.")
    args = parser.parse_args()

    proxy = args.proxy or os.environ.get("GO_UPC_PROXY") or None
    api_key = args.api_key or os.environ.get("BRIGHTDATA_API_KEY") or None
    zone = args.zone or os.environ.get("BRIGHTDATA_ZONE") or DEFAULT_WEB_UNLOCKER_ZONE
    collect_metadata = args.metadata or args.metadata_only
    download_images = not args.metadata_only
    use_api = bool(api_key)

    if collect_metadata and not api_key:
        sys.exit("Metadata collection requires Bright Data mode. Set --api-key or BRIGHTDATA_API_KEY.")

    fast_metadata_mode = collect_metadata and not download_images and use_api
    effective_delay = args.delay
    if effective_delay is None:
        effective_delay = 0.0 if fast_metadata_mode else 2.0
    effective_jitter = args.jitter
    if effective_jitter is None:
        effective_jitter = 0.0 if fast_metadata_mode or effective_delay <= 0 else 0.8

    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")

    rows = chemical_rows(load_rows(args.csv))
    if not rows:
        sys.exit("No chemical items with UPCs in the CSV.")

    if args.list_brands:
        print_brand_table(brand_summary(rows))
        return 0

    if args.all:
        brand_name, brand_abbrev, brand_order = "(all Chemical rows)", None, "-"
        items = select_items(rows, None, None, all_items=True)
    elif args.skus:
        brand_name, brand_abbrev, brand_order = "(per-sku run)", None, "-"
        items = select_items(rows, None, args.skus)
        if not items:
            sys.exit(f"No matching rows for SKUs: {args.skus}")
    else:
        brand_name, brand_abbrev, brand_order = resolve_brand(rows, args.brand)
        items = select_items(rows, brand_abbrev, None)
        if not items:
            sys.exit(f"No items found for brand {brand_name} ({brand_abbrev}).")

    if args.first:
        items = items[: args.first]

    metadata_existing_keys = load_metadata_keys(args.metadata_csv) if collect_metadata else set()
    already_have = sum(1 for r in items if existing_file(r["sku"]))
    will_fetch = len(items) - (0 if args.overwrite else already_have) if download_images else 0
    metadata_already_have = sum(1 for r in items if metadata_key(r) in metadata_existing_keys)
    will_fetch_metadata = (
        len(items) - (0 if args.metadata_overwrite else metadata_already_have)
        if collect_metadata
        else 0
    )

    print(f"{'='*64}")
    print(f"Brand:           {brand_name}  ({brand_abbrev}, brand_order={brand_order})")
    print(f"Items in scope:  {len(items)}")
    if download_images:
        print(f"Already on disk: {already_have}{'  (will overwrite)' if args.overwrite else ''}")
        print(f"Images to fetch: {will_fetch}")
    if collect_metadata:
        print(f"Metadata CSV:    {args.metadata_csv}")
        print(f"Metadata rows:   {metadata_already_have} already collected{'  (will overwrite)' if args.metadata_overwrite else ''}")
        print(f"Metadata fetch:  {will_fetch_metadata}")
    print(f"Output dir:      {OUTPUT_DIR}")
    print(f"Delay:           {effective_delay}s + jitter 0-{effective_jitter}s   |   Headed: {args.headed}   |   Limit-failures: {args.limit_failures or 'off'}")
    if use_api:
        print(f"Mode:            Bright Data Web Unlocker API (zone={zone}, key=***)")
    else:
        print(f"Mode:            CloakBrowser  |  Proxy: {_redact_proxy(proxy) if proxy else '(none — using direct connection)'}")
    print(f"{'='*64}\n")

    if will_fetch == 0 and will_fetch_metadata == 0 and not args.overwrite:
        print("Nothing to do.")
        return 0

    Image = import_pillow() if download_images else None
    success: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    metadata_success: list[str] = []
    metadata_skipped: list[str] = []
    metadata_failed: list[tuple[str, str]] = []
    consecutive_failures = 0
    started = time.time()

    if use_api:
        requests_mod = import_requests()

        def get_product_info(upc):
            return fetch_product_info_via_api(requests_mod, api_key, zone, upc)

        def get_image_url(upc):
            return fetch_image_url_via_api(requests_mod, api_key, zone, upc)

        def get_image_bytes(image_url):
            return download_image_bytes_direct(requests_mod, image_url)

        ctx = None  # API mode does not need a browser context
    else:
        launch_persistent_context, PlaywrightTimeoutError = import_cloakbrowser()
        ctx = start_context(launch_persistent_context, headed=args.headed, profile_dir=PROFILE_DIR, proxy=proxy)
        page = get_page(ctx)
        try:
            page.set_default_timeout(15_000)
        except Exception:
            pass

        def get_image_url(upc):
            return fetch_image_url(page, upc, PlaywrightTimeoutError)

        def get_image_bytes(image_url):
            return download_image_bytes(page, image_url)

    try:
        for i, item in enumerate(items, 1):
            sku = item["sku"]
            upc = item["upc"]
            label = f"[{i}/{len(items)}] {sku} ({upc})"
            key = metadata_key(item)
            need_metadata = collect_metadata and (args.metadata_overwrite or key not in metadata_existing_keys)
            need_image = download_images and (args.overwrite or not existing_file(sku))

            if download_images and not need_image:
                skipped.append(sku)

            if collect_metadata and not need_metadata:
                metadata_skipped.append(sku)

            if not need_image and not need_metadata:
                print(f"{label} -> skip (image and metadata already exist)")
                continue

            print(f"{label} -> go-upc.com...")
            product_info = None
            err = None
            image_url = None

            if use_api:
                product_info, err = get_product_info(upc)
                if need_metadata:
                    write_metadata_row(
                        args.metadata_csv,
                        build_metadata_row(item, product_info, err),
                        replace=args.metadata_overwrite,
                    )
                    metadata_existing_keys.add(key)
                    if err is None:
                        metadata_success.append(sku)
                    else:
                        metadata_failed.append((sku, err))
                image_url = (product_info or {}).get("image_url")
            else:
                image_url, err = get_image_url(upc)

            if err is not None:
                print(f"    [miss] {err}")
                if need_image:
                    failed.append((sku, err))
                consecutive_failures += 1
                if args.limit_failures and consecutive_failures >= args.limit_failures:
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(effective_delay, effective_jitter)
                continue

            if not need_image:
                print("    [ok]   metadata saved" if need_metadata else "    [ok]   page checked")
                consecutive_failures = 0
                if i < len(items):
                    _sleep_with_jitter(effective_delay, effective_jitter)
                continue

            if not image_url:
                err = "no product image"
                print(f"    [miss] {err}")
                failed.append((sku, err))
                consecutive_failures += 1
                if args.limit_failures and consecutive_failures >= args.limit_failures:
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(effective_delay, effective_jitter)
                continue

            raw, err = get_image_bytes(image_url)
            if err is not None or raw is None:
                print(f"    [miss] {err}")
                failed.append((sku, err or "download failed"))
                consecutive_failures += 1
                if args.limit_failures and consecutive_failures >= args.limit_failures:
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(effective_delay, effective_jitter)
                continue

            jpeg_bytes, err = convert_to_jpeg(raw, Image)
            if err is not None or jpeg_bytes is None:
                print(f"    [miss] {err}")
                failed.append((sku, err or "convert failed"))
                consecutive_failures += 1
                _sleep_with_jitter(effective_delay, effective_jitter)
                continue

            dest, err = save_jpeg(sku, jpeg_bytes)
            if err is not None or dest is None:
                print(f"    [miss] {err}")
                failed.append((sku, err or "save failed"))
                consecutive_failures += 1
                _sleep_with_jitter(effective_delay, effective_jitter)
                continue

            size_kb = dest.stat().st_size // 1024
            meta_note = " + metadata" if need_metadata else ""
            print(f"    [ok]   saved {dest.name}  ({size_kb} KB){meta_note}  <-  {image_url}")
            success.append(sku)
            consecutive_failures = 0

            if i < len(items):
                _sleep_with_jitter(effective_delay, effective_jitter)
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    elapsed = time.time() - started
    print(f"\n{'='*64}")
    print(f"Done in {elapsed:.1f}s.  success={len(success)}  skipped={len(skipped)}  failed={len(failed)}")
    if collect_metadata:
        print(
            f"Metadata: saved={len(metadata_success)}  skipped={len(metadata_skipped)}  "
            f"failed={len(metadata_failed)}  csv={args.metadata_csv}"
        )
    if failed:
        reasons = Counter(reason for _, reason in failed)
        print("\nFailure tally:")
        for reason, count in reasons.most_common():
            print(f"  {count:>4}  {reason}")
        print("\nFailed items:")
        for sku, reason in failed:
            print(f"  {sku}  -  {reason}")
    if metadata_failed:
        reasons = Counter(reason for _, reason in metadata_failed)
        print("\nMetadata failure tally:")
        for reason, count in reasons.most_common():
            print(f"  {count:>4}  {reason}")

    return 0 if not failed else 1


def _sleep_with_jitter(base: float, jitter: float) -> None:
    base = max(0.0, base)
    jitter = max(0.0, jitter)
    if base == 0 and jitter == 0:
        return
    time.sleep(base + random.uniform(0, jitter))


if __name__ == "__main__":
    raise SystemExit(main())
