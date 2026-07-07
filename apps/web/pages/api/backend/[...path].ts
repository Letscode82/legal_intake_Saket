/**
 * BFF proxy — forward-only bridge to the FastAPI backend.
 *
 * The Next.js app is presentation + a thin BFF. This catch-all forwards
 * `/api/backend/<path>` to `${BACKEND_URL}/api/v1/<path>`, attaching the
 * Auth0 access token as a Bearer header so the browser never holds the
 * token and the backend URL/CORS stays server-side.
 *
 * NON-NEGOTIABLE: this is FORWARD-ONLY. No business logic, no data shaping,
 * no auth decisions — the FastAPI backend is the sole authority for
 * permissions, the human-approval gate, and the audit chain. If you find
 * yourself adding an `if` about the payload here, it belongs in the backend.
 *
 * Dev mode (Auth0 unconfigured): no token is attached; the backend's
 * dev-mode fallback resolves the seeded admin. Optionally forwards
 * `x-dev-user-email` for role-lens testing.
 */
import type { NextApiRequest, NextApiResponse } from "next";

const BACKEND_URL =
  process.env.BACKEND_URL ||
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "http://localhost:8000";

// Hop-by-hop + host headers that must not be forwarded verbatim.
const STRIP = new Set([
  "host",
  "connection",
  "content-length",
  "transfer-encoding",
  "accept-encoding",
]);

async function bearerToken(
  req: NextApiRequest,
  res: NextApiResponse,
): Promise<string | null> {
  // Auth0 is optional in dev. Import lazily so the app runs without it
  // configured, and never let a token-fetch failure 500 the proxy.
  try {
    const mod = await import("@auth0/nextjs-auth0");
    const getAccessToken = (mod as { getAccessToken?: unknown })
      .getAccessToken as
      | ((
          req: NextApiRequest,
          res: NextApiResponse,
        ) => Promise<{ accessToken?: string }>)
      | undefined;
    if (!getAccessToken) return null;
    const { accessToken } = await getAccessToken(req, res);
    return accessToken ?? null;
  } catch {
    return null;
  }
}

export const config = {
  api: { bodyParser: false }, // stream the raw body through unchanged
};

async function rawBody(req: NextApiRequest): Promise<Buffer | undefined> {
  if (req.method === "GET" || req.method === "HEAD") return undefined;
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks);
}

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  const { path = [] } = req.query;
  const suffix = Array.isArray(path) ? path.join("/") : String(path);
  const qs = req.url?.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
  const target = `${BACKEND_URL}/api/v1/${suffix}${qs}`;

  const headers: Record<string, string> = {};
  for (const [key, value] of Object.entries(req.headers)) {
    if (STRIP.has(key.toLowerCase()) || value === undefined) continue;
    headers[key] = Array.isArray(value) ? value.join(", ") : value;
  }

  const token = await bearerToken(req, res);
  if (token) headers["authorization"] = `Bearer ${token}`;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: await rawBody(req),
    });
  } catch (err) {
    res.status(502).json({
      error: "backend_unreachable",
      detail: `Could not reach the AEGIS backend at ${BACKEND_URL}.`,
    });
    return;
  }

  res.status(upstream.status);
  upstream.headers.forEach((value, key) => {
    if (!STRIP.has(key.toLowerCase())) res.setHeader(key, value);
  });
  const buf = Buffer.from(await upstream.arrayBuffer());
  res.send(buf);
}
