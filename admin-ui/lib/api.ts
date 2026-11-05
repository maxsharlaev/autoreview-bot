const API_BASE = "/api/v1/admin";

export type AdminMe = {
  login: string;
  role: "admin" | "viewer";
  kind: string;
};

export type LoginResponse = {
  ok: boolean;
  role: string;
  kind: string;
};

async function parseError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") return data.detail;
    return JSON.stringify(data?.detail ?? data);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body ?? {}),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(await parseError(res));
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function getMe(): Promise<AdminMe> {
  return apiGet<AdminMe>("/me");
}

export function login(loginName: string, password: string): Promise<LoginResponse> {
  return apiPost<LoginResponse>("/login", { login: loginName, password });
}

export function logout(): Promise<void> {
  return apiPost<void>("/logout", {});
}
