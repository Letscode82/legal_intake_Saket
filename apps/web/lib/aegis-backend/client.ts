/**
 * Typed AEGIS backend client (browser-side).
 *
 * Calls go through the same-origin BFF proxy (`/api/backend/*`), which
 * attaches the Auth0 token and forwards to FastAPI. Components never talk to
 * the backend URL directly and never hold the token.
 *
 * Types: run `pnpm --filter @aegis/web gen:api` to regenerate
 * `./schema.ts` from the backend's live OpenAPI document, then import
 * `paths` from it for end-to-end type safety. Until generated, the client
 * is still usable with explicit generics.
 */

const BASE = "/api/backend";

export class BackendError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `Backend error ${status}`);
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { devUserEmail?: string } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("content-type") && init.body) {
    headers.set("content-type", "application/json");
  }
  // Dev-only: preview a role lens without Auth0 (ignored in production).
  if (init.devUserEmail) headers.set("x-dev-user-email", init.devUserEmail);

  const res = await fetch(`${BASE}/${path.replace(/^\//, "")}`, {
    ...init,
    headers,
  });
  const isJson = res.headers.get("content-type")?.includes("application/json");
  const payload = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    throw new BackendError(
      res.status,
      typeof payload === "object" && payload && "detail" in payload
        ? (payload as { detail: unknown }).detail
        : payload,
    );
  }
  return payload as T;
}

export const backend = {
  get: <T>(path: string, opts?: { devUserEmail?: string }) =>
    request<T>(path, { method: "GET", ...opts }),
  post: <T>(path: string, body?: unknown, opts?: { devUserEmail?: string }) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
      ...opts,
    }),
};

// ── Flagship typed helpers (hand-written until schema.ts is generated) ──
export interface FrontDoorRequest {
  description: string;
  requester_name: string;
  requester_email?: string;
  department?: string;
  request_type?: string;
  context?: Record<string, unknown>;
  external_message_id?: string;
}

export interface AgentDecision {
  id: string;
  agent_id: string;
  resource_type: string;
  resource_id: string;
  action_key: string;
  action_payload: Record<string, unknown>;
  recommendation: {
    confidence: number;
    suggested_action: string;
    reasoning: string;
    concerns: string[];
    citations: { type: string; id: string; title: string }[];
    drafted_response?: string;
  };
  status: "PENDING" | "APPROVED" | "APPROVED_WITH_OVERRIDE" | "REJECTED";
}

export const aegis = {
  frontDoor: (body: FrontDoorRequest) =>
    backend.post<{ workflow_instance_id: string; request_type: string }>(
      "intake/front-door",
      body,
    ),
  pendingDecisions: () =>
    backend.get<AgentDecision[]>("cockpit/decisions?status=PENDING"),
  approveDecision: (id: string, payloadOverride?: Record<string, unknown>) =>
    backend.post<AgentDecision>(`cockpit/decisions/${id}/approve`, {
      payload_override: payloadOverride ?? null,
    }),
  rejectDecision: (id: string, comment: string) =>
    backend.post<AgentDecision>(`cockpit/decisions/${id}/reject`, { comment }),
  askBrain: (question: string) =>
    backend.post<{ answer: string; citations: unknown[]; gap_notes: string[] }>(
      "brain/query",
      { question },
    ),
};
