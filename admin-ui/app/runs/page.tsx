"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getMe, type AdminMe } from "@/lib/api";
import { Shell } from "@/components/Shell";

export default function RunsPage() {
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
    <Shell user={me} pageTitle="Runs">
      <div className="card">
        <h2>Runs</h2>
        <p className="muted">
          Coming in a later release. Use the admin JSON API at <code>/api/v1/admin/runs</code> for
          now.
        </p>
      </div>
    </Shell>
  );
}
