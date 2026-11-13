import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RepoDetailResponse, RepoFindingItem } from "@/lib/api";

const nav = vi.hoisted(() => ({
  params: new URLSearchParams("space=1&repo=acme/repo&finding=find-9"),
}));

const api = vi.hoisted(() => ({
  getMe: vi.fn(),
  getSpaces: vi.fn(),
  getSpaceRepos: vi.fn(),
  getRepoDetail: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/repositories/",
  useSearchParams: () => nav.params,
}));

vi.mock("@/lib/api", () => ({
  getMe: api.getMe,
  getSpaces: api.getSpaces,
  getSpaceRepos: api.getSpaceRepos,
  getRepoDetail: api.getRepoDetail,
  logout: api.logout,
}));

import RepositoriesPage from "./page";

type DetailCall = {
  repo: string;
  installationId: number | undefined;
  offset: number;
  findingId: string | null | undefined;
  signal: AbortSignal | undefined;
  resolve: (value: RepoDetailResponse) => void;
  reject: (reason: unknown) => void;
};

let calls: DetailCall[];

function finding(id: string, title: string): RepoFindingItem {
  return {
    id,
    stable_id: id,
    severity: "P2",
    title,
    path: "app.py",
    line: 1,
    pull_request_number: 4,
  };
}

function detail(overrides: Partial<RepoDetailResponse> = {}): RepoDetailResponse {
  const openFindings = overrides.open_findings ?? [];
  return {
    full_name: "acme/repo",
    open_prs: [],
    open_findings: openFindings,
    open_findings_count: openFindings.length,
    limit: 50,
    offset: 0,
    ...overrides,
  };
}

function installDetailMock() {
  calls = [];
  api.getRepoDetail.mockImplementation(
    (
      _owner: string,
      repo: string,
      installationId?: number,
      _limit?: number,
      offset = 0,
      findingId?: string | null,
      signal?: AbortSignal
    ) => {
      let resolve!: (value: RepoDetailResponse) => void;
      let reject!: (reason: unknown) => void;
      const promise = new Promise<RepoDetailResponse>((res, rej) => {
        resolve = res;
        reject = rej;
      });
      calls.push({ repo, installationId, offset, findingId, signal, resolve, reject });
      return promise;
    }
  );
}

beforeEach(() => {
  nav.params = new URLSearchParams("space=1&repo=acme/repo&finding=find-9");
  Element.prototype.scrollIntoView = () => {};
  api.getMe.mockResolvedValue({ login: "root", role: "admin", kind: "root" });
  api.getSpaces.mockResolvedValue({
    items: [
      { id: 1, login: "acme", type: "Organization" },
      { id: 2, login: "other", type: "User" },
    ],
  });
  api.getSpaceRepos.mockResolvedValue({ items: [] });
  api.logout.mockResolvedValue(undefined);
  installDetailMock();
});

afterEach(() => {
  cleanup();
});

function findingCalls(id?: string) {
  return calls.filter((call) => (id === undefined ? Boolean(call.findingId) : call.findingId === id));
}

async function renderPage() {
  const view = render(<RepositoriesPage />);
  await screen.findByText("Searching for finding…");
  return view;
}

describe("finding deep link", () => {
  it("requests repo detail once after the searching re-render", async () => {
    await renderPage();
    expect(findingCalls("find-9")).toHaveLength(1);
    await act(async () => {
      await Promise.resolve();
    });
    expect(findingCalls("find-9")).toHaveLength(1);
    expect(calls[0]?.signal).toBeInstanceOf(AbortSignal);
    expect(calls[0]?.signal?.aborted).toBe(false);
  });

  it("highlights a finding already on the loaded page without another request", async () => {
    nav.params = new URLSearchParams("space=1&repo=acme/repo");
    const view = render(<RepositoriesPage />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.findingId).toBeFalsy();

    await act(async () => {
      calls[0]?.resolve(detail({ open_findings: [finding("find-9", "Already here")] }));
    });
    expect(await screen.findByText("Already here")).toBeTruthy();

    nav.params = new URLSearchParams("space=1&repo=acme/repo&finding=find-9");
    view.rerender(<RepositoriesPage />);

    await waitFor(() => {
      expect(document.getElementById("finding-find-9")?.className).toContain("row-highlight");
    });
    expect(screen.queryByText("Searching for finding…")).toBeNull();
    expect(findingCalls()).toHaveLength(0);
    expect(calls).toHaveLength(1);
  });

  it("aborts the in-flight lookup and ignores its response when the finding changes", async () => {
    const view = await renderPage();
    const first = calls[0];
    expect(first).toBeDefined();

    nav.params = new URLSearchParams("space=1&repo=acme/repo&finding=find-2");
    view.rerender(<RepositoriesPage />);
    await waitFor(() => expect(findingCalls("find-2")).toHaveLength(1));

    expect(first?.signal?.aborted).toBe(true);
    expect(findingCalls("find-9")).toHaveLength(1);

    await act(async () => {
      first?.resolve(detail({ open_findings: [finding("find-9", "Stale title")] }));
    });
    expect(screen.queryByText("Stale title")).toBeNull();

    await act(async () => {
      findingCalls("find-2")[0]?.resolve(detail({ open_findings: [finding("find-2", "Fresh title")] }));
    });
    expect(await screen.findByText("Fresh title")).toBeTruthy();
    expect(findingCalls("find-9")).toHaveLength(1);
    expect(findingCalls("find-2")).toHaveLength(1);
  });

  it("aborts the in-flight lookup when the repository changes", async () => {
    const view = await renderPage();
    const first = calls[0];

    nav.params = new URLSearchParams("space=1&repo=acme/other&finding=find-9");
    view.rerender(<RepositoriesPage />);

    await waitFor(() => expect(calls.some((call) => call.repo === "other")).toBe(true));
    expect(first?.signal?.aborted).toBe(true);
    expect(calls.filter((call) => call.repo === "repo" && call.findingId === "find-9")).toHaveLength(1);
    expect(calls.filter((call) => call.repo === "other" && call.findingId === "find-9")).toHaveLength(1);
  });

  it("aborts the in-flight lookup when the space changes", async () => {
    const view = await renderPage();
    const first = calls[0];

    nav.params = new URLSearchParams("space=2&repo=acme/repo&finding=find-9");
    view.rerender(<RepositoriesPage />);

    await waitFor(() => expect(calls.some((call) => call.installationId === 2)).toBe(true));
    expect(first?.signal?.aborted).toBe(true);
    expect(calls.filter((call) => call.installationId === 1 && call.findingId === "find-9")).toHaveLength(1);
    expect(calls.filter((call) => call.installationId === 2 && call.findingId === "find-9")).toHaveLength(1);
  });

  it("does not show an error or reload page 0 when the lookup is aborted", async () => {
    await renderPage();
    await act(async () => {
      calls[0]?.reject(Object.assign(new Error("The operation was aborted"), { name: "AbortError" }));
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.queryByText(/Failed to search/)).toBeNull();
    expect(calls.filter((call) => !call.findingId)).toHaveLength(0);
    expect(findingCalls("find-9")).toHaveLength(1);
  });

  it("marks the finding found when the lookup page contains it", async () => {
    await renderPage();
    await act(async () => {
      calls[0]?.resolve(
        detail({
          offset: 50,
          open_findings: [finding("find-9", "Target")],
          open_findings_count: 120,
        })
      );
    });

    expect(await screen.findByText("Target")).toBeTruthy();
    expect(document.getElementById("finding-find-9")?.className).toContain("row-highlight");
    expect(screen.queryByText(/was not found in open findings/)).toBeNull();
    expect(screen.queryByText(/Failed to search/)).toBeNull();
    expect(findingCalls("find-9")).toHaveLength(1);
  });

  it("reports not_found without treating it as an error", async () => {
    await renderPage();
    await act(async () => {
      calls[0]?.resolve(detail({ open_findings: [finding("other", "Other")] }));
    });

    expect(await screen.findByText(/was not found in open findings/)).toBeTruthy();
    expect(screen.queryByText(/Failed to search/)).toBeNull();
    expect(await screen.findByText("Other")).toBeTruthy();
    expect(calls).toHaveLength(1);
  });

  it("reports a lookup error and falls back to page 0", async () => {
    await renderPage();
    await act(async () => {
      calls[0]?.reject(new Error("github down"));
    });

    await waitFor(() => expect(calls.filter((call) => !call.findingId)).toHaveLength(1));
    expect(findingCalls("find-9")).toHaveLength(1);
    expect(calls.find((call) => !call.findingId)?.offset).toBe(0);

    await act(async () => {
      calls.find((call) => !call.findingId)?.resolve(detail());
    });
    expect(await screen.findByText(/Failed to search for finding/)).toBeTruthy();
    expect(screen.getByText("github down")).toBeTruthy();
    expect(screen.queryByText(/was not found in open findings/)).toBeNull();
  });

  it("does not repeat the lookup or reload its page when paginating after found", async () => {
    await renderPage();
    await act(async () => {
      calls[0]?.resolve(
        detail({
          offset: 50,
          open_findings: [finding("find-9", "Target")],
          open_findings_count: 120,
        })
      );
    });
    expect(await screen.findByText("Target")).toBeTruthy();
    expect(findingCalls("find-9")).toHaveLength(1);
    expect(calls.filter((call) => call.offset === 50 && !call.findingId)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(calls.some((call) => call.offset === 100 && !call.findingId)).toBe(true));
    expect(findingCalls("find-9")).toHaveLength(1);
    expect(calls.filter((call) => call.offset === 50 && !call.findingId)).toHaveLength(0);
  });
});
