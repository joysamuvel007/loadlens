import React, { useEffect, useMemo, useState } from "react";
import { LineChart, fmtNum } from "./charts.jsx";
import { Card, Kpi, Badge, VerdictBadge, Legend } from "./ui.jsx";
import { api, words, when } from "./api.js";

export default function History({ onOpen }) {
    const [rows, setRows] = useState(null), [svc, setSvc] = useState("");
    useEffect(() => { api("/history").then(setRows).catch(() => setRows([])); }, []);
    const names = useMemo(() => [...new Set((rows || []).map((r) => r.name))], [rows]);
    const list = (rows || []).filter((r) => !svc || r.name === svc);
    const xs = list.map((_, i) => i + 1);
    const regress = list.filter((r) => r.regression).length;
    const last = list[list.length - 1], first = list[0];
    return (
        <>
            <div className="topbar"><div><div className="crumb">Over time</div><h1>Run history</h1></div>
                <select value={svc} onChange={(e) => setSvc(e.target.value)} className="btn" aria-label="Filter by service">
                    <option value="">All services</option>{names.map((n) => <option key={n}>{n}</option>)}
                </select></div>
            <div className="page">
                {rows == null && <div className="empty"><span className="spin" /></div>}
                {rows && list.length === 0 && <div className="card empty">No finished runs yet. Run a test and it will appear here.</div>}
                {list.length > 0 && (
                    <>
                        <div className="kpis">
                            <Kpi k="Runs" v={list.length} note={svc || "all services"} />
                            <Kpi k="Latest safe capacity" v={last.safe_capacity ?? "–"} unit="concurrent" note={first !== last && first.safe_capacity != null && last.safe_capacity != null ? `${last.safe_capacity - first.safe_capacity >= 0 ? "+" : ""}${last.safe_capacity - first.safe_capacity} since first run` : undefined} />
                            <Kpi k="Regressions flagged" v={regress} tone={regress ? "var(--crit)" : "var(--good)"} note="capacity lower than the previous run" />
                            <Kpi k="Latest worst p95" v={fmtNum(last.worst_p95, 0)} unit="ms" />
                        </div>
                        <div className="grid2">
                            <Card title="Safe capacity per run" sub="Higher is better" right={<Legend items={[{ name: "safe capacity", color: "var(--series-1)" }]} />}>
                                <LineChart xs={xs} xLabel="run number (oldest to newest)" unit="concurrent" series={[{ name: "safe capacity", color: "var(--series-1)", values: list.map((r) => r.safe_capacity) }]} />
                            </Card>
                            <Card title="Worst p95 per run" sub="Lower is better" right={<Legend items={[{ name: "worst p95 (ms)", color: "var(--series-2)" }]} />}>
                                <LineChart xs={xs} xLabel="run number (oldest to newest)" unit="ms" decimals={0} series={[{ name: "worst p95", color: "var(--series-2)", values: list.map((r) => r.worst_p95) }]} />
                            </Card>
                        </div>
                        <Card title="All runs" pad={false}>
                            <div className="tablewrap"><table>
                                <thead><tr><th>#</th><th>When</th><th>Service</th><th>Result</th><th>Safe capacity</th><th>Worst p95</th><th>Top suspect</th><th>Change applied</th><th /></tr></thead>
                                <tbody>{list.map((r, i) => (
                                    <tr key={r.id} className="click" onClick={() => onOpen(r.id)}>
                                        <td>{i + 1}</td><td>{when(r.created)}</td><td>{r.name}</td><td><VerdictBadge v={r.verdict} /></td>
                                        <td>{r.safe_capacity ?? "–"}</td><td>{fmtNum(r.worst_p95, 0)} ms</td><td>{words(r.top_suspect) || "–"}</td>
                                        <td>{r.fix_label || "–"}</td><td>{r.regression && <Badge tone="crit">Regression</Badge>}</td>
                                    </tr>))}</tbody>
                            </table></div>
                        </Card>
                    </>
                )}
            </div>
        </>
    );
}