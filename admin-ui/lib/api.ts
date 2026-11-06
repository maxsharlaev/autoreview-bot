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

export type SpaceItem = {
  id: number;
  login: string;
  type: "User" | "Organization";
};

export type SpaceListResponse = {
  items: SpaceItem[];
};

export type OpenFindings = {
  P0: number;
  P1: number;
  P2: number;
  P3: number;
};

export type RepoItem = {
  full_name: string;
  open_pr_count: number | null;
  open_findings: OpenFindings;
  last_run_at: string | null;
};

export type RepoListResponse = {
  items: RepoItem[];
};

export type RepoPRItem = {
  number: number;
  title: string;
  head_sha_short: string;
  last_run_status: string | null;
};

export type RepoFindingItem = {
  id: string;
  stable_id: string;
  severity: string;
  title: string;
  path: string;
  line: number | null;
  pull_request_number: number;
};

export type RepoDetailResponse = {
  full_name: string;
  open_prs: RepoPRItem[];
  open_findings: RepoFindingItem[];
  open_findings_count: number;
  limit: number;
  offset: number;
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

export function getSpaces(): Promise<SpaceListResponse> {
  return apiGet<SpaceListResponse>("/spaces");
}

export function getSpaceRepos(
  installationId: number,
  hideEmptyPrs = true
): Promise<RepoListResponse> {
  const params = new URLSearchParams();
  params.set("hide_empty_prs", String(hideEmptyPrs));
  return apiGet<RepoListResponse>(`/spaces/${installationId}/repos?${params}`);
}

export function getRepoDetail(
  owner: string,
  repo: string,
  installationId?: number,
  limit = 50,
  offset = 0,
  findingId?: string | null
): Promise<RepoDetailResponse> {
  const params = new URLSearchParams();
  if (installationId !== undefined) {
    params.set("installation_id", String(installationId));
  }
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (findingId) {
    params.set("finding_id", findingId);
  }
  return apiGet<RepoDetailResponse>(`/repos/${owner}/${repo}?${params}`);
}
