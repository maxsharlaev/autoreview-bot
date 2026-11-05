import Link from "next/link";

export default function Page() {
  return (
    <main>
      <p>
        <Link href="/">← Control</Link>
      </p>
      <div className="card">
        <h1>Findings</h1>
        <p className="muted">Coming in a later PR. Use the admin JSON API for now.</p>
      </div>
    </main>
  );
}
