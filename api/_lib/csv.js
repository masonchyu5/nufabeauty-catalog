import { IMAGES_DIR } from "./github.js";

// Column set build_catalog.py reads. "Required" means the build breaks or
// silently drops data without it; the rest are known-but-optional columns.
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
  "image_path",
  "category",
];
const KNOWN_HEADERS = new Set([
  ...REQUIRED_HEADERS,
  "section",
  "section_order",
  "group_title",
  "group_order",
  "verified",
]);

const MAX_LISTED_ROWS = 15;

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

export function csvImageBasenames(records) {
  const names = new Set();
  for (const rec of records) {
    const rel = (rec.image_path || "").trim();
    if (rel) names.add(rel.split("/").pop());
  }
  return names;
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
  const unknown = headers.filter((h) => h && !KNOWN_HEADERS.has(h));
  if (unknown.length) {
    warnings.push(`Unrecognized column(s), ignored by the build: ${unknown.join(", ")}`);
  }

  const chemical = records.filter((r) => r.category === "Chemical");
  const inScope = chemical.filter((r) => r.brand && r.brand_abbrev && r.brand_order);

  stats.totalRows = records.length;
  stats.chemicalRows = chemical.length;
  stats.inScopeRows = inScope.length;
  stats.skippedMissingBrand = chemical.length - inScope.length;

  if (!inScope.length) {
    errors.push(
      'No publishable rows: nothing has category "Chemical" plus brand, brand_abbrev, and brand_order.'
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

  const noImagePath = inScope.filter((r) => !r.image_path).length;
  if (noImagePath) {
    warnings.push(`${noImagePath} row(s) have no image_path and will show no product photo.`);
  }

  const outsideDir = [];
  const missingFile = [];
  for (const r of inScope) {
    const rel = r.image_path;
    if (!rel) continue;
    if (!rel.startsWith(`${IMAGES_DIR}/`)) {
      outsideDir.push(r.__row);
      continue;
    }
    const base = rel.slice(IMAGES_DIR.length + 1);
    if (base.includes("/") || (!repoImages.has(base) && !batchImages.has(base))) {
      missingFile.push(`row ${r.__row}: ${rel}`);
    }
  }
  if (outsideDir.length) {
    warnings.push(
      `image_path outside ${IMAGES_DIR}/ (cannot verify the file exists): rows ${listRows(outsideDir)}`
    );
  }
  if (missingFile.length) {
    warnings.push(
      `Image file not found in the repo or this upload batch (product will show no photo): ${listRows(missingFile)}`
    );
  }

  stats.referencedImages = csvImageBasenames(inScope).size;

  return { ok: errors.length === 0, errors, warnings, stats };
}
