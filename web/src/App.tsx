import { useEffect, useState } from "react";
import { createApplication } from "./api";
import { Wizard } from "./Wizard";
import { TemporalMark } from "./TemporalMark";
import temporalLogo from "./assets/temporal-logo.svg";

// The application id lives in the URL (`?app=<id>`), so a bookmark or a shared
// link resumes the exact same Workflow. The interface keeps no id of its own.
function readIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("app");
}

export function App() {
  const [applicationId, setApplicationId] = useState<string | null>(readIdFromUrl);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keep the id in sync with the browser's back/forward navigation.
  useEffect(() => {
    const onPop = () => setApplicationId(readIdFromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      const id = await createApplication();
      const url = `${window.location.pathname}?app=${id}`;
      window.history.pushState({ app: id }, "", url);
      setApplicationId(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  const reset = () => {
    window.history.pushState({}, "", window.location.pathname);
    setApplicationId(null);
  };

  return (
    <div className="page">
      <header className="topbar">
        <div className="topbar-brand">
          <img src={temporalLogo} alt="Temporal" className="topbar-logo" />
          <span className="topbar-divider" aria-hidden="true" />
          <span className="topbar-app">Loan Wizard</span>
        </div>
        {applicationId && (
          <button className="ghost-btn" onClick={reset}>
            Start over
          </button>
        )}
      </header>

      <main className="content">
        {applicationId ? (
          <Wizard applicationId={applicationId} />
        ) : (
          <section className="hero">
            <div className="hero-mark">
              <TemporalMark className="hero-mark-svg" />
            </div>
            <h1 className="hero-title">Apply for a loan</h1>
            <p className="hero-lede">
              Four short steps. Your progress saves automatically and survives a
              refresh — pick up exactly where you left off, backed by a durable
              Temporal Workflow.
            </p>
            <button className="primary lg" onClick={start} disabled={starting}>
              {starting ? "Starting…" : "Start a new application"}
            </button>
            {error && <p className="error-text">{error}</p>}
            <ul className="hero-points">
              <li>Resumable by link — no session store</li>
              <li>Every step validated server-side</li>
              <li>Autosaves as you type</li>
            </ul>
          </section>
        )}
      </main>

      <footer className="footer">
        Durable execution demo · powered by{" "}
        <a href="https://temporal.io" target="_blank" rel="noreferrer">
          Temporal
        </a>
      </footer>
    </div>
  );
}
