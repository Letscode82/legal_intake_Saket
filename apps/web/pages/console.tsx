/**
 * Backend Console — the flagship "one brain" loop running against the
 * FastAPI backend through the BFF proxy (pages/api/backend). This is the
 * reference screen for the monorepo split: no business logic here, no DB,
 * no Anthropic key — just calls to the typed backend client.
 *
 * It proves the whole path end-to-end in the real app: file via the Legal
 * Front Door → the agent's recommendation lands PENDING → a human
 * approves/rejects it in the Cockpit → Ask the Brain answers with citations.
 * Legacy screens are untouched; migrate them onto this same client
 * incrementally (see DEPLOYMENT.md).
 */
import Head from "next/head";
import { useCallback, useEffect, useState } from "react";

import { aegis, type AgentDecision } from "../lib/aegis-backend/client";

const card: React.CSSProperties = {
  background: "#0f1420",
  border: "1px solid #232b3d",
  borderRadius: 12,
  padding: 20,
  marginBottom: 20,
};
const label: React.CSSProperties = { fontSize: 12, color: "#8b96ad", marginBottom: 6 };
const input: React.CSSProperties = {
  width: "100%",
  background: "#070a12",
  border: "1px solid #2a3346",
  borderRadius: 8,
  color: "#e6ebf5",
  padding: "10px 12px",
  fontSize: 14,
};
const btn: React.CSSProperties = {
  background: "#3b6cf6",
  color: "white",
  border: "none",
  borderRadius: 8,
  padding: "9px 16px",
  fontSize: 13,
  cursor: "pointer",
  fontWeight: 600,
};
const ghost: React.CSSProperties = { ...btn, background: "transparent", border: "1px solid #2a3346", color: "#c7d0e0" };

export default function Console() {
  const [err, setErr] = useState<string | null>(null);

  // ── Front Door ─────────────────────────────────────────────
  const [desc, setDesc] = useState("We need an NDA with Meridian Biotech for a co-development discussion.");
  const [requester, setRequester] = useState("Dana Li");
  const [filed, setFiled] = useState<string | null>(null);

  // ── Cockpit ────────────────────────────────────────────────
  const [decisions, setDecisions] = useState<AgentDecision[]>([]);

  // ── Ask the Brain ──────────────────────────────────────────
  const [question, setQuestion] = useState("do we have an NDA with Acme?");
  const [answer, setAnswer] = useState<{ answer: string; citations: unknown[]; gap_notes: string[] } | null>(null);

  const run = useCallback(async (fn: () => Promise<void>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadDecisions = useCallback(
    () => run(async () => setDecisions(await aegis.pendingDecisions())),
    [run],
  );

  useEffect(() => {
    loadDecisions();
  }, [loadDecisions]);

  return (
    <>
      <Head>
        <title>AEGIS · Backend Console</title>
        <meta name="viewport" content="width=device-width,initial-scale=1" />
      </Head>
      <main style={{ maxWidth: 860, margin: "0 auto", padding: 24, color: "#e6ebf5", fontFamily: "ui-sans-serif, system-ui", background: "#070a12", minHeight: "100vh" }}>
        <h1 style={{ fontSize: 22, marginBottom: 4 }}>AEGIS Backend Console</h1>
        <p style={{ color: "#8b96ad", fontSize: 13, marginBottom: 20 }}>
          Live against the FastAPI backend via the BFF proxy
          (<code>/api/backend → {"{BACKEND_URL}"}/api/v1</code>).
        </p>
        {err && (
          <div style={{ ...card, borderColor: "#7a2a2a", color: "#ff9d9d" }}>Error: {err}</div>
        )}

        {/* Front Door */}
        <section style={card}>
          <h2 style={{ fontSize: 15, marginBottom: 12 }}>1 · Legal Front Door</h2>
          <div style={label}>Request (free text — routed deterministically)</div>
          <textarea style={{ ...input, minHeight: 60 }} value={desc} onChange={(e) => setDesc(e.target.value)} />
          <div style={{ display: "flex", gap: 10, marginTop: 10, alignItems: "flex-end" }}>
            <div style={{ flex: 1 }}>
              <div style={label}>Requester</div>
              <input style={input} value={requester} onChange={(e) => setRequester(e.target.value)} />
            </div>
            <button
              style={btn}
              onClick={() =>
                run(async () => {
                  const r = await aegis.frontDoor({ description: desc, requester_name: requester });
                  setFiled(`Routed to "${r.request_type}" · workflow ${r.workflow_instance_id}`);
                  await loadDecisions();
                })
              }
            >
              File request
            </button>
          </div>
          {filed && <p style={{ color: "#7fd18b", fontSize: 13, marginTop: 10 }}>{filed}</p>}
        </section>

        {/* Cockpit */}
        <section style={card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h2 style={{ fontSize: 15 }}>2 · Cockpit — pending decisions ({decisions.length})</h2>
            <button style={ghost} onClick={loadDecisions}>Refresh</button>
          </div>
          {decisions.length === 0 && <p style={{ color: "#8b96ad", fontSize: 13 }}>No pending recommendations.</p>}
          {decisions.map((d) => (
            <div key={d.id} style={{ borderTop: "1px solid #1b2233", paddingTop: 12, marginTop: 12 }}>
              <div style={{ fontSize: 13 }}>
                <strong>{d.agent_id}</strong> · {d.resource_type} {d.resource_id} · confidence{" "}
                {Math.round((d.recommendation?.confidence ?? 0) * 100)}%
              </div>
              <div style={{ color: "#c7d0e0", fontSize: 13, margin: "6px 0" }}>{d.recommendation?.reasoning}</div>
              {(d.recommendation?.concerns ?? []).map((c, i) => (
                <div key={i} style={{ color: "#f0c674", fontSize: 12 }}>⚠ {c}</div>
              ))}
              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                <button style={btn} onClick={() => run(async () => { await aegis.approveDecision(d.id); await loadDecisions(); })}>
                  Approve
                </button>
                <button style={ghost} onClick={() => run(async () => { await aegis.rejectDecision(d.id, "Rejected from console"); await loadDecisions(); })}>
                  Reject
                </button>
              </div>
            </div>
          ))}
        </section>

        {/* Ask the Brain */}
        <section style={card}>
          <h2 style={{ fontSize: 15, marginBottom: 12 }}>3 · Ask the Brain</h2>
          <div style={{ display: "flex", gap: 10 }}>
            <input style={input} value={question} onChange={(e) => setQuestion(e.target.value)} />
            <button style={btn} onClick={() => run(async () => setAnswer(await aegis.askBrain(question)))}>Ask</button>
          </div>
          {answer && (
            <div style={{ marginTop: 12 }}>
              <div style={{ color: "#e6ebf5", fontSize: 14, whiteSpace: "pre-wrap" }}>{answer.answer}</div>
              <div style={{ color: "#8b96ad", fontSize: 12, marginTop: 8 }}>
                Citations: {answer.citations.length} · Gaps: {answer.gap_notes.join(" ")}
              </div>
            </div>
          )}
        </section>
      </main>
    </>
  );
}
