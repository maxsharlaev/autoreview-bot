"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getMe, logout, type AdminMe } from "@/lib/api";

const NAV = [
  { href: "/findings/", label: "Findings" },
  { href: "/runs/", label: "Runs" },
  { href: "/queue/", label: "Queue" },
  { href: "/users/", label: "Users" },
] as const;

export default function ControlHomePage() {
  const router = useRouter();
  const [me, setMe] = useState<AdminMe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  async function onLogout() {
    setBusy(true);
    setError(null);
    try {
      await logout();
      router.replace("/login/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "logout failed");
      setBusy(false);
    }
  }

  if (!me) {
    return (
      <main>
        <p className="muted">Checking session…</p>
      </main>
    );
  }

  return (
    <>
      <header className="shell-header">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <nav>
            <strong style={{ marginRight: "1rem" }}>Control</strong>
            {NAV.map((item) => (
              <Link key={item.href} href={item.href}>
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="row">
            <span className="muted">
              {me.login} · {me.role}
            </span>
            <button type="button" className="secondary" disabled={busy} onClick={onLogout}>
              Log out
            </button>
          </div>
        </div>
      </header>
      <main>
        <div className="card">
          <h1>Control panel</h1>
          <p className="muted">
            Skeleton only. Findings, runs, queue and users pages are placeholders; the JSON API is already
            available under <code>/api/v1/admin</code>.
          </p>
          {error ? <p className="error">{error}</p> : null}
        </div>
      </main>
    </>
  );
}
