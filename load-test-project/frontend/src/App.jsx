import React, { useCallback, useEffect, useState } from "react";
import { api, VERDICTS, when } from "./api.js";
import { Icon } from "./ui.jsx";
import NewTest from "./NewTest.jsx";
import Progress from "./Progress.jsx";
import RunView from "./RunView.jsx";
import History from "./History.jsx";

function useRuns() {
    const [runs, setRuns] = useState([]);
    const refresh = useCallback(() => api("/runs").then(setRuns).catch(() => { }), []);
    useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, [refresh]);
    return { runs, refresh };
}

export default function App() {
    const { runs, refresh } = useRuns();
    const [view, setView] = useState({ page: "new" });          // new | history | run
    const [status, setStatus] = useState(null);
    useEffect(() => { api("/models").then(setStatus).catch(() => setStatus({ ollama: { running: false, models: [] } })); }, []);

    const open = (id) => setView(id ? { page: "run", id } : { page: "new" });
    const started = (id, sla) => { setView({ page: "run", id, sla, live: true }); refresh(); };
    const running = view.page === "run" && runs.find((r) => r.id === view.id)?.status === "running";
    const o = status?.ollama;

    return (
        <div className="app">
            <aside className="side">
                <div className="brand">
                    <div className="logo"><Icon n="bolt" size={18} /></div>
                    <div><b>LoadLens</b><small>Load test result interpreter</small></div>
                </div>
                <nav className="nav">
                    <button className={view.page === "new" ? "on" : ""} onClick={() => setView({ page: "new" })}><Icon n="plus" /> New test</button>
                    <button className={view.page === "history" ? "on" : ""} onClick={() => setView({ page: "history" })}><Icon n="clock" /> History &amp; trends</button>
                </nav>
                <h6>Recent runs</h6>
                <div className="runs">
                    {runs.length === 0 && <div className="muted" style={{ padding: "6px 10px", color: "#6b7893" }}>No runs yet</div>}
                    {runs.map((r) => (
                        <button key={r.id} className={`run ${view.page === "run" && view.id === r.id ? "on" : ""}`} onClick={() => open(r.id)}>
                            <span className="dot" style={{ background: r.status === "running" ? "#6b8cff" : r.status === "error" ? "#d92d20" : VERDICTS[r.verdict]?.color || "#64748b" }} />
                            <span><div className="t">{r.name}</div><div className="s">{r.status === "running" ? "Running…" : r.fix_label ? `Fix: ${r.fix_label}` : when(r.created)}</div></span>
                        </button>
                    ))}
                </div>
                <div className="sidefoot">
                    <div className="row"><span className="dot" style={{ margin: 0, background: o == null ? "#64748b" : o.running ? "#12a150" : "#d98a00" }} />
                        {o == null ? "Checking Ollama…" : o.running ? `Ollama running · ${o.models.length} model${o.models.length === 1 ? "" : "s"}` : "Ollama not running · using built-in summary"}</div>
                </div>
            </aside>

            <div className="main">
                {view.page === "new" && <><div className="topbar"><div><div className="crumb">Start</div><h1>New test</h1></div></div><NewTest status={status} onStarted={started} /></>}
                {view.page === "history" && <History onOpen={open} />}
                {view.page === "run" && (running
                    ? <><div className="topbar"><div><div className="crumb">In progress</div><h1>{runs.find((r) => r.id === view.id)?.name || "Run"}</h1></div></div>
                        <Progress key={view.id} id={view.id} sla={view.sla || runs.find((r) => r.id === view.id)?.sla_ms} onDone={refresh} /></>
                    : <RunView key={view.id} id={view.id} refresh={refresh} onStarted={started} onOpen={open} />)}
            </div>
        </div>
    );
}