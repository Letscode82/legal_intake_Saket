/**
 * Auth0 catch-all handler factory.
 *
 * apps/web mounts this at `pages/api/auth/[...auth0].ts`. When Auth0 is
 * not configured (dev mode, missing env vars), the handler serves a
 * deterministic JSON response so the route still exists — anything that
 * follows a login link sees a 200 and a "disabled" payload, instead of
 * a build-time crash from the SDK initialising with an empty AUTH0_SECRET.
 */

import type { NextApiRequest, NextApiResponse } from "next";
import { isAuth0Configured } from "./server";

type AuthHandler = (req: NextApiRequest, res: NextApiResponse) => unknown;

const REQUIRED_ENV_VARS = [
  "AUTH0_SECRET",
  "AUTH0_BASE_URL",
  "AUTH0_ISSUER_BASE_URL",
  "AUTH0_CLIENT_ID",
  "AUTH0_CLIENT_SECRET",
] as const;

/**
 * Once-per-cold-start diagnostic log. Reports which AUTH0_* env vars
 * are present vs. missing AND whether the runtime is production. Logs
 * server-side only — never appears in any HTTP response — and never
 * logs the actual values.
 *
 * Why: a missing env var manifests as a 500 from /api/auth/login in
 * production, with the failure cause invisible without redeploying.
 * This line in the cold-start log makes the failure mode self-evident:
 * a glance at Vercel function logs answers "did the build see the
 * vars" without redeploying with extra logging.
 */
let _envCheckLogged = false;
function logEnvCheckOnce() {
  if (_envCheckLogged) return;
  _envCheckLogged = true;

  const present: string[] = [];
  const missing: string[] = [];
  for (const k of REQUIRED_ENV_VARS) {
    const v = process.env[k];
    if (typeof v === "string" && v.length > 0) {
      present.push(k);
    } else {
      missing.push(k);
    }
  }

  const nodeEnv = process.env.NODE_ENV ?? "(unset)";
  const vercelEnv = process.env.VERCEL_ENV ?? "(unset)";

  // Single-line structured log so it's grep-able from Vercel function logs.
  console.log(
    "[@aegis/auth] env check on first /api/auth/* request: " +
      `NODE_ENV=${nodeEnv} VERCEL_ENV=${vercelEnv} ` +
      `present=[${present.join(",")}] ` +
      `missing=[${missing.join(",")}] ` +
      `auth0_configured=${present.length === REQUIRED_ENV_VARS.length}`,
  );
}

/**
 * Returns a handler suitable for the `pages/api/auth/[...auth0].ts`
 * file. Lazy-imports @auth0/nextjs-auth0 only when configured.
 */
export function makeAuthHandler(): AuthHandler {
  return async (req, res) => {
    // Cold-start diagnostic — once per process, server-side only.
    logEnvCheckOnce();

    if (!isAuth0Configured()) {
      // Dev mode — describe the state instead of crashing.
      res.status(200).json({
        ok: false,
        mode: "dev-no-auth",
        message:
          "Auth0 is not configured. The demo runs as the seeded admin (Alex Nguyen). " +
          "Set AUTH0_SECRET, AUTH0_BASE_URL, AUTH0_ISSUER_BASE_URL, AUTH0_CLIENT_ID, and AUTH0_CLIENT_SECRET to enable the login flow.",
      });
      return;
    }
    const { handleAuth, handleLogin } = await import("@auth0/nextjs-auth0");
    // W4-7 — enterprise SSO: when AUTH0_ENTERPRISE_CONNECTION names an
    // Auth0 enterprise connection (e.g. the Entra ID connection for the
    // client tenant), /api/auth/login sends users straight to that IdP
    // instead of the Auth0 universal-login picker. Unset = unchanged.
    const connection = process.env.AUTH0_ENTERPRISE_CONNECTION;
    // Monorepo split: when AUTH0_AUDIENCE is set (the FastAPI backend's API
    // identifier), request that audience at login so the resulting access
    // token is a JWT the backend's JWKS validation accepts. The BFF proxy
    // (apps/web pages/api/backend) forwards this token as a Bearer header.
    const audience = process.env.AUTH0_AUDIENCE;
    const authorizationParams: Record<string, string> = {};
    if (connection) authorizationParams.connection = connection;
    if (audience) {
      authorizationParams.audience = audience;
      // openid/profile/email for the session; offline_access for refresh.
      authorizationParams.scope = "openid profile email offline_access";
    }
    const handler = Object.keys(authorizationParams).length
      ? handleAuth({ login: handleLogin({ authorizationParams }) })
      : handleAuth();
    return handler(req, res);
  };
}
