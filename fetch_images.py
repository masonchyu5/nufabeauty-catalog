"""
Fetch general-catalog product images from go-upc.com through Bright Data.

Reads UPCs from items_general_firstextraction.csv, looks each one up on
go-upc.com, and saves the product image as images/general_products_original/<SKU>.webp.

Images are stored as lossless WebP at their original pixel dimensions, so
nothing is resized and no additional lossy compression is applied.

Both the input CSV and the output folder are hard-coded: this script will not
read any other CSV and will not write images anywhere else.

Bright Data is REQUIRED — the script refuses to run without credentials. Supply
either a Web Unlocker API key or a proxy URL:

  export BRIGHTDATA_API_KEY=...            # Web Unlocker API (zone: BRIGHTDATA_ZONE)
  export BRIGHTDATA_PROXY='http://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:33335'

If neither is set and stdin is a terminal, the script prompts for one. That
input is hidden, held only in memory, and never written to disk or shell
history. Non-interactive runs still fail fast instead of hanging on a prompt.

Usage:
  python fetch_images.py                             # every row with a UPC
  python fetch_images.py --list-sections             # show sections + counts
  python fetch_images.py --section BARBER            # one section only
  python fetch_images.py --first 3                   # smoke test
  python fetch_images.py --sku 3ANN5909              # single SKU (ignores --section)
  python fetch_images.py --overwrite                 # re-download existing files
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
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

GO_UPC_SEARCH = "https://go-upc.com/search?q={upc}"
NOT_FOUND_TEXT = "Sorry, we were not able to find a product for"
GO_UPC_IMAGE_RE = re.compile(r"https://go-upc\.s3\.amazonaws\.com/images/\d+\.[a-zA-Z]+")
SAFE_SKU_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MIN_IMAGE_BYTES = 5_000
WEBP_METHOD = 6  # 0-6; 6 = slowest encode, smallest lossless file
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


def import_pillow():
    try:
        from PIL import Image, features
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: pillow\n"
            "Install with:  python -m pip install pillow"
        ) from exc
    if not features.check("webp"):
        raise SystemExit(
            "This Pillow build has no WebP support, so images cannot be encoded.\n"
            "Reinstall with:  python -m pip install --force-reinstall pillow"
        )
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
    dest = (OUTPUT_DIR / f"{sku}.webp").resolve()
    if dest.parent != OUTPUT_DIR.resolve():
        return None
    return dest


def existing_file(sku: str) -> Optional[Path]:
    dest = sku_path(sku)
    return dest if dest and dest.exists() else None


def save_webp(sku: str, data: bytes) -> tuple[Optional[Path], Optional[str]]:
    dest = sku_path(sku)
    if dest is None:
        return None, "unsafe SKU for a filename"
    dest.write_bytes(data)
    if dest.stat().st_size < MIN_IMAGE_BYTES:
        dest.unlink()
        return None, "image too small"
    return dest, None


def is_webp(raw_bytes: bytes) -> bool:
    return len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP"


def convert_to_webp(raw_bytes: bytes, Image) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Re-encode to lossless WebP at the original dimensions.

    Returns (webp_bytes, note, error). Source pixels are never resampled and
    never lossily recompressed, so the saved image matches the original exactly.
    A source that is already WebP is passed through untouched.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        return None, None, f"image decode failed: {type(e).__name__}"

    size = f"{img.width}x{img.height}"

    if is_webp(raw_bytes):
        return raw_bytes, f"{size} webp passthrough", None

    src_format = (img.format or "?").lower()

    # WebP handles RGB/RGBA/L directly; palette and other modes need promoting,
    # to RGBA where transparency would otherwise be dropped.
    if img.mode in ("P", "PA", "LA"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

    try:
        buf = io.BytesIO()
        img.save(buf, format="WEBP", lossless=True, quality=100, method=WEBP_METHOD)
        return buf.getvalue(), f"{size} lossless from {src_format}", None
    except Exception as e:
        return None, None, f"webp encode failed: {type(e).__name__}"


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


DEBUG_DUMP_DIR: Optional[Path] = None


def _dump_body(upc: str, body: str) -> None:
    """Save a page we failed to parse, so the real cause is inspectable."""
    if DEBUG_DUMP_DIR is None:
        return
    try:
        DEBUG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        dest = DEBUG_DUMP_DIR / f"{upc}.html"
        dest.write_text(body or "", encoding="utf-8", errors="replace")
        print(f"    [debug] wrote {dest}  ({len(body or '')} chars)")
    except Exception as e:
        print(f"    [debug] could not dump page: {_short(e)}")


# Signatures for pages that came back 200 but are not the product page we
# wanted. Kept narrow so ordinary go-upc markup never trips them.
CHALLENGE_MARKERS = (
    (re.compile(r"cf-browser-verification|cf_chl_|Checking your browser|Just a moment", re.I), "Cloudflare challenge"),
    (re.compile(r"g-recaptcha|hcaptcha|solve the captcha|are you a robot", re.I), "CAPTCHA wall"),
    (re.compile(r"access denied|you have been blocked|request blocked|unusual traffic", re.I), "block page"),
    (re.compile(r"brd_error|proxy_error|bright ?data|luminati", re.I), "Bright Data error page"),
)


def _snippet(body: str, limit: int = 160) -> str:
    """Readable text from an HTML page, for putting inside an error message."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", body or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:limit] or "(no readable text)"


def _page_kind(body: str) -> Optional[str]:
    """Detect a page that is not go-upc's own HTML. None means it looks genuine."""
    if not body or not body.strip():
        return "empty response body"
    if body.lstrip()[:1] in "{[":
        return "JSON response instead of HTML"
    for rx, label in CHALLENGE_MARKERS:
        if rx.search(body):
            return label
    if "go-upc" not in body.lower():
        return "not a go-upc page"
    return None


def classify_body(body: str) -> str:
    """Explain why a 200 response was not a usable product page."""
    return _page_kind(body) or "go-upc page with no product image"


def describe_body(body: str) -> str:
    """Neutral description for progress output, where the page may be fine."""
    return _page_kind(body) or "go-upc HTML"


def extract_image_url(body: str, upc: str = "") -> tuple[Optional[str], Optional[str]]:
    if NOT_FOUND_TEXT in body:
        return None, "not found on go-upc"

    m = GO_UPC_IMAGE_RE.search(body)
    if m:
        return m.group(0), None

    # Say what actually came back — "no product image" alone hides whether this
    # was a real miss, a challenge page, or a proxy error.
    _dump_body(upc, body)
    return None, f"{classify_body(body)} [{len(body)} chars] :: {_snippet(body)}"


def prompt_for_credential() -> tuple[Optional[str], Optional[str]]:
    """Ask for a Bright Data credential on an interactive terminal.

    Returns (proxy, api_key). Input is read without echo because both forms
    embed a secret, and it is only held in memory — never written to disk and
    never echoed back, so it stays out of your shell history too. On a
    non-interactive stdin this returns (None, None) so scripted runs still fail
    fast instead of hanging on a prompt.
    """
    if not sys.stdin.isatty():
        return None, None

    print("No Bright Data credential found in the environment.")
    print("  Paste a proxy URL   (http://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:33335)")
    print("  or a Web Unlocker API key.")
    print("Input is hidden and is not saved anywhere. Press Enter alone to abort.")
    try:
        value = getpass.getpass("Bright Data credential: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None, None

    if not value:
        return None, None
    if value.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
        return value, None
    return None, value


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


# ---------------- Transports ----------------
#
# Each transport is reduced to one primitive: fetch_html(url) -> (status, body,
# transport_error). Everything above it (backoff, parsing, --check) is shared,
# so a connectivity check exercises exactly the code a real run uses.

def _unwrap_unlocker_body(text: str) -> str:
    """Web Unlocker honours format:raw, but some zones still answer with a JSON
    envelope. Pull the HTML out of it rather than failing to parse the wrapper."""
    if not text or text.lstrip()[:1] != "{":
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    for key in ("body", "html", "content", "data", "text"):
        value = data.get(key)
        if isinstance(value, str) and len(value) > 200:
            return value
    return text


def unlocker_fetcher(requests_mod, api_key: str, zone: str):
    def fetch_html(url: str):
        try:
            resp = requests_mod.post(
                WEB_UNLOCKER_ENDPOINT,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                json={"zone": zone, "url": url, "format": "raw"},
                timeout=90,
            )
        except Exception as e:
            return None, None, f"unlocker request error: {_short(e)}"
        if resp.status_code in (401, 403):
            return None, None, f"unlocker auth rejected (HTTP {resp.status_code}) — check API key/zone"
        return resp.status_code, _unwrap_unlocker_body(resp.text), None

    return fetch_html


def proxy_fetcher(requests_mod, proxy: str):
    proxies = {"http": proxy, "https": proxy}
    # Bright Data's super proxy terminates TLS with its own CA; skip verification
    # rather than requiring the user to install that cert.
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    def fetch_html(url: str):
        try:
            resp = requests_mod.get(
                url, proxies=proxies, headers=BROWSER_HEADERS, timeout=90, verify=False
            )
        except Exception as e:
            return None, None, f"proxy request error: {_short(e)}"
        return resp.status_code, resp.text, None

    return fetch_html


def direct_fetcher(requests_mod):
    """Query go-upc straight from this machine. Opt-in via --direct."""
    def fetch_html(url: str):
        try:
            resp = requests_mod.get(url, headers=BROWSER_HEADERS, timeout=60)
        except Exception as e:
            return None, None, f"direct request error: {_short(e)}"
        return resp.status_code, resp.text, None

    return fetch_html


def make_fetcher(requests_mod, *, api_key, zone, proxy, direct) -> tuple[str, Callable]:
    """Pick a transport and return (label, fetch_html)."""
    if direct:
        return "DIRECT — no proxy, requests come from this machine's IP", direct_fetcher(requests_mod)
    if api_key:
        return f"Bright Data Web Unlocker API (zone={zone}, key=***)", unlocker_fetcher(requests_mod, api_key, zone)
    return f"requests via Bright Data proxy {_redact_proxy(proxy)}", proxy_fetcher(requests_mod, proxy)


def image_url_for_upc(fetch_html, upc: str) -> tuple[Optional[str], Optional[str]]:
    """Look one UPC up on go-upc through the given transport."""
    body, err = _html_with_backoff(lambda: fetch_html(GO_UPC_SEARCH.format(upc=upc)))
    if err is not None:
        return None, err
    return extract_image_url(body, upc)


CHECK_UPC = "705372059099"  # 3ANN5909, confirmed to have an image on go-upc


def run_check(requests_mod, Image, label: str, fetch_html: Callable) -> int:
    """Probe the configured transport end to end and say where it breaks.

    Four stages, each isolating one link: can we reach go-upc at all, does a
    search page come back, does it contain a product image, and can that image
    be downloaded and decoded.
    """
    print(f"{'='*64}")
    print("Bright Data connectivity check")
    print(f"Transport:  {label}")
    print(f"Test UPC:   {CHECK_UPC}  (known to have an image)")
    print(f"{'='*64}\n")

    def show(n: int, name: str, ok: bool, detail: str) -> None:
        print(f"[{n}/4] {name:<34} {'PASS' if ok else 'FAIL'}  {detail}")

    # 1 — reach go-upc's homepage through the transport
    status, body, err = fetch_html("https://go-upc.com/")
    if err is not None:
        show(1, "reach go-upc.com", False, err)
        print("\nVERDICT: the transport itself failed — nothing reached go-upc.")
        print("Check the credential, and for a proxy the host/port and zone password.")
        return 1
    homepage_ok = status == 200 and "go-upc" in (body or "").lower()
    show(1, "reach go-upc.com", homepage_ok,
         f"HTTP {status}, {len(body or '')} chars, {describe_body(body or '')}")
    if not homepage_ok:
        print(f"\n  page text: {_snippet(body or '', 300)}")
        print("\nVERDICT: connected to Bright Data, but go-upc did not return its homepage.")
        print("The classification above says what came back instead.")
        return 1

    # 2 — the search page for a UPC we know is in their database
    status, body, err = fetch_html(GO_UPC_SEARCH.format(upc=CHECK_UPC))
    if err is not None:
        show(2, "search page for test UPC", False, err)
        return 1
    search_ok = status == 200 and len(body or "") > 1000
    show(2, "search page for test UPC", search_ok,
         f"HTTP {status}, {len(body or '')} chars, {describe_body(body or '')}")
    if not search_ok:
        print(f"\n  page text: {_snippet(body or '', 300)}")
        print("\nVERDICT: the homepage came through but the search page did not.")
        return 1

    # 3 — the parse the real run depends on
    image_url, perr = extract_image_url(body, CHECK_UPC)
    show(3, "product image URL in page", perr is None, perr or image_url)
    if perr is not None:
        print(f"\n  page text: {_snippet(body or '', 300)}")
        print("\nVERDICT: go-upc answered, but the page has no product image in it.")
        print("This is the failure you were seeing. The classification above is the cause;")
        print("if it names a challenge or block page, the zone is not unblocking go-upc.")
        if DEBUG_DUMP_DIR is not None:
            print(f"Full page saved under {DEBUG_DUMP_DIR}/.")
        else:
            print("Re-run with --debug-dump to save the full page.")
        return 1

    # 4 — the image itself (always fetched directly, not through the proxy)
    raw, derr = download_image_bytes(requests_mod, image_url)
    if derr is not None or raw is None:
        show(4, "download + decode image", False, derr or "download failed")
        return 1
    webp_bytes, note, cerr = convert_to_webp(raw, Image)
    ok = cerr is None and webp_bytes is not None
    show(4, "download + decode image", ok, cerr or f"{len(raw)//1024} KB -> {note}")
    if not ok:
        return 1

    print("\nVERDICT: all four stages passed. This transport can fetch the catalog.")
    print("Run the real thing with the same credential flags, minus --check.")
    return 0


def download_image_bytes(requests_mod, image_url: str) -> tuple[Optional[bytes], Optional[str]]:
    """S3-hosted product images are public, so we fetch them directly instead of burning proxy bandwidth."""
    try:
        resp = requests_mod.get(image_url, headers=BROWSER_HEADERS, timeout=30)
    except Exception as e:
        return None, f"image fetch error: {_short(e)}"
    if not resp.ok:
        return None, f"image http {resp.status_code}"
    return resp.content, None


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
    parser.add_argument("--limit-failures", type=int, default=0, metavar="N", help="Abort after N consecutive failures (0 = off).")
    parser.add_argument("--proxy", default=None, metavar="URL", help="Bright Data proxy URL. Overrides BRIGHTDATA_PROXY / GO_UPC_PROXY.")
    parser.add_argument("--api-key", default=None, metavar="KEY", help="Bright Data Web Unlocker API key. Overrides BRIGHTDATA_API_KEY.")
    parser.add_argument("--zone", default=None, metavar="NAME", help=f"Bright Data Web Unlocker zone (default {DEFAULT_WEB_UNLOCKER_ZONE}).")
    parser.add_argument("--direct", action="store_true", help="Skip Bright Data and query go-upc straight from this machine.")
    parser.add_argument("--debug-dump", nargs="?", const="debug_pages", default=None, metavar="DIR",
                        help="Save any page we fail to parse as DIR/<upc>.html (default dir: debug_pages).")
    parser.add_argument("--check", action="store_true", help="Probe the transport against go-upc and exit. Verifies credentials before a real run.")
    parser.add_argument("--list-sections", action="store_true", help="List sections with UPC counts and exit.")
    args = parser.parse_args()

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

    global DEBUG_DUMP_DIR
    if args.debug_dump:
        DEBUG_DUMP_DIR = Path(args.debug_dump)

    # Bright Data is required unless --direct is passed explicitly. Offer a
    # prompt before giving up, so a one-off run needs nothing exported.
    if not args.direct:
        if not api_key and not proxy:
            proxy, api_key = prompt_for_credential()

        if not api_key and not proxy:
            sys.exit(
                "Bright Data credentials are required — go-upc.com is never queried directly.\n"
                "Set one of:\n"
                "  export BRIGHTDATA_API_KEY=<key>     # Web Unlocker API (zone via BRIGHTDATA_ZONE)\n"
                "  export BRIGHTDATA_PROXY='http://brd-customer-<id>-zone-<zone>:<pass>@brd.superproxy.io:33335'\n"
                "...or pass --api-key / --proxy.\n"
                "To bypass Bright Data entirely and query go-upc from this machine, pass --direct."
            )

    if args.check:
        requests_mod = import_requests()
        Image = import_pillow()
        label, fetch_html = make_fetcher(
            requests_mod, api_key=api_key, zone=zone, proxy=proxy, direct=args.direct
        )
        return run_check(requests_mod, Image, label, fetch_html)

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
    use_api = bool(api_key) and not args.direct

    print(f"{'='*64}")
    print(f"Scope:           {scope_label}")
    print(f"Input CSV:       {CSV_PATH}")
    print(f"Output dir:      {OUTPUT_DIR}")
    print(f"Items in scope:  {len(items)}" + (f"   ({len(missing_upc)} CSV rows skipped: no UPC)" if missing_upc else ""))
    print(f"Already on disk: {already_have}{'  (will overwrite)' if args.overwrite else ''}")
    print(f"To fetch now:    {will_fetch}")
    print(f"Format:          lossless WebP, original dimensions")
    print(f"Delay:           {args.delay}s + jitter   |   Limit-failures: {args.limit_failures or 'off'}")
    if use_api:
        print(f"Mode:            Bright Data Web Unlocker API (zone={zone}, key=***)")
    elif args.direct:
        print(f"Mode:            DIRECT — no proxy, requests come from this machine's IP")
    else:
        print(f"Mode:            requests via Bright Data proxy {_redact_proxy(proxy)}")
    if DEBUG_DUMP_DIR is not None:
        print(f"Debug dumps:     {DEBUG_DUMP_DIR}/<upc>.html on parse failure")
    print(f"{'='*64}\n")

    if will_fetch == 0 and not args.overwrite:
        print("Nothing to do (all items already downloaded).")
        return 0

    Image = import_pillow()
    requests_mod = import_requests()

    _, fetch_html = make_fetcher(
        requests_mod, api_key=api_key, zone=zone, proxy=proxy, direct=args.direct
    )

    def get_image_url(upc):
        return image_url_for_upc(fetch_html, upc)

    success: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    consecutive_failures = 0
    started = time.time()

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

        raw, err = download_image_bytes(requests_mod, image_url)
        if err is not None or raw is None:
            consecutive_failures += 1
            if fail(err or "download failed"):
                print(f"\nAborting: {consecutive_failures} consecutive failures.")
                break
            _sleep_with_jitter(args.delay)
            continue

        webp_bytes, note, err = convert_to_webp(raw, Image)
        if err is not None or webp_bytes is None:
            consecutive_failures += 1
            if fail(err or "convert failed"):
                print(f"\nAborting: {consecutive_failures} consecutive failures.")
                break
            _sleep_with_jitter(args.delay)
            continue

        dest, err = save_webp(sku, webp_bytes)
        if err is not None or dest is None:
            consecutive_failures += 1
            if fail(err or "save failed"):
                print(f"\nAborting: {consecutive_failures} consecutive failures.")
                break
            _sleep_with_jitter(args.delay)
            continue

        size_kb = dest.stat().st_size // 1024
        print(f"    [ok]   saved {dest.name}  ({size_kb} KB, {note})  <-  {image_url}")
        success.append(sku)
        consecutive_failures = 0

        if i < len(items):
            _sleep_with_jitter(args.delay)

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
