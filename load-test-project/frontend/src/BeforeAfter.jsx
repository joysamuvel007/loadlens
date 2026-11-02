import React, { useEffect, useState } from "react";
import { LineChart, fmtNum } from "./charts.jsx";
import { Card, Legend, Icon, Badge } from "./ui.jsx";
import { api, words } from "./api.js";

const OUT = {
    improved: ["good", "Improved", "Safe capacity went up after the change."],
    regressed: ["crit", "Regressed", "Safe capacity went down after the change."],
    unchanged: ["warn", "No change in safe capacity", "The change did not move the capacity limit that was measured."],
    unclear: ["", "Unclear", "One of the runs had no level that met the target, so capacity cannot be compared directly."],
};

const Delta = ({ b, a, unit = "", invert }) => {
    if (b == null || a == null) return <span className="muted">–</span>;
    const d = +(a - b).toFixed(1);
    const good = invert ? d < 0 : d > 0;
    return <span className={`delta ${d === 0 ? "" : good ? "up" : "down"}`}>{d > 0 ? "+" : ""}{d}{unit}</span>;
};

export default function BeforeAfter({ before, after, fixLabel, sla, onOpen }) {
    const [c, setC] = useState(null), [err, setErr] = useState(null);
    useEffect(() => { api(`/runs/${before}/compare/${after}`).then(setC).catch((e) => setErr(e.message)); }, [before, after]);
    if (err) return <div className="banner warn">{err}</div>;
    if (!c) return null;
    const [tone, title, note] = OUT[c.outcome];
    const xs = [...new Set([...c.before.steps, ...c.after.steps].map((s) => s.c))].sort((x, y) => x - y);
    const at = (run, x) => run.steps.find((s) => s.c === x)?.p95 ?? null;
    return (
        <Card title="Before and after" sub={fixLabel ? `Change applied: ${fixLabel}` : undefined}
            right={<span style={{ display: "flex", gap: 8, alignItems: "center" }}><Badge tone={tone}>{title}</Badge>
                <button className="btn" onClick={() => onOpen(before)}><Icon n="clock" size={14} /> Open previous run</button></span>}>
            <p className="muted" style={{ marginBottom: 12 }}>{note}</p>
            <div className="grid2">
                <div>
                    <table>
                        <thead><tr><th></th><th>Before</th><th>After</th><th>Change</th></tr></thead>
                        <tbody>
                            <tr><td>Safe capacity (concurrent)</td><td>{c.before.safe_capacity ?? "–"}</td><td>{c.after.safe_capacity ?? "–"}</td><td><Delta b={c.before.safe_capacity} a={c.after.safe_capacity} /></td></tr>
                            <tr><td>Peak goodput (req/s)</td><td>{fmtNum(c.before.peak_rps, 1)}</td><td>{fmtNum(c.after.peak_rps, 1)}</td><td><Delta b={c.before.peak_rps} a={c.after.peak_rps} /></td></tr>
                            <tr><td>Worst p95 (ms)</td>
                                <td>{fmtNum(Math.max(...c.before.steps.map((s) => s.p95 ?? 0)), 0)}</td><td>{fmtNum(Math.max(...c.after.steps.map((s) => s.p95 ?? 0)), 0)}</td>
                                <td><Delta invert b={Math.max(...c.before.steps.map((s) => s.p95 ?? 0))} a={Math.max(...c.after.steps.map((s) => s.p95 ?? 0))} /></td></tr>
                            <tr><td>Top suspect</td><td>{words(c.before.top_suspect) || "none"}</td><td>{words(c.after.top_suspect) || "none"}</td><td /></tr>
                        </tbody>
                    </table>
                    <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {c.suspects_gone.map((m) => <Badge key={m} tone="good">No longer flagged: {words(m)}</Badge>)}
                        {c.suspects_new.map((m) => <Badge key={m} tone="crit">Newly flagged: {words(m)}</Badge>)}
                    </div>
                </div>
                <div>
                    <Legend items={[{ name: "before", color: "var(--series-3)" }, { name: "after", color: "var(--series-1)" }]} />
                    <LineChart xs={xs} unit="ms" decimals={1} refLine={{ value: sla, label: `target ${sla} ms` }}
                        series={[{ name: "before", color: "var(--series-3)", values: xs.map((x) => at(c.before, x)) },
                        { name: "after", color: "var(--series-1)", values: xs.map((x) => at(c.after, x)) }]} />
                </div>
            </div>
            {c.endpoints?.length > 0 && (
                <div style={{ marginTop: 18 }}>
                    <div className="sect">Per endpoint</div>
                    <div className="tablewrap"><table>
                        <thead><tr><th>Endpoint</th><th>Safe capacity</th><th>Worst p95 (ms)</th><th>Limit before</th><th>Limit after</th><th>Result</th></tr></thead>
                        <tbody>{c.endpoints.map((e) => (
                            <tr key={e.name}>
                                <td>{e.name}</td>
                                <td>{e.before.safe_capacity ?? "–"} → {e.after.safe_capacity ?? "–"} <Delta b={e.before.safe_capacity} a={e.after.safe_capacity} /></td>
                                <td>{fmtNum(e.before.worst_p95, 0)} → {fmtNum(e.after.worst_p95, 0)} <Delta invert b={e.before.worst_p95} a={e.after.worst_p95} /></td>
                                <td>{words(e.before.top_suspect) || "–"}</td><td>{words(e.after.top_suspect) || "–"}</td>
                                <td><Badge tone={e.outcome === "improved" ? "good" : e.outcome === "regressed" ? "crit" : e.outcome === "unchanged" ? "warn" : ""}>{e.outcome}</Badge></td>
                            </tr>))}</tbody>
                    </table></div>
                </div>
            )}
        </Card>
    );
}