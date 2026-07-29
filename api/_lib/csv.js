// Column set build_catalog.py reads. "Required" means the build breaks or
// silently drops data without it.
const REQUIRED_HEADERS = [
  "sku",
  "name",
  "upc",
  "item_order",
  "brand",
  "brand_abbrev",
  "brand_order",
  "unit_price",
  "qty_display",
];
// Columns the chemical CSV used to carry: the General catalog's layout fields,
// a `category` discriminator back when both catalogs shared one file, an unused
// data-entry flag, and `image_path` from before photos were matched to products
// by filename. A CSV exported before those cleanups still publishes correctly --
// these are ignored, with a note so the admin knows the file is stale. Anything
// else unrecognized gets the generic warning.
const RETIRED_HEADERS = new Set([
  "section",
  "section_order",
  "group_title",
  "group_order",
  "verified",
  "category",
  "image_path",
]);
const KNOWN_HEADERS = new Set([...REQUIRED_HEADERS, ...RETIRED_HEADERS]);

const MAX_LISTED_ROWS = 15;

// Mirrors _slug() in build_catalog.py, which names every display copy.
export function slugify(text) {
  const out = String(text || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return out || "untitled";
}

// "pages/chemical-upc-v3/CH110612.jpg" -> "ch110612". A master's stem is the
// SKU of the product it belongs to; matching is case-insensitive because the
// uploader preserves whatever case was typed.
export function imageStem(filename) {
  const base = String(filename || "").split("/").pop();
  const dot = base.lastIndexOf(".");
  return (dot > 0 ? base.slice(0, dot) : base).toLowerCase();
}

// The display copy build_catalog.py derives from a given master.
export function normalizedNameFor(filename) {
  return `${slugify(imageStem(filename))}.jpg`;
}

export function parseCsv(text) {
  const src = text.startsWith("\uFEFF") ? text.slice(1) : text;
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (inQuotes) {
      if (c === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += c;
      }
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && src[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += c;
    }
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function listRows(rowNumbers) {
  const shown = rowNumbers.slice(0, MAX_LISTED_ROWS).join(", ");
  const more = rowNumbers.length - MAX_LISTED_ROWS;
  return more > 0 ? `${shown} (+${more} more)` : shown;
}

// SKUs that have a photo available, given what is in the repo plus whatever is
// being uploaded alongside this CSV.
export function availablePhotoStems(repoImages, batchImages) {
  const stems = new Set();
  for (const name of repoImages) stems.add(imageStem(name));
  for (const name of batchImages) stems.add(imageStem(name));
  return stems;
}

export function csvRecords(text) {
  const rows = parseCsv(text).filter((r) => r.some((cell) => cell.trim() !== ""));
  if (!rows.length) return { headers: [], records: [] };
  const headers = rows[0].map((h) => h.trim());
  const records = rows.slice(1).map((cells, idx) => {
    const rec = { __row: idx + 2 };
    headers.forEach((h, i) => {
      if (h) rec[h] = (cells[i] ?? "").trim();
    });
    return rec;
  });
  return { headers, records };
}

export function validateCsv(text, { repoImages = new Set(), batchImages = new Set() } = {}) {
  const errors = [];
  const warnings = [];
  const stats = {};

  const { headers, records } = csvRecords(text);
  if (!headers.length) {
    return { ok: false, errors: ["The CSV file is empty."], warnings, stats };
  }

  const missing = REQUIRED_HEADERS.filter((h) => !headers.includes(h));
  if (missing.length) {
    errors.push(`Missing or renamed column(s): ${missing.join(", ")}`);
    return { ok: false, errors, warnings, stats };
  }
  const retired = headers.filter((h) => h && RETIRED_HEADERS.has(h));
  if (retired.length) {
    warnings.push(
      `Column(s) no longer part of the chemical catalog, ignored: ${retired.join(", ")}. ` +
        "Download the current CSV to get the up-to-date columns."
    );
  }
  const unknown = headers.filter((h) => h && !KNOWN_HEADERS.has(h));
  if (unknown.length) {
    warnings.push(`Unrecognized column(s), ignored by the build: ${unknown.join(", ")}`);
  }

  // Every row in this file is a chemical item; only missing brand metadata can
  // put one out of scope. Must stay in step with in_scope() in build_catalog.py.
  const inScope = records.filter((r) => r.brand && r.brand_abbrev && r.brand_order);

  stats.totalRows = records.length;
  stats.chemicalRows = records.length;
  stats.inScopeRows = inScope.length;
  stats.skippedMissingBrand = records.length - inScope.length;

  if (!inScope.length) {
    errors.push(
      "No publishable rows: no row has all of brand, brand_abbrev, and brand_order filled in."
    );
    return { ok: false, errors, warnings, stats };
  }

  const emptySku = inScope.filter((r) => !r.sku).map((r) => r.__row);
  if (emptySku.length) {
    errors.push(`Rows missing a SKU: ${listRows(emptySku)}`);
  }

  const seenSkus = new Map();
  const dupSku = [];
  for (const r of inScope) {
    if (!r.sku) continue;
    if (seenSkus.has(r.sku)) dupSku.push(r.__row);
    else seenSkus.set(r.sku, r.__row);
  }
  if (dupSku.length) {
    warnings.push(
      `Duplicate SKUs (later rows overwrite the earlier product image): rows ${listRows(dupSku)}`
    );
  }

  const badPrice = inScope
    .filter((r) => r.unit_price && !/^\d+(\.\d+)?$/.test(r.unit_price))
    .map((r) => r.__row);
  if (badPrice.length) {
    errors.push(
      `Malformed unit_price (must be a plain number like 2.35): rows ${listRows(badPrice)}`
    );
  }
  const emptyPrice = inScope.filter((r) => !r.unit_price).length;
  if (emptyPrice) {
    warnings.push(`${emptyPrice} row(s) have no unit_price and will show no price.`);
  }

  const badOrder = inScope
    .filter((r) => !/^\d+$/.test(r.brand_order) || !/^\d+$/.test(r.item_order || ""))
    .map((r) => r.__row);
  if (badOrder.length) {
    warnings.push(
      `Non-numeric brand_order/item_order (these rows sort last): rows ${listRows(badOrder)}`
    );
  }

  const badUpc = inScope.filter((r) => {
    const digits = (r.upc || "").replace(/\D+/g, "");
    return digits.length !== 12 && digits.length !== 13;
  }).length;
  if (badUpc) {
    warnings.push(`${badUpc} row(s) have a UPC that is not 12 or 13 digits; no barcode will render.`);
  }

  // A product's photo is the uploaded master whose filename matches its SKU,
  // so the only thing to check is whether one exists yet.
  const available = availablePhotoStems(repoImages, batchImages);
  const noPhoto = inScope.filter((r) => r.sku && !available.has(r.sku.toLowerCase()));
  if (noPhoto.length) {
    warnings.push(
      `${noPhoto.length} product(s) have no uploaded photo and will show a placeholder. ` +
        "Upload a photo named after the SKU to fix that."
    );
  }
  stats.withPhoto = inScope.length - noPhoto.length;

  return { ok: errors.length === 0, errors, warnings, stats };
}
