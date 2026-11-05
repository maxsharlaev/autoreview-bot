"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useState } from "react";
import { logout, type AdminMe } from "@/lib/api";

const NAV_ITEMS = [
  { href: "/findings/", label: "Findings" },
  { href: "/runs/", label: "Runs" },
  { href: "/queue/", label: "Queue" },
  { href: "/users/", label: "Users" },
] as const;

interface ShellProps {
  children: ReactNode;
  user: AdminMe;
  pageTitle: string;
}

export function Shell({ children, user, pageTitle }: ShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const isActive = (href: string) => {
    if (href === "/") return pathname === "/" || pathname === "";
    return pathname.startsWith(href.replace(/\/$/, ""));
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">Control</div>
        <nav className="sidebar-nav">
          <Link
            href="/"
            className={`sidebar-link${pathname === "/" || pathname === "" ? " active" : ""}`}
          >
            Dashboard
          </Link>
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`sidebar-link${isActive(item.href) ? " active" : ""}`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-user">
            <span className="sidebar-user-name">{user.login}</span>
            <span className="sidebar-user-role">{user.role}</span>
          </div>
          <button
            type="button"
            className="logout-btn"
            disabled={busy}
            onClick={onLogout}
          >
            {busy ? "Logging out…" : "Log out"}
          </button>
          {error && <p className="error">{error}</p>}
        </div>
      </aside>
      <main className="main-content">
        <header className="topbar">
          <h1 className="topbar-title">{pageTitle}</h1>
          <span className="topbar-badge">local</span>
        </header>
        <div className="content-area">{children}</div>
      </main>
    </div>
  );
}
