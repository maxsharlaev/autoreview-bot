"use client";

import { useEffect, useState, useCallback, useRef, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  getMe,
  getSpaces,
  getSpaceRepos,
  getRepoDetail,
  type AdminMe,
  type SpaceItem,
  type RepoItem,
  type RepoDetailResponse,
  type RepoPRItem,
  type RepoFindingItem,
} from "@/lib/api";
import { Shell } from "@/components/Shell";

type LoadState = "idle" | "loading" | "loaded" | "error";
type FindingSearchState = "idle" | "searching" | "found" | "not_found" | "error";

const FINDINGS_LIMIT = 50;

function RepositoriesContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [me, setMe] = useState<AdminMe | null>(null);
  const [spaces, setSpaces] = useState<SpaceItem[]>([]);
  const [spacesState, setSpacesState] = useState<LoadState>("idle");
  const [spacesError, setSpacesError] = useState<string | null>(null);

  const [selectedSpace, setSelectedSpace] = useState<SpaceItem | null>(null);
  const [repos, setRepos] = useState<RepoItem[]>([]);
  const [reposState, setReposState] = useState<LoadState>("idle");
  const [reposError, setReposError] = useState<string | null>(null);
  const [hideEmptyPrs, setHideEmptyPrs] = useState(true);

  const selectedRepo = searchParams.get("repo");
  const highlightFindingId = searchParams.get("finding");
  const [repoDetail, setRepoDetail] = useState<RepoDetailResponse | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [detailError, setDetailError] = useState<string | null>(null);
  const [findingsOffset, setFindingsOffset] = useState(0);
  const [findingSearchState, setFindingSearchState] = useState<FindingSearchState>("idle");
  const [findingSearchError, setFindingSearchError] = useState<string | null>(null);

  const reposRequestGen = useRef(0);
  const detailRequestGen = useRef(0);
  const findingSearchGen = useRef(0);
  const lastSearchedFindingKey = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMe()
      .then((principal) => {
        if (!cancelled) setMe(principal);
      })
      .catch(() => {
        if (!cancelled) router.replace("/login/");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  useEffect(() => {
    if (!me) return;
    let cancelled = false;
    setSpacesState("loading");
    setSpacesError(null);
    getSpaces()
      .then((res) => {
        if (cancelled) return;
        setSpaces(res.items);
        setSpacesState("loaded");
        const spaceIdParam = searchParams.get("space");
        if (spaceIdParam) {
          const found = res.items.find((s) => String(s.id) === spaceIdParam);
          if (found) {
            setSelectedSpace(found);
          } else if (res.items.length > 0) {
            setSelectedSpace(res.items[0]);
          }
        } else if (res.items.length > 0) {
          setSelectedSpace(res.items[0]);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setSpacesError(err instanceof Error ? err.message : "Failed to load spaces");
        setSpacesState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [me, searchParams]);

  const loadRepos = useCallback(
    (space: SpaceItem, hideEmpty: boolean, requestGen: number) => {
      setReposState("loading");
      setReposError(null);
      getSpaceRepos(space.id, hideEmpty)
        .then((res) => {
          if (requestGen !== reposRequestGen.current) return;
          setRepos(res.items);
          setReposState("loaded");
        })
        .catch((err) => {
          if (requestGen !== reposRequestGen.current) return;
          setReposError(err instanceof Error ? err.message : "Failed to load repositories");
          setReposState("error");
        });
    },
    []
  );

  useEffect(() => {
    if (!selectedSpace || selectedRepo) return;
    reposRequestGen.current += 1;
    const currentGen = reposRequestGen.current;
    loadRepos(selectedSpace, hideEmptyPrs, currentGen);
  }, [selectedSpace, hideEmptyPrs, loadRepos, selectedRepo]);

  const loadRepoDetail = useCallback(
    (fullName: string, offset: number, requestGen: number) => {
      if (!selectedSpace) return;
      setDetailState("loading");
      setDetailError(null);
      const [owner, repo] = fullName.split("/");
      getRepoDetail(owner, repo, selectedSpace.id, FINDINGS_LIMIT, offset)
        .then((res) => {
          if (requestGen !== detailRequestGen.current) return;
          setRepoDetail(res);
          setDetailState("loaded");
        })
        .catch((err) => {
          if (requestGen !== detailRequestGen.current) return;
          setDetailError(err instanceof Error ? err.message : "Failed to load repository details");
          setDetailState("error");
        });
    },
    [selectedSpace]
  );

  useEffect(() => {
    if (!me || !selectedSpace || !selectedRepo) return;
    if (findingSearchState === "searching") return;
    if (highlightFindingId && findingSearchState === "idle") return;
    if (
      repoDetail &&
      repoDetail.full_name === selectedRepo &&
      repoDetail.offset === findingsOffset
    ) {
      return;
    }
    detailRequestGen.current += 1;
    const currentGen = detailRequestGen.current;
    loadRepoDetail(selectedRepo, findingsOffset, currentGen);
  }, [me, selectedSpace, selectedRepo, findingsOffset, loadRepoDetail, findingSearchState, highlightFindingId, repoDetail]);

  useEffect(() => {
    const currentKey = selectedRepo && selectedSpace && highlightFindingId
      ? `${selectedSpace.id}:${selectedRepo}:${highlightFindingId}`
      : null;

    if (currentKey !== lastSearchedFindingKey.current) {
      if (lastSearchedFindingKey.current !== null || currentKey !== null) {
        setFindingSearchState("idle");
        setFindingSearchError(null);
        findingSearchGen.current += 1;
      }
      lastSearchedFindingKey.current = currentKey;
    }

    if (!me || !selectedSpace || !selectedRepo || !highlightFindingId) {
      return;
    }

    if (repoDetail) {
      const foundInCurrent = repoDetail.open_findings.some((f) => f.id === highlightFindingId);
      if (foundInCurrent) {
        setFindingSearchState("found");
        return;
      }
    }

    if (findingSearchState === "found" || findingSearchState === "not_found" || findingSearchState === "error") {
      return;
    }

    findingSearchGen.current += 1;
    const currentSearchGen = findingSearchGen.current;
    setFindingSearchState("searching");
    setFindingSearchError(null);

    const [owner, repo] = selectedRepo.split("/");

    async function searchForFinding() {
      if (currentSearchGen !== findingSearchGen.current) return;

      try {
        const res = await getRepoDetail(
          owner,
          repo,
          selectedSpace!.id,
          FINDINGS_LIMIT,
          0,
          highlightFindingId
        );

        if (currentSearchGen !== findingSearchGen.current) return;

        detailRequestGen.current += 1;
        setFindingsOffset(res.offset);
        setRepoDetail(res);
        setDetailState("loaded");
        const found = res.open_findings.some((f) => f.id === highlightFindingId);
        setFindingSearchState(found ? "found" : "not_found");
      } catch (err) {
        if (currentSearchGen !== findingSearchGen.current) return;
        setFindingSearchError(err instanceof Error ? err.message : "Search failed");
        setFindingSearchState("error");
        setFindingsOffset(0);
        detailRequestGen.current += 1;
        const gen = detailRequestGen.current;
        loadRepoDetail(selectedRepo!, 0, gen);
      }
    }

    searchForFinding();

    return () => {
      findingSearchGen.current += 1;
    };
  }, [me, selectedSpace, selectedRepo, highlightFindingId, repoDetail, findingSearchState, loadRepoDetail]);

  useEffect(() => {
    if (findingSearchState === "found" && highlightFindingId && repoDetail) {
      const element = document.getElementById(`finding-${highlightFindingId}`);
      if (element) {
        element.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }
  }, [findingSearchState, highlightFindingId, repoDetail]);

  useEffect(() => {
    if (!selectedRepo) {
      setRepoDetail(null);
      setDetailState("idle");
      setDetailError(null);
      setFindingsOffset(0);
      setFindingSearchState("idle");
      setFindingSearchError(null);
      lastSearchedFindingKey.current = null;
    }
  }, [selectedRepo]);

  function handleSpaceChange(event: React.ChangeEvent<HTMLSelectElement>) {
    const id = Number(event.target.value);
    const space = spaces.find((s) => s.id === id);
    if (space) {
      setSelectedSpace(space);
      const params = new URLSearchParams();
      params.set("space", String(space.id));
      router.replace(`?${params.toString()}`);
    }
  }

  function handleToggleHideEmpty() {
    setHideEmptyPrs((prev) => !prev);
  }

  function formatDate(dateStr: string | null): string {
    if (!dateStr) return "—";
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return "—";
    }
  }

  function findingsTotal(f: RepoItem["open_findings"]): number {
    return f.P0 + f.P1 + f.P2 + f.P3;
  }

  function handleRepoClick(fullName: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("repo", fullName);
    router.push(`?${params.toString()}`);
  }

  function handleBackToList() {
    const params = new URLSearchParams();
    if (selectedSpace) {
      params.set("space", String(selectedSpace.id));
    }
    router.push(`?${params.toString()}`);
  }

  function runStatusClass(status: string | null): string {
    if (!status) return "";
    if (status === "completed") return "status-success";
    if (status === "error" || status === "failed") return "status-danger";
    if (status === "skipped") return "status-warn";
    return "";
  }

  function severityClass(sev: string): string {
    if (sev === "P0") return "sev-p0";
    if (sev === "P1") return "sev-p1";
    if (sev === "P2") return "sev-p2";
    if (sev === "P3") return "sev-p3";
    return "";
  }

  function handlePrevPage() {
    setFindingsOffset((prev) => Math.max(0, prev - FINDINGS_LIMIT));
  }

  function handleNextPage() {
    if (repoDetail && findingsOffset + FINDINGS_LIMIT < repoDetail.open_findings_count) {
      setFindingsOffset((prev) => prev + FINDINGS_LIMIT);
    }
  }

  if (!me) {
    return (
      <div className="login-container">
        <p className="muted">Checking session…</p>
      </div>
    );
  }

  if (selectedRepo) {
    const isSearchingFinding = findingSearchState === "searching";
    const findingNotFound = findingSearchState === "not_found" && highlightFindingId;
    const findingSearchFailed = findingSearchState === "error" && highlightFindingId;

    return (
      <Shell user={me} pageTitle={selectedRepo}>
        <div className="breadcrumb">
          <button type="button" className="link-button" onClick={handleBackToList}>
            ← Back to Repositories
          </button>
        </div>

        {isSearchingFinding && (
          <div className="card">
            <p className="muted">Searching for finding…</p>
          </div>
        )}

        {detailState === "loading" && !repoDetail && !isSearchingFinding && (
          <div className="card">
            <p className="muted">Loading repository details…</p>
          </div>
        )}

        {detailState === "error" && (
          <div className="card card-error">
            <h2>Error loading repository</h2>
            <p className="error">{detailError}</p>
          </div>
        )}

        {detailState === "loaded" && repoDetail && (
          <>
            {findingSearchFailed && (
              <div className="card card-error">
                <p className="muted">
                  Failed to search for finding <code>{highlightFindingId}</code>:{" "}
                  <span className="error">{findingSearchError}</span>
                </p>
                <p className="muted">
                  The finding may still exist. Try reloading the page.
                </p>
              </div>
            )}

            {findingNotFound && (
              <div className="card card-warn">
                <p className="muted">
                  Finding <code>{highlightFindingId}</code> was not found in open findings. It may
                  have been resolved or suppressed.
                </p>
              </div>
            )}

            <section className="section">
              <h2 className="section-title">
                Open Pull Requests ({repoDetail.open_prs.length})
              </h2>
              {repoDetail.open_prs.length === 0 ? (
                <div className="card">
                  <p className="muted">No open pull requests in this repository.</p>
                </div>
              ) : (
                <div className="table-container">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>Title</th>
                        <th>Head</th>
                        <th>Last Run</th>
                      </tr>
                    </thead>
                    <tbody>
                      {repoDetail.open_prs.map((pr: RepoPRItem) => (
                        <tr key={pr.number}>
                          <td className="mono">
                            <a
                              href={`https://github.com/${selectedRepo}/pull/${pr.number}`}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              #{pr.number}
                            </a>
                          </td>
                          <td>{pr.title}</td>
                          <td className="mono muted">{pr.head_sha_short || "—"}</td>
                          <td>
                            {pr.last_run_status ? (
                              <span className={runStatusClass(pr.last_run_status)}>
                                {pr.last_run_status}
                              </span>
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="section">
              <h2 className="section-title">
                Open Findings ({repoDetail.open_findings_count})
              </h2>
              {repoDetail.open_findings_count === 0 ? (
                <div className="card">
                  <p className="muted">No open findings in this repository.</p>
                </div>
              ) : (
                <>
                  <div className="table-container">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Severity</th>
                          <th>Title</th>
                          <th>Path</th>
                          <th>PR</th>
                        </tr>
                      </thead>
                      <tbody>
                        {repoDetail.open_findings.map((finding: RepoFindingItem) => {
                          const isHighlighted = highlightFindingId === finding.id;
                          const rowClass = isHighlighted ? "row-highlight" : "";
                          return (
                            <tr
                              key={finding.id}
                              className={rowClass}
                              id={`finding-${finding.id}`}
                            >
                              <td>
                                <span
                                  className={`severity-badge ${severityClass(finding.severity)}`}
                                >
                                  {finding.severity}
                                </span>
                              </td>
                              <td>
                                <span className="finding-title">{finding.title}</span>
                                <div className="muted mono text-small">{finding.stable_id}</div>
                              </td>
                              <td className="mono muted">
                                {finding.path}
                                {finding.line !== null && `:${finding.line}`}
                              </td>
                              <td>
                                <a
                                  href={`https://github.com/${selectedRepo}/pull/${finding.pull_request_number}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="mono"
                                >
                                  #{finding.pull_request_number}
                                </a>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  {repoDetail.open_findings_count > FINDINGS_LIMIT && (
                    <div className="pagination">
                      <button
                        type="button"
                        className="secondary"
                        onClick={handlePrevPage}
                        disabled={findingsOffset === 0}
                      >
                        Previous
                      </button>
                      <span className="pagination-info">
                        {findingsOffset + 1}–
                        {Math.min(
                          findingsOffset + FINDINGS_LIMIT,
                          repoDetail.open_findings_count
                        )}{" "}
                        of {repoDetail.open_findings_count}
                      </span>
                      <button
                        type="button"
                        className="secondary"
                        onClick={handleNextPage}
                        disabled={
                          findingsOffset + FINDINGS_LIMIT >= repoDetail.open_findings_count
                        }
                      >
                        Next
                      </button>
                    </div>
                  )}
                </>
              )}
            </section>
          </>
        )}
      </Shell>
    );
  }

  return (
    <Shell user={me} pageTitle="Repositories">
      {spacesState === "loading" && (
        <div className="card">
          <p className="muted">Loading spaces…</p>
        </div>
      )}

      {spacesState === "error" && (
        <div className="card card-error">
          <h2>Error loading spaces</h2>
          <p className="error">{spacesError}</p>
          <p className="muted">
            The GitHub App installation enumeration failed. Check that the bot has valid App
            credentials and network access to GitHub.
          </p>
        </div>
      )}

      {spacesState === "loaded" && spaces.length === 0 && (
        <div className="card">
          <h2>No spaces found</h2>
          <p className="muted">
            No GitHub App installations are accessible to this bot. Install the GitHub App on at
            least one organization or user account.
          </p>
        </div>
      )}

      {spacesState === "loaded" && spaces.length > 0 && (
        <>
          <div className="toolbar">
            <div className="toolbar-left">
              <label htmlFor="space-select" className="toolbar-label">
                Space:
              </label>
              <select
                id="space-select"
                className="select"
                value={selectedSpace?.id ?? ""}
                onChange={handleSpaceChange}
              >
                {spaces.map((space) => (
                  <option key={space.id} value={space.id}>
                    {space.login} ({space.type})
                  </option>
                ))}
              </select>
            </div>
            <div className="toolbar-right">
              <label className="toggle-label">
                <input
                  type="checkbox"
                  checked={hideEmptyPrs}
                  onChange={handleToggleHideEmpty}
                  className="toggle-input"
                />
                <span className="toggle-text">Hide repos without open PRs</span>
              </label>
            </div>
          </div>

          {reposState === "loading" && (
            <div className="card">
              <p className="muted">Loading repositories…</p>
            </div>
          )}

          {reposState === "error" && (
            <div className="card card-error">
              <h2>Error loading repositories</h2>
              <p className="error">{reposError}</p>
            </div>
          )}

          {reposState === "loaded" && repos.length === 0 && (
            <div className="card">
              <h2>No repositories</h2>
              <p className="muted">
                {hideEmptyPrs
                  ? "No repositories with open PRs found in this space. Disable the filter to see all repositories."
                  : "No repositories found in this space."}
              </p>
            </div>
          )}

          {reposState === "loaded" && repos.length > 0 && (
            <div className="table-container">
              <table className="table">
                <thead>
                  <tr>
                    <th>Repository</th>
                    <th className="col-center">Open PRs</th>
                    <th className="col-center">P0</th>
                    <th className="col-center">P1</th>
                    <th className="col-center">P2</th>
                    <th className="col-center">P3</th>
                    <th className="col-center">Total</th>
                    <th>Last Run</th>
                  </tr>
                </thead>
                <tbody>
                  {repos.map((repo) => (
                    <tr key={repo.full_name}>
                      <td>
                        <button
                          type="button"
                          className="link-button repo-link"
                          onClick={() => handleRepoClick(repo.full_name)}
                        >
                          {repo.full_name}
                        </button>
                      </td>
                      <td className="col-center">
                        {repo.open_pr_count !== null ? (
                          repo.open_pr_count
                        ) : (
                          <span className="text-warn" title="GitHub API error">
                            —
                          </span>
                        )}
                      </td>
                      <td className="col-center">
                        <span className={repo.open_findings.P0 > 0 ? "sev-p0" : ""}>
                          {repo.open_findings.P0}
                        </span>
                      </td>
                      <td className="col-center">
                        <span className={repo.open_findings.P1 > 0 ? "sev-p1" : ""}>
                          {repo.open_findings.P1}
                        </span>
                      </td>
                      <td className="col-center">
                        <span className={repo.open_findings.P2 > 0 ? "sev-p2" : ""}>
                          {repo.open_findings.P2}
                        </span>
                      </td>
                      <td className="col-center">
                        <span className={repo.open_findings.P3 > 0 ? "sev-p3" : ""}>
                          {repo.open_findings.P3}
                        </span>
                      </td>
                      <td className="col-center">{findingsTotal(repo.open_findings)}</td>
                      <td className="muted">{formatDate(repo.last_run_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Shell>
  );
}

export default function RepositoriesPage() {
  return (
    <Suspense
      fallback={
        <div className="login-container">
          <p className="muted">Loading…</p>
        </div>
      }
    >
      <RepositoriesContent />
    </Suspense>
  );
}
