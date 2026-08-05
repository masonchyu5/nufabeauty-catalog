// Column set build_catalog.py reads. "Required" means the build breaks or
// silently drops data without it.
const CHEMICAL_REQUIRED_HEADERS = [
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
const CHEMICAL_RETIRED_HEADERS = new Set([
  "section",
  "section_order",
  "group_title",
  "group_order",
  "verified",
  "category",
  "image_path",
]);
const CHEMICAL_KNOWN_HEADERS = new Set([
  ...CHEMICAL_REQUIRED_HEADERS,
  ...CHEMICAL_RETIRED_HEADERS,
]);

// EXPECTED_HEADER in build_general.py: the build compares the raw header row
// for exact equality (names, order, nothing extra) and refuses the whole file
// on any drift, so validation must be exactly as strict or a publish would
// sail through here and then fail in CI.
const GENERAL_HEADER = [
  "sku",
  "name",
  "unit_price",
  "qty_display",
  "bullets",
  "upc",
  "section",
  "section_order",
  "group_title",
  "group_order",
  "item_order",
];

const MAX_LISTED_ROWS = 15;

// Mirrors _slug() in both build scripts, which names every display copy.
export function slugify(text) {
  const out = String(text || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return out || "untitled";
}

// "source/chemical/master-images/CH110612.jpg" -> "ch110612". A master's stem is the
// SKU of the product it belongs to; matching is case-insensitive because the
// uploader preserves whatever case was typed.
export function imageStem(filename) {
  const base = String(filename || "").split("/").pop();
  const dot = base.lastIndexOf(".");
  return (dot > 0 ? base.slice(0, dot) : base).toLowerCase();
}

// The display copy the catalog's build derives from a given master.
export function normalizedNameFor(filename, catalog) {
  return `${slugify(imageStem(filename))}${catalog.normalizedExt}`;
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
  const allRows = parseCsv(text);
  const rows = allRows.filter((r) => r.some((cell) => cell.trim() !== ""));
  if (!rows.length) return { headers: [], rawHeaders: [], records: [] };
  const headers = rows[0].map((h) => h.trim());
  const records = rows.slice(1).map((cells, idx) => {
    const rec = { __row: idx + 2 };
    headers.forEach((h, i) => {
      if (h) rec[h] = (cells[i] ?? "").trim();
    });
    return rec;
  });
  // rawHeaders is the first parsed row untrimmed and unfiltered — what
  // csv.DictReader.fieldnames sees — for the General build's exact-match check.
  return { headers, rawHeaders: allRows[0], records };
}

export function validateCsv(text, { catalog, repoImages = new Set(), batchImages = new Set() } = {}) {
  return catalog && catalog.key === "general"
    ? validateGeneralCsv(text, repoImages, batchImages)
    : validateChemicalCsv(text, repoImages, batchImages);
}

function validateChemicalCsv(text, repoImages, batchImages) {
  const errors = [];
  const warnings = [];
  const stats = {};

  const { headers, records } = csvRecords(text);
  if (!headers.length) {
    return { ok: false, errors: ["The CSV file is empty."], warnings, stats };
  }

  const missing = CHEMICAL_REQUIRED_HEADERS.filter((h) => !headers.includes(h));
  if (missing.length) {
    errors.push(`Missing or renamed column(s): ${missing.join(", ")}`);
    return { ok: false, errors, warnings, stats };
  }
  const retired = headers.filter((h) => h && CHEMICAL_RETIRED_HEADERS.has(h));
  if (retired.length) {
    warnings.push(
      `Column(s) no longer part of the chemical catalog, ignored: ${retired.join(", ")}. ` +
        "Download the current CSV to get the up-to-date columns."
    );
  }
  const unknown = headers.filter((h) => h && !CHEMICAL_KNOWN_HEADERS.has(h));
  if (unknown.length) {
    warnings.push(`Unrecognized column(s), ignored by the build: ${unknown.join(", ")}`);
  }

  // Every row in this file is a chemical item; only missing brand metadata can
  // put one out of scope. Must stay in step with in_scope() in build_catalog.py.
  const inScope = records.filter((r) => r.brand && r.brand_abbrev && r.brand_order);

  stats.totalRows = records.length;
  stats.inScopeRows = inScope.length;
  stats.skipped = records.length - inScope.length;
  stats.skippedNote = "missing brand info";

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

function validateGeneralCsv(text, repoImages, batchImages) {
  const errors = [];
  const warnings = [];
  const stats = {};

  const { headers, rawHeaders, records } = csvRecords(text);
  if (!headers.length) {
    return { ok: false, errors: ["The CSV file is empty."], warnings, stats };
  }

  // build_general.py: `reader.fieldnames != EXPECTED_HEADER` — raw, ordered,
  // nothing extra (even a stray space in a column name fails the build).
  if ((rawHeaders || []).join(",") !== GENERAL_HEADER.join(",")) {
    errors.push(
      "The column header row must match the General catalog exactly (same names, same order, nothing extra). " +
        `Expected: ${GENERAL_HEADER.join(",")} — found: ${(rawHeaders || []).join(",")}. ` +
        "Download the current CSV and redo the edits in that file."
    );
    return { ok: false, errors, warnings, stats };
  }

  // Mirrors load_sections(): rows with a blank sku or section never render.
  const inScope = records.filter((r) => r.sku && r.section);
  const skippedRows = records.filter((r) => !(r.sku && r.section)).map((r) => r.__row);

  stats.totalRows = records.length;
  stats.inScopeRows = inScope.length;
  stats.skipped = skippedRows.length;
  stats.skippedNote = "blank sku or section";

  if (skippedRows.length) {
    warnings.push(
      `Row(s) with a blank sku or section are left out of the catalog: rows ${listRows(skippedRows)}`
    );
  }
  if (!inScope.length) {
    errors.push("No publishable rows: every row is missing its sku or its section.");
    return { ok: false, errors, warnings, stats };
  }

  // build_general.py refuses the whole file when two rows' SKUs share a display
  // slug — which includes plain duplicate SKUs. Errors, not warnings: a publish
  // would land, then the CI build would fail and the site would keep the old data.
  const bySlug = new Map();
  const collisions = [];
  for (const r of inScope) {
    const slug = slugify(r.sku);
    const prev = bySlug.get(slug);
    if (prev) collisions.push({ prev, row: r });
    else bySlug.set(slug, r);
  }
  if (collisions.length) {
    const examples = collisions.slice(0, 5).map(({ prev, row }) =>
      prev.sku === row.sku
        ? `${row.sku} appears twice (rows ${prev.__row} and ${row.__row})`
        : `${prev.sku} and ${row.sku} differ only in case/punctuation (rows ${prev.__row} and ${row.__row})`
    );
    errors.push(
      "Every General row needs a unique SKU (and no two SKUs may differ only in case or punctuation) — " +
        `the build refuses the whole file otherwise: ${examples.join("; ")}` +
        (collisions.length > 5 ? ` (+${collisions.length - 5} more)` : "")
    );
  }

  // The build silently blanks a malformed price (the card shows no price at
  // all), so catch it here — a blank price is a feature, a typo is not.
  const badPrice = inScope
    .filter((r) => r.unit_price && !/^\d+(\.\d+)?$/.test(r.unit_price))
    .map((r) => r.__row);
  if (badPrice.length) {
    errors.push(
      `Malformed unit_price (must be a plain number like 17.25, or empty for no price): rows ${listRows(badPrice)}`
    );
  }
  const emptyPrice = inScope.filter((r) => !r.unit_price).length;
  if (emptyPrice) {
    warnings.push(
      `${emptyPrice} row(s) have no unit_price and will show quantity only (normal for JOY PRODUCTS).`
    );
  }

  const badOrder = inScope
    .filter((r) =>
      ["section_order", "group_order", "item_order"].some(
        (k) => r[k] && !/^\d+$/.test(r[k])
      )
    )
    .map((r) => r.__row);
  if (badOrder.length) {
    warnings.push(
      `Non-numeric section_order/group_order/item_order (these sort last): rows ${listRows(badOrder)}`
    );
  }

  // The build takes a section's order from its first row; a section whose rows
  // disagree is a hand-editing slip that would silently reorder pages.
  const sectionOrder = new Map();
  const conflicted = new Set();
  for (const r of inScope) {
    if (!/^\d+$/.test(r.section_order || "")) continue;
    const prev = sectionOrder.get(r.section);
    if (prev === undefined) sectionOrder.set(r.section, r.section_order);
    else if (prev !== r.section_order) conflicted.add(r.section);
  }
  if (conflicted.size) {
    warnings.push(
      `Section(s) with more than one section_order (the first row's value wins): ${[...conflicted].join(", ")}`
    );
  }

  // A blank UPC renders no barcode by design; only a non-blank wrong-length
  // one is worth flagging.
  const badUpc = inScope.filter((r) => {
    const digits = (r.upc || "").replace(/\D+/g, "");
    return digits.length > 0 && digits.length !== 12 && digits.length !== 13;
  }).length;
  if (badUpc) {
    warnings.push(
      `${badUpc} row(s) have a UPC that is not 12 or 13 digits; no barcode will render for them.`
    );
  }

  const available = availablePhotoStems(repoImages, batchImages);
  const noPhoto = inScope.filter((r) => !available.has(r.sku.toLowerCase()));
  if (noPhoto.length) {
    warnings.push(
      `${noPhoto.length} product(s) have no photo yet and will show the placeholder. ` +
        "Upload a photo named after the SKU to fill one in."
    );
  }
  stats.withPhoto = inScope.length - noPhoto.length;

  return { ok: errors.length === 0, errors, warnings, stats };
}
