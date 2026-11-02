import React, { useEffect, useState } from "react";
import { LineChart } from "./charts.jsx";
import { Card, Legend, Icon } from "./ui.jsx";
import { api } from "./api.js";

export default function Progress({ id, sla, onDone }) {
    const [stages, setStages] = useState([]);
    const [notes, setNotes] = useState([]);
    const [steps, setSteps] = useState([]);
    const [total, setTotal] = useState(0);
    const [err, setErr] = useState(null);
    const [stopping, setStopping] = useState(false);

    useEffect(() => {
        const es = new EventSource(`/api/runs/${id}/events`);
        es.onmessage = (m) => {
            const ev = JSON.parse(m.data);
            if (ev.type === "stage") setStages((s) => [...s, ev.label]);
            if (ev.type === "note") setNotes((n) => [...n, ev]);
            if (ev.type === "step") { setSteps((s) => [...s, ev]); setTotal(ev.total); }
            if (ev.type === "done") { es.close(); onDone(); }
            if (ev.type === "error") { es.close(); setErr(ev.message); onDone(); }
        };
        es.onerror = () => { es.close(); onDone(); };
        return () => es.close();
    }, [id]);

    const last = steps[steps.length - 1];
    const pct = total ? Math.round((steps.length / total) * 100) : 0;
    const analysing = stages.some((s) => s === "Computing evidence");
    return (
        <div className="page" aria-live="polite">
            {notes.map((n, i) => <div key={i} className={`banner ${n.level === "warn" ? "warn" : "info"}`}>{n.text}</div>)}
            <Card title={total && !analysing ? "Load test running" : "Analysing results"}
                sub={last ? `Level ${steps.length} of ${total}: ${last.concurrency} concurrent requests, p95 ${last.p95_ms} ms, ${Math.round(last.error_rate * 100)}% failed` : "Starting up"}
                right={total > 0 && !stopping && !analysing && (
                    <button className="btn danger" onClick={() => { setStopping(true); api(`/runs/${id}/cancel`, { method: "POST" }); }}>
                        <Icon n="stop" size={14} /> Stop and analyse what was collected
                    </button>)}>
                {total > 0 && <div className="bar" style={{ marginBottom: 16 }}><i style={{ width: `${pct}%` }} /></div>}
                {steps.length > 0 && (
                    <>
                        <div className="legend" style={{ marginBottom: 6 }}><Legend items={[{ name: "p95 latency (ms)", color: "var(--series-1)" }]} /></div>
                        <LineChart xs={steps.map((s) => s.concurrency)} unit="ms"
                            series={[{ name: "p95 latency", color: "var(--series-1)", values: steps.map((s) => s.p95_ms) }]}
                            refLine={sla ? { value: sla, label: `target ${sla} ms` } : null} />
                    </>
                )}
            </Card>
            <Card title="Pipeline">
                <ul className="steps">
                    {stages.map((s, i) => <li key={i}><Icon n="check" /> {s}</li>)}
                    {!err && <li><span className="spin" /> Working…</li>}
                </ul>
                {err && <p className="err">{err}</p>}
            </Card>
        </div>
    );
}