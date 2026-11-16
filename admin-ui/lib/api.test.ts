import { afterEach, describe, expect, it, vi } from "vitest";

import { getRepoDetail } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getRepoDetail", () => {
  it("forwards AbortSignal and finding_id to fetch", async () => {
    const body = {
      full_name: "acme/repo",
      open_prs: [],
      open_prs_count: 0,
      open_prs_has_next: false,
      pr_limit: 50,
      pr_offset: 0,
      open_findings: [],
      open_findings_count: 0,
      limit: 50,
      offset: 0,
    };
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await getRepoDetail("acme", "repo", 7, 50, 0, "find-9", controller.signal);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/repos/acme/repo?");
    expect(url).toContain("finding_id=find-9");
    expect(url).toContain("installation_id=7");
    expect(url).toContain("pr_limit=50");
    expect(url).toContain("pr_offset=0");
    expect(init.signal).toBe(controller.signal);
  });
});
