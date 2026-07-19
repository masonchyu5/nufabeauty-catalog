export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

const FILENAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const EXTENSION_KIND = {
  ".jpg": "jpeg",
  ".jpeg": "jpeg",
  ".png": "png",
  ".webp": "webp",
};

// Returns { base, kind } for an acceptable image filename, else null.
// Directory components are stripped; the result is always a bare basename.
export function sanitizeImageFilename(raw) {
  const base = String(raw || "").split(/[\\/]/).pop() || "";
  if (base.includes("..") || !FILENAME_RE.test(base) || base.length > 120) return null;
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return null;
  const kind = EXTENSION_KIND[base.slice(dot).toLowerCase()];
  if (!kind) return null;
  return { base, kind };
}

const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export function sniffImageKind(buf) {
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "jpeg";
  if (buf.length >= 8 && buf.subarray(0, 8).equals(PNG_MAGIC)) return "png";
  if (
    buf.length >= 12 &&
    buf.subarray(0, 4).toString("latin1") === "RIFF" &&
    buf.subarray(8, 12).toString("latin1") === "WEBP"
  ) {
    return "webp";
  }
  return null;
}
