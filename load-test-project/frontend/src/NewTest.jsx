import React, { useEffect, useState } from "react";
import { Card, Icon } from "./ui.jsx";
import { api, postJson, words } from "./api.js";

export function ModelSelect({ value, onChange, status }) {
    const o = status?.ollama;
    const installed = o?.models || [];
    const missing = value !== "mock" && o?.running && !installed.includes(value);
    return (
        <div className="f">
            <label htmlFor="model">Explanation model</label>
            <select id="model" value={value} onChange={(e) => onChange(e.target.value)}>
                <option value="mock">Built-in summary (instant, no model needed)</option>
                {installed.map((m) => <option key={m} value={m}>Ollama: {m}</option>)}
                {!installed.includes("qwen3:8b") && <option value="qwen3:8b">Ollama: qwen3:8b</option>}
            </select>
            <small>
                {o == null ? "Checking Ollama…"
                    : !o.running ? "Ollama is not running. If you pick a model, the built-in summary is used instead."
                        : missing ? `That model is not installed. Run: ollama pull ${value}`
                            : value === "mock" ? "The measured evidence is always computed by code; the model only rewords it."
                                : "The model only rewords computed evidence. If it fails, the built-in summary is used."}
            </small>
        </div>
    );
}

const Field = ({ label, hint, wide, children }) => (
    <div className={`f ${wide ? "wide" : ""}`}><label>{label}</label>{children}{hint && <small>{hint}</small>}</div>
);

const blankEp = () => ({ name: "", url: "http://127.0.0.1:8000/", method: "GET", body: "", weight: 1 });

function EndpointEditor({ eps, setEps }) {
    const upd = (i, k, v) => setEps(eps.map((e, j) => (j === i ? { ...e, [k]: v } : e)));
    const total = eps.reduce((a, e) => a + (+e.weight || 0), 0) || 1;
    return (
        <div style={{ display: "grid", gap: 10 }}>
            {eps.map((e, i) => (
                <div key={i} className="epRow">
                    <div className="f"><label>Name (optional)</label><input value={e.name} onChange={(x) => upd(i, "name", x.target.value)} placeholder={`endpoint ${i + 1}`} /></div>
                    <div className="f"><label>Method</label>
                        <select value={e.method} onChange={(x) => upd(i, "method", x.target.value)}>{["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m}>{m}</option>)}</select></div>
                    <div className="f epUrl"><label>URL</label><input value={e.url} onChange={(x) => upd(i, "url", x.target.value)} /></div>
                    <div className="f"><label>Traffic share</label>
                        <input type="number" min="1" max="100" value={e.weight} onChange={(x) => upd(i, "weight", x.target.value)} />
                        <small>{Math.round(((+e.weight || 0) / total) * 100)}% of requests</small></div>
                    <div className="epDel">{eps.length > 1 && <button className="btn" onClick={() => setEps(eps.filter((_, j) => j !== i))} aria-label="Remove endpoint">Remove</button>}</div>
                    {e.method !== "GET" && <div className="f epBody"><label>Request body (JSON, optional)</label>
                        <textarea rows={2} value={e.body} onChange={(x) => upd(i, "body", x.target.value)} /></div>}
                </div>
            ))}
            {eps.length < 8 && <div><button className="btn" onClick={() => setEps([...eps, blankEp()])}>Add another endpoint</button></div>}
        </div>
    );
}

function LiveForm({ status, onStarted, init }) {
    const [eps, setEps] = useState([blankEp()]);
    const [f, setF] = useState({
        name: "my-service", sla_ms: 500,
        start: 2, step: 3, max: 30, rounds: 4, delay: 0.5, metrics_url: "", target_port: "", model: "mock", confirm: false, ...init,
    });
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
    const levels = Math.max(0, Math.floor((f.max - f.start) / Math.max(1, f.step)) + 1);
    const total = Array.from({ length: levels }, (_, i) => +f.start + i * +f.step).reduce((a, b) => a + b, 0) * f.rounds;

    const go = async () => {
        setBusy(true); setErr(null);
        try {
            const r = await postJson("/tests", {
                ...f, endpoints: eps.map((e) => ({ ...e, weight: +e.weight || 1, body: e.body || null })), sla_ms: +f.sla_ms, start: +f.start, step: +f.step, max: +f.max, rounds: +f.rounds, delay: +f.delay,
                body: f.body || null, metrics_url: f.metrics_url || null, target_port: f.target_port ? +f.target_port : null,
            });
            onStarted(r.id, +f.sla_ms);
        } catch (e) { setErr(e.message); setBusy(false); }
    };
    return (
        <div className="form">
            <div className="sect">Target</div>
            <div className="fgrid">
                <Field label="Service name"><input value={f.name} onChange={set("name")} /></Field>
                <Field label="Latency target (p95, ms)" hint="Responses slower than this count as a violation"><input type="number" value={f.sla_ms} onChange={set("sla_ms")} /></Field>
            </div>
            <div className="sect">Endpoints to test</div>
            <p className="muted">One endpoint tests a single URL. Add more to send a realistic mix: each load level splits its requests across the endpoints by traffic share, and every endpoint gets its own result. All endpoints must be on the same server.</p>
            <EndpointEditor eps={eps} setEps={setEps} />
            <div className="sect">Load ramp</div>
            <div className="fgrid">
                <Field label="Start at"><input type="number" value={f.start} onChange={set("start")} /></Field>
                <Field label="Step"><input type="number" value={f.step} onChange={set("step")} /></Field>
                <Field label="Up to"><input type="number" value={f.max} onChange={set("max")} /></Field>
                <Field label="Rounds per level"><input type="number" value={f.rounds} onChange={set("rounds")} /></Field>
                <Field label="Pause between levels (s)"><input type="number" step="0.1" value={f.delay} onChange={set("delay")} /></Field>
            </div>
            <div className="muted">{levels} load levels, about {total.toLocaleString()} requests in total.</div>
            <div className="sect">Evidence sources (optional)</div>
            <div className="fgrid">
                <Field label="App metrics URL" hint="JSON with a “counters” object, for pools and queues"><input value={f.metrics_url} onChange={set("metrics_url")} placeholder="http://127.0.0.1:8000/metrics" /></Field>
                <Field label="Server port override" hint="Only if the port is not detected from the URL"><input type="number" value={f.target_port} onChange={set("target_port")} /></Field>
                <ModelSelect value={f.model} onChange={(m) => setF({ ...f, model: m })} status={status} />
            </div>
            <label className="check"><input type="checkbox" checked={f.confirm} onChange={set("confirm")} />
                <span>I am allowed to load-test this target. Process metrics (CPU, memory, threads) are recorded only for local targets.</span></label>
            {err && <div className="banner crit">{err}</div>}
            <div><button className="btn primary" disabled={busy || !f.confirm} onClick={go}><Icon n="play" size={14} /> Start load test</button></div>
        </div>
    );
}

function UploadForm({ status, onStarted }) {
    const [lt, setLt] = useState(null), [vt, setVt] = useState(null);
    const [name, setName] = useState("uploaded-run"), [sla, setSla] = useState(500), [model, setModel] = useState("mock");
    const [err, setErr] = useState(null), [busy, setBusy] = useState(false);
    const go = async () => {
        setBusy(true); setErr(null);
        const fd = new FormData();
        fd.append("loadtest", lt); fd.append("vitals", vt); fd.append("name", name); fd.append("sla_ms", sla); fd.append("model", model);
        try { const r = await api("/runs", { method: "POST", body: fd }); onStarted(r.id, +sla); }
        catch (e) { setErr(e.message); setBusy(false); }
    };
    return (
        <div className="form">
            <div className="fgrid">
                <Field label="Run name"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
                <Field label="Latency target (p95, ms)"><input type="number" value={sla} onChange={(e) => setSla(e.target.value)} /></Field>
                <ModelSelect value={model} onChange={setModel} status={status} />
                <Field label="Load test CSV" hint="Output of ramp_test.py"><input type="file" accept=".csv" onChange={(e) => setLt(e.target.files[0])} /></Field>
                <Field label="Server vitals CSV" hint="Output of metrics_collector.py"><input type="file" accept=".csv" onChange={(e) => setVt(e.target.files[0])} /></Field>
            </div>
            {err && <div className="banner crit">{err}</div>}
            <div><button className="btn primary" disabled={busy || !lt || !vt} onClick={go}><Icon n="play" size={14} /> Analyse files</button></div>
        </div>
    );
}

function Samples({ status, onStarted }) {
    const [list, setList] = useState([]);
    const [sla, setSla] = useState(100), [model, setModel] = useState("mock"), [err, setErr] = useState(null);
    useEffect(() => { api("/samples").then(setList).catch((e) => setErr(e.message)); }, []);
    const go = (s) => api(`/samples/${s}?sla_ms=${sla}&model=${encodeURIComponent(model)}`, { method: "POST" })
        .then((r) => onStarted(r.id, +sla)).catch((e) => setErr(e.message));
    return (
        <div className="form">
            <div className="fgrid">
                <Field label="Latency target (p95, ms)"><input type="number" value={sla} onChange={(e) => setSla(e.target.value)} /></Field>
                <ModelSelect value={model} onChange={setModel} status={status} />
            </div>
            <div className="sect">Recorded runs with a known cause</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {list.map((s) => <button key={s} className="btn" onClick={() => go(s)}>{words(s)}</button>)}
            </div>
            {err && <div className="banner crit">{err}</div>}
        </div>
    );
}

export default function NewTest({ status, onStarted }) {
    const [tab, setTab] = useState("live");
    return (
        <div className="page">
            <div className="tabs" role="tablist">
                {[["live", "Live load test"], ["upload", "Upload CSV files"], ["samples", "Sample runs"]].map(([k, l]) =>
                    <button key={k} role="tab" className={tab === k ? "on" : ""} aria-selected={tab === k} onClick={() => setTab(k)}>{l}</button>)}
            </div>
            <Card title={tab === "live" ? "Stress-test a service" : tab === "upload" ? "Analyse existing recordings" : "Try a recorded sample"}
                sub="LoadLens measures what happened, finds what was limiting the service, and only then explains it.">
                {tab === "live" && <LiveForm status={status} onStarted={onStarted} />}
                {tab === "upload" && <UploadForm status={status} onStarted={onStarted} />}
                {tab === "samples" && <Samples status={status} onStarted={onStarted} />}
            </Card>
        </div>
    );
}