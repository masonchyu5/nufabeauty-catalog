// The only per-catalog path constants in the admin backend. Every route
// resolves one of these entries and passes it down; no other file may name a
// source/, images/, or CSV path.
//
// normalizedDir holds the display copies the build derives from the masters.
// Deleting a master has to take its derivative too, or the repo keeps the
// bytes of every photo ever removed — nothing in the Chemical build ever
// unlinks from that directory (the General build prunes orphans itself, but
// deleting both in one commit keeps the site from serving a dead image until
// the next build).
export const CATALOGS = {
  chemical: {
    key: "chemical",
    label: "Chemical",
    csvPath: "source/chemical/items.csv",
    imagesDir: "source/chemical/master-images",
    normalizedDir: "images/chemical",
    // build_catalog.py derives <slug of SKU>.jpg from every master.
    normalizedExt: ".jpg",
    workflowName: "Build catalog",
  },
  general: {
    key: "general",
    label: "General",
    csvPath: "source/general/items.csv",
    imagesDir: "source/general/master-images",
    normalizedDir: "images/general",
    // build_general.py derives <slug of SKU>.webp from every master.
    normalizedExt: ".webp",
    workflowName: "Build general catalog",
  },
};

// Missing/blank means Chemical — the only catalog the admin knew before the
// picker existed, so old bookmarks and in-flight tabs keep working. Anything
// else unrecognized is null; callers must 400, never guess a path.
export function resolveCatalog(raw) {
  if (raw == null || raw === "") return CATALOGS.chemical;
  return CATALOGS[String(raw)] || null;
}
