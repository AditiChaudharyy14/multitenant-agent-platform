// Same-origin calls to /api/* (proxied to FastAPI by next.config.ts), so the session
// cookie and the backend's Origin check just work. Identity is never sent by the client:
// the server reads it from the session cookie.

import type { Conversation, KbSource, Me, MyBookings, MyOrders, ProposalInfo, PublicOrg, StoredMessage } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfter: number | null = null,
  ) {
    super(message);
  }

  static async from(res: Response): Promise<ApiError> {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON: keep the generic message
    }
    const ra = Number(res.headers.get("Retry-After"));
    return new ApiError(res.status, detail, Number.isFinite(ra) && ra > 0 ? Math.ceil(ra) : null);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: init.body ? { "Content-Type": "application/json", ...init.headers } : init.headers,
  });
  if (!res.ok) throw await ApiError.from(res);
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  me: () => call<Me>("/api/me"),
  login: (slug: string, email: string, password: string) =>
    call<Me>(`/api/orgs/${encodeURIComponent(slug)}/auth/login`, json({ email, password })),
  signup: (slug: string, email: string, password: string, display_name: string) =>
    call<Me>(`/api/orgs/${encodeURIComponent(slug)}/auth/signup`, json({ email, password, display_name })),
  logout: () => call<void>("/api/auth/logout", { method: "POST" }),
  conversations: () => call<Conversation[]>("/api/conversations"),
  createConversation: () => call<Conversation>("/api/conversations", { method: "POST" }),
  messages: (id: string) => call<StoredMessage[]>(`/api/conversations/${encodeURIComponent(id)}/messages`),
  renameConversation: (id: string, title: string) =>
    call<Conversation>(`/api/conversations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteConversation: (id: string) => call<void>(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  proposals: (id: string) => call<ProposalInfo[]>(`/api/conversations/${encodeURIComponent(id)}/proposals`),
  myOrders: () => call<MyOrders>("/api/orders"),
  myBookings: () => call<MyBookings>("/api/bookings"),
  kbSource: (ref: string) => call<KbSource>(`/api/kb/source?ref=${encodeURIComponent(ref)}`),
};

/** Server-side only: branding for the landing page, straight from the backend. */
export async function fetchPublicOrg(slug: string): Promise<PublicOrg | null> {
  const base = process.env.BACKEND_URL ?? "http://localhost:8000";
  const res = await fetch(`${base}/api/orgs/${encodeURIComponent(slug)}`, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`org lookup failed: ${res.status}`);
  return res.json();
}

/**
 * Server-side only: who the visitor's session cookie belongs to, or null. The cookie is
 * forwarded as-is; the backend alone decides identity from it.
 */
export async function fetchSessionMe(sid: string | undefined): Promise<Me | null> {
  if (!sid) return null;
  const base = process.env.BACKEND_URL ?? "http://localhost:8000";
  const res = await fetch(`${base}/api/me`, { headers: { Cookie: `sid=${sid}` }, cache: "no-store" });
  return res.ok ? res.json() : null;
}
