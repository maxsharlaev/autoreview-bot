"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getMe, type AdminMe } from "@/lib/api";
import { Shell } from "@/components/Shell";

export default function ControlHomePage() {
  const router = useRouter();
  const [me, setMe] = useState<AdminMe | null>(null);

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

  if (!me) {
    return (
      <div className="login-container">
        <p className="muted">Checking session…</p>
      </div>
    );
  }

  return (
    <Shell user={me} pageTitle="Dashboard">
      <div className="card-grid">
        <div className="card">
          <h2>Welcome to Control</h2>
          <p className="muted">
            This is the operator control panel for Open PR Review. Navigate using the sidebar to
            manage findings, view runs, monitor the queue, or manage users.
          </p>
        </div>
        <div className="card">
          <h2>API Access</h2>
          <p className="muted">
            The admin JSON API is available at <code>/api/v1/admin</code>. All UI pages are
            placeholders that will connect to this API in upcoming releases.
          </p>
        </div>
      </div>
    </Shell>
  );
}
