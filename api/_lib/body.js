async function collectStream(req, maxBytes) {
  const chunks = [];
  let total = 0;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > maxBytes) {
      const err = new Error("Request body too large");
      err.status = 413;
      throw err;
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export async function readRawBody(req, maxBytes) {
  if (Buffer.isBuffer(req.body)) {
    if (req.body.length > maxBytes) {
      const err = new Error("Request body too large");
      err.status = 413;
      throw err;
    }
    return req.body;
  }
  return collectStream(req, maxBytes);
}

export async function readJsonBody(req, maxBytes = 10 * 1024 * 1024) {
  if (req.body !== undefined && !Buffer.isBuffer(req.body)) {
    if (typeof req.body === "string") return JSON.parse(req.body);
    return req.body;
  }
  const raw = Buffer.isBuffer(req.body) ? req.body : await collectStream(req, maxBytes);
  if (!raw.length) return {};
  return JSON.parse(raw.toString("utf8"));
}
