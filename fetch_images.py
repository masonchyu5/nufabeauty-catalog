"""
Fetch general-catalog product images from go-upc.com through Bright Data.

Reads UPCs from items_general_firstextraction.csv, looks each one up on
go-upc.com, and saves the product image as images/general_products_original/<SKU>.jpg.

Both the input CSV and the output folder are hard-coded: this script will not
read any other CSV and will not write images anywhere else.

Bright Data is REQUIRED — the script refuses to run without credentials. Supply
either a Web Unlocker API key or a proxy URL:

  export BRIGHTDATA_API_KEY=...            # Web Unlocker API (zone: BRIGHTDATA_ZONE)
  export BRIGHTDATA_PROXY='http://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:33335'

Usage:
  python fetch_images.py                             # every row with a UPC
  python fetch_images.py --list-sections             # show sections + counts
  python fetch_images.py --section BARBER            # one section only
  python fetch_images.py --first 3                   # smoke test
  python fetch_images.py --sku 3ANN5909              # single SKU (ignores --section)
  python fetch_images.py --overwrite                 # re-download existing files
  python fetch_images.py --browser --headed          # route CloakBrowser via the proxy
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
# Locked down on purpose: the only CSV this script may read, and the only
# directory it may write images into.
CSV_PATH = SCRIPT_DIR / "items_general_firstextraction.csv"
OUTPUT_DIR = SCRIPT_DIR / "images" / "general_products_original"
PROFILE_DIR = SCRIPT_DIR / ".cloak_profile"

GO_UPC_SEARCH = "https://go-upc.com/search?q={upc}"
NOT_FOUND_TEXT = "Sorry, we were not able to find a product for"
GO_UPC_IMAGE_RE = re.compile(r"https://go-upc\.s3\.amazonaws\.com/images/\d+\.[a-zA-Z]+")
SAFE_SKU_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MIN_IMAGE_BYTES = 5_000
JPEG_QUALITY = 90
RATE_LIMIT_BACKOFFS = (60, 180, 600)  # seconds to wait on consecutive 429s before giving up on an item
WEB_UNLOCKER_ENDPOINT = "https://api.brightdata.com/request"
DEFAULT_WEB_UNLOCKER_ZONE = "nufaminer"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


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
            "Install with:  python -m pip install pillow"
        ) from exc
    return Image


def import_requests():
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: requests\n"
            "Install with:  python -m pip install requests"
        ) from exc
    return requests


# ---------------- CSV ----------------

def load_rows() -> list[dict]:
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def usable_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split rows into (has UPC, missing UPC). Rows without a SKU are unusable either way."""
    keep, no_upc = [], []
    for r in rows:
        if not r.get("sku"):
            continue
        (keep if r.get("upc") else no_upc).append(r)
    return keep, no_upc


def section_summary(rows: list[dict]) -> list[dict]:
    by_section: dict[str, dict] = {}
    for r in rows:
        key = r.get("section") or "?"
        entry = by_section.setdefault(
            key, {"section": key, "order": r.get("section_order", ""), "count": 0}
        )
        entry["count"] += 1

    def order_key(e: dict) -> tuple:
        try:
            return (0, int(e["order"]))
        except (ValueError, TypeError):
            return (1, e["section"].lower())

    return sorted(by_section.values(), key=order_key)


def print_section_table(summary: list[dict], stream=sys.stdout) -> None:
    width = max(7, max(len(e["section"]) for e in summary))
    print(f"  {'order':>5}  {'section':<{width}}  count", file=stream)
    print(f"  {'-'*5}  {'-'*width}  -----", file=stream)
    for e in summary:
        print(f"  {str(e['order']):>5}  {e['section']:<{width}}  {e['count']}", file=stream)


def resolve_section(rows: list[dict], section_arg: str) -> str:
    summary = section_summary(rows)
    needle = section_arg.strip().lower()
    matches = [e for e in summary if e["section"].lower() == needle]
    if not matches:
        matches = [e for e in summary if needle in e["section"].lower()]
    if len(matches) != 1:
        problem = "matches no section" if not matches else "is ambiguous"
        print(f"{section_arg!r} {problem}.\n\nAvailable sections:", file=sys.stderr)
        print_section_table(summary, stream=sys.stderr)
        sys.exit(2)
    return matches[0]["section"]


def select_items(rows: list[dict], section: Optional[str], skus: Optional[list[str]]) -> list[dict]:
    if skus:
        wanted = {s.strip() for s in skus}
        items = [r for r in rows if r["sku"] in wanted]
    elif section:
        items = [r for r in rows if r.get("section") == section]
    else:
        items = list(rows)

    def item_key(r: dict) -> tuple:
        def num(field: str) -> tuple:
            try:
                return (0, int(r.get(field, "") or 0))
            except (ValueError, TypeError):
                return (1, 0)
        return (num("section_order"), num("group_order"), num("item_order"), r["sku"])

    items.sort(key=item_key)
    return items


# ---------------- Output files ----------------

def sku_path(sku: str) -> Optional[Path]:
    """Destination for a SKU, or None if the SKU is not a safe bare filename."""
    if not SAFE_SKU_RE.match(sku):
        return None
    dest = (OUTPUT_DIR / f"{sku}.jpg").resolve()
    if dest.parent != OUTPUT_DIR.resolve():
        return None
    return dest


def existing_file(sku: str) -> Optional[Path]:
    dest = sku_path(sku)
    return dest if dest and dest.exists() else None


def save_jpeg(sku: str, data: bytes) -> tuple[Optional[Path], Optional[str]]:
    dest = sku_path(sku)
    if dest is None:
        return None, "unsafe SKU for a filename"
    dest.write_bytes(data)
    if dest.stat().st_size < MIN_IMAGE_BYTES:
        dest.unlink()
        return None, "image too small"
    return dest, None


def convert_to_jpeg(raw_bytes: bytes, Image) -> tuple[Optional[bytes], Optional[str]]:
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue(), None
    except Exception as e:
        return None, f"image decode failed: {type(e).__name__}"


# ---------------- Shared fetch plumbing ----------------

def _short(e: Exception) -> str:
    msg = str(e).strip()
    return msg.splitlines()[0][:200] if msg else type(e).__name__


def _html_with_backoff(do_request: Callable[[], tuple[Optional[int], Optional[str], Optional[str]]]):
    """Run do_request() -> (status, body, transport_error) with retry-on-429/5xx backoff."""
    for attempt in range(len(RATE_LIMIT_BACKOFFS) + 1):
        status, body, err = do_request()
        if err is not None:
            return None, err
        if status == 429:
            failure_kind = "rate limited (HTTP 429)"
        elif status is not None and status >= 500:
            failure_kind = f"upstream HTTP {status}"
        elif status is not None and status >= 400:
            return None, f"page http {status}"
        else:
            return body, None

        if attempt >= len(RATE_LIMIT_BACKOFFS):
            return None, failure_kind
        wait = RATE_LIMIT_BACKOFFS[attempt]
        print(f"    [{failure_kind}] sleeping {wait}s before retry {attempt + 1}/{len(RATE_LIMIT_BACKOFFS)}...")
        time.sleep(wait)
    return None, "exceeded retry budget"


def extract_image_url(body: str) -> tuple[Optional[str], Optional[str]]:
    if NOT_FOUND_TEXT in body:
        return None, "not found on go-upc"
    m = GO_UPC_IMAGE_RE.search(body)
    if not m:
        return None, "no product image"
    return m.group(0), None


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


# ---------------- Transport: Bright Data Web Unlocker API ----------------

def unlocker_transport(requests_mod, api_key: str, zone: str):
    def get_image_url(upc: str):
        def do_request():
            try:
                resp = requests_mod.post(
                    WEB_UNLOCKER_ENDPOINT,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                    json={"zone": zone, "url": GO_UPC_SEARCH.format(upc=upc), "format": "raw"},
                    timeout=90,
                )
            except Exception as e:
                return None, None, f"unlocker request error: {_short(e)}"
            if resp.status_code in (401, 403):
                return None, None, f"unlocker auth rejected (HTTP {resp.status_code}) — check API key/zone"
            return resp.status_code, resp.text, None

        body, err = _html_with_backoff(do_request)
        if err is not None:
            return None, err
        return extract_image_url(body)

    return get_image_url


# ---------------- Transport: Bright Data proxy (plain requests) ----------------

def proxy_transport(requests_mod, proxy: str):
    proxies = {"http": proxy, "https": proxy}
    # Bright Data's super proxy terminates TLS with its own CA; skip verification
    # rather than requiring the user to install that cert.
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    def get_image_url(upc: str):
        def do_request():
            try:
                resp = requests_mod.get(
                    GO_UPC_SEARCH.format(upc=upc),
                    proxies=proxies,
                    headers=BROWSER_HEADERS,
                    timeout=90,
                    verify=False,
                )
            except Exception as e:
                return None, None, f"proxy request error: {_short(e)}"
            return resp.status_code, resp.text, None

        body, err = _html_with_backoff(do_request)
        if err is not None:
            return None, err
        return extract_image_url(body)

    return get_image_url


def download_image_bytes_direct(requests_mod, image_url: str) -> tuple[Optional[bytes], Optional[str]]:
    """S3-hosted product images are public, so we fetch them directly instead of burning proxy bandwidth."""
    try:
        resp = requests_mod.get(image_url, headers=BROWSER_HEADERS, timeout=30)
    except Exception as e:
        return None, f"image fetch error: {_short(e)}"
    if not resp.ok:
        return None, f"image http {resp.status_code}"
    return resp.content, None


# ---------------- Transport: CloakBrowser through the Bright Data proxy ----------------

def start_context(launch_persistent_context, *, headed: bool, profile_dir: Path, proxy: str):
    """Launch CloakBrowser with a persistent profile, routed through the Bright Data proxy."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = dict(
        user_data_dir=str(profile_dir),
        headless=not headed,
        humanize=True,
        proxy=proxy,
        args=["--ignore-certificate-errors"],
        ignore_https_errors=True,
    )
    try:
        ctx = launch_persistent_context(**kwargs)
    except TypeError:
        kwargs.pop("humanize", None)
        ctx = launch_persistent_context(**kwargs)
    print(
        f"[cloak] launch_persistent_context(user_data_dir={profile_dir!s}, "
        f"headless={not headed}, proxy={_redact_proxy(proxy)})"
    )
    return ctx


def get_page(ctx):
    """Return a Page from a BrowserContext, reusing an existing page if any."""
    if hasattr(ctx, "pages") and ctx.pages:
        return ctx.pages[0]
    return ctx.new_page()


def browser_transport(page, PlaywrightTimeoutError):
    def get_image_url(upc: str):
        def do_request():
            try:
                resp = page.request.get(GO_UPC_SEARCH.format(upc=upc), timeout=30_000)
            except PlaywrightTimeoutError:
                return None, None, "page load timeout"
            except Exception as e:
                return None, None, f"request error: {_short(e)}"
            try:
                return resp.status, resp.text(), None
            except Exception as e:
                return None, None, f"page read error: {_short(e)}"

        body, err = _html_with_backoff(do_request)
        if err is not None:
            return None, err
        return extract_image_url(body)

    def get_image_bytes(image_url: str):
        try:
            resp = page.request.get(image_url, timeout=20_000)
        except Exception as e:
            return None, f"image fetch error: {_short(e)}"
        if not resp.ok:
            return None, f"image http {resp.status}"
        try:
            return resp.body(), None
        except Exception as e:
            return None, f"image read error: {_short(e)}"

    return get_image_url, get_image_bytes


# ---------------- Main ----------------

def _sleep_with_jitter(base: float) -> None:
    time.sleep(max(0.0, base) + random.uniform(0, 0.8))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch general-catalog product images from go-upc.com through Bright Data."
    )
    parser.add_argument("--section", help="Section name (case-insensitive, substring OK). Default: all sections.")
    parser.add_argument("--sku", action="append", dest="skus", metavar="SKU", help="Specific SKU(s) to fetch (repeatable). Ignores --section.")
    parser.add_argument("--first", type=int, metavar="N", help="Process only the first N items in scope.")
    parser.add_argument("--delay", type=float, default=2.0, metavar="SECONDS", help="Base inter-request delay (default 2.0). Jittered +0-0.8s.")
    parser.add_argument("--overwrite", action="store_true", help="Re-download images that already exist.")
    parser.add_argument("--browser", action="store_true", help="Use CloakBrowser through the proxy instead of plain requests.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (implies --browser).")
    parser.add_argument("--limit-failures", type=int, default=0, metavar="N", help="Abort after N consecutive failures (0 = off).")
    parser.add_argument("--proxy", default=None, metavar="URL", help="Bright Data proxy URL. Overrides BRIGHTDATA_PROXY / GO_UPC_PROXY.")
    parser.add_argument("--api-key", default=None, metavar="KEY", help="Bright Data Web Unlocker API key. Overrides BRIGHTDATA_API_KEY.")
    parser.add_argument("--zone", default=None, metavar="NAME", help=f"Bright Data Web Unlocker zone (default {DEFAULT_WEB_UNLOCKER_ZONE}).")
    parser.add_argument("--list-sections", action="store_true", help="List sections with UPC counts and exit.")
    args = parser.parse_args()

    if args.headed:
        args.browser = True

    proxy = args.proxy or os.environ.get("BRIGHTDATA_PROXY") or os.environ.get("GO_UPC_PROXY") or None
    api_key = args.api_key or os.environ.get("BRIGHTDATA_API_KEY") or None
    zone = args.zone or os.environ.get("BRIGHTDATA_ZONE") or DEFAULT_WEB_UNLOCKER_ZONE

    if not CSV_PATH.exists():
        sys.exit(f"CSV not found: {CSV_PATH}")

    rows, missing_upc = usable_rows(load_rows())
    if not rows:
        sys.exit(f"No rows with both a SKU and a UPC in {CSV_PATH.name}.")

    if args.list_sections:
        print_section_table(section_summary(rows))
        return 0

    # Bright Data is mandatory for the go-upc lookups.
    if args.browser and not proxy:
        sys.exit(
            "--browser needs a Bright Data proxy URL.\n"
            "  export BRIGHTDATA_PROXY='http://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:33335'"
        )
    if not api_key and not proxy:
        sys.exit(
            "Bright Data credentials are required — go-upc.com is never queried directly.\n"
            "Set one of:\n"
            "  export BRIGHTDATA_API_KEY=<key>     # Web Unlocker API (zone via BRIGHTDATA_ZONE)\n"
            "  export BRIGHTDATA_PROXY='http://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:33335'\n"
            "...or pass --api-key / --proxy."
        )

    if args.skus:
        scope_label = "(per-sku run)"
        items = select_items(rows, None, args.skus)
        if not items:
            sys.exit(f"No matching rows with a UPC for SKUs: {args.skus}")
    elif args.section:
        section = resolve_section(rows, args.section)
        scope_label = section
        items = select_items(rows, section, None)
    else:
        scope_label = "ALL sections"
        items = select_items(rows, None, None)

    if args.first:
        items = items[: args.first]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    already_have = sum(1 for r in items if existing_file(r["sku"]))
    will_fetch = len(items) - (0 if args.overwrite else already_have)

    # --browser wins over the API key when both are available, since it needs the proxy.
    use_browser = args.browser
    use_api = bool(api_key) and not use_browser

    print(f"{'='*64}")
    print(f"Scope:           {scope_label}")
    print(f"Input CSV:       {CSV_PATH}")
    print(f"Output dir:      {OUTPUT_DIR}")
    print(f"Items in scope:  {len(items)}" + (f"   ({len(missing_upc)} CSV rows skipped: no UPC)" if missing_upc else ""))
    print(f"Already on disk: {already_have}{'  (will overwrite)' if args.overwrite else ''}")
    print(f"To fetch now:    {will_fetch}")
    print(f"Delay:           {args.delay}s + jitter   |   Limit-failures: {args.limit_failures or 'off'}")
    if use_api:
        print(f"Mode:            Bright Data Web Unlocker API (zone={zone}, key=***)")
    elif use_browser:
        print(f"Mode:            CloakBrowser via Bright Data proxy {_redact_proxy(proxy)}   |   Headed: {args.headed}")
    else:
        print(f"Mode:            requests via Bright Data proxy {_redact_proxy(proxy)}")
    print(f"{'='*64}\n")

    if will_fetch == 0 and not args.overwrite:
        print("Nothing to do (all items already downloaded).")
        return 0

    Image = import_pillow()
    requests_mod = import_requests()
    ctx = None

    if use_api:
        get_image_url = unlocker_transport(requests_mod, api_key, zone)
        get_image_bytes = lambda url: download_image_bytes_direct(requests_mod, url)
    elif use_browser:
        launch_persistent_context, PlaywrightTimeoutError = import_cloakbrowser()
        ctx = start_context(launch_persistent_context, headed=args.headed, profile_dir=PROFILE_DIR, proxy=proxy)
        page = get_page(ctx)
        try:
            page.set_default_timeout(15_000)
        except Exception:
            pass
        get_image_url, get_image_bytes = browser_transport(page, PlaywrightTimeoutError)
    else:
        get_image_url = proxy_transport(requests_mod, proxy)
        get_image_bytes = lambda url: download_image_bytes_direct(requests_mod, url)

    success: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    consecutive_failures = 0
    started = time.time()

    try:
        for i, item in enumerate(items, 1):
            sku = item["sku"]
            upc = item["upc"]
            label = f"[{i}/{len(items)}] {sku} ({upc})"

            if sku_path(sku) is None:
                print(f"{label} -> [miss] unsafe SKU for a filename")
                failed.append((sku, "unsafe SKU for a filename"))
                continue

            if not args.overwrite and existing_file(sku):
                print(f"{label} -> skip (already exists)")
                skipped.append(sku)
                continue

            print(f"{label} -> go-upc.com...")

            def fail(reason: str) -> bool:
                """Record a failure; return True if the run should abort."""
                print(f"    [miss] {reason}")
                failed.append((sku, reason))
                return bool(args.limit_failures) and consecutive_failures >= args.limit_failures

            image_url, err = get_image_url(upc)
            if err is not None:
                consecutive_failures += 1
                if fail(err):
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(args.delay)
                continue

            raw, err = get_image_bytes(image_url)
            if err is not None or raw is None:
                consecutive_failures += 1
                if fail(err or "download failed"):
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(args.delay)
                continue

            jpeg_bytes, err = convert_to_jpeg(raw, Image)
            if err is not None or jpeg_bytes is None:
                consecutive_failures += 1
                if fail(err or "convert failed"):
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(args.delay)
                continue

            dest, err = save_jpeg(sku, jpeg_bytes)
            if err is not None or dest is None:
                consecutive_failures += 1
                if fail(err or "save failed"):
                    print(f"\nAborting: {consecutive_failures} consecutive failures.")
                    break
                _sleep_with_jitter(args.delay)
                continue

            size_kb = dest.stat().st_size // 1024
            print(f"    [ok]   saved {dest.name}  ({size_kb} KB)  <-  {image_url}")
            success.append(sku)
            consecutive_failures = 0

            if i < len(items):
                _sleep_with_jitter(args.delay)
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    elapsed = time.time() - started
    print(f"\n{'='*64}")
    print(f"Done in {elapsed:.1f}s.  success={len(success)}  skipped={len(skipped)}  failed={len(failed)}")
    if missing_upc:
        print(f"Rows with no UPC in the CSV (never attempted): {len(missing_upc)}  "
              f"[{', '.join(r['sku'] for r in missing_upc[:10])}{'...' if len(missing_upc) > 10 else ''}]")
    if failed:
        reasons = Counter(reason for _, reason in failed)
        print("\nFailure tally:")
        for reason, count in reasons.most_common():
            print(f"  {count:>4}  {reason}")
        print("\nFailed items:")
        for sku, reason in failed:
            print(f"  {sku}  -  {reason}")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
