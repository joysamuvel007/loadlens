import React, { useEffect, useState } from "react";
import { LineChart, Spark, fmtNum } from "./charts.jsx";
import { Card, Kpi, Badge, VerdictBadge, Legend, Icon } from "./ui.jsx";
import { api, postJson, words, VERDICTS, when } from "./api.js";
import BeforeAfter from "./BeforeAfter.jsx";

function headline(run) {
    const f = run.result.facts, att = f.attribution || {}, gp = att.goodput || {};
    switch (run.verdict) {
        case "invalid_test": return ["This test can't be diagnosed", f.test_validity?.reason || "The target did not answer normally at the lowest load."];
        case "identified": return [`The limit is ${words(att.suspects[0].metric)}`, att.suspects[0].why];
        case "capacity_limited_unattributed":
            return [`Capacity tops out near ${fmtNum(gp.ceiling_rps, 0)} requests per second`, "Successful responses stopped growing while load kept rising, but no recorded metric shows why."];
        case "no_degradation": return ["The service stayed healthy across the tested range", att.reason || "Test at higher concurrency to find the real limit."];
        default: return ["The data does not identify a cause", att.reason || ""];
    }
}

function Evidence({ att }) {
    const groups = [["Limiting resource", att.suspects || [], "var(--series-2)"], ["Consequences of the slowdown", att.consequences || [], "var(--series-1)"]];
    const any = groups.some(([, l]) => l.length);
    if (!any) return <p className="muted">No recorded server metric moved in a way that explains the slowdown.</p>;
    return (
        <>
            {groups.map(([title, list, color]) => list.length > 0 && (
                <div key={title} style={{ marginBottom: 8 }}>
                    <div className="sect" style={{ marginTop: 6 }}>{title}</div>
                    {list.map((e) => (
                        <div className="ev" key={e.metric}>
                            <div>
                                <h4>{words(e.metric)} {e.role && <Badge tone={e.role === "ceiling" ? "crit" : "info"}>{e.role}</Badge>}
                                    {e.resource_kind && <Badge>{words(e.resource_kind)}</Badge>}</h4>
                                <p>{e.why}</p>
                                <p className="muted" style={{ fontSize: 12 }}>
                                    Baseline {fmtNum(e.baseline_value, 2)} → peak {fmtNum(e.peak_value, 2)}
                                    {e.spearman_vs_p95 != null && ` · rank correlation with p95: ${e.spearman_vs_p95}`}
                                </p>
                            </div>
                            {e.per_step && <Spark points={e.per_step} onset={att.degradation_onset_concurrency} color={color} label={`${e.metric} per load level`} />}
                        </div>
                    ))}
                </div>
            ))}
        </>
    );
}

const EP_COLORS = ["#3358e4", "#f0722a", "#7c3aed", "#12a150", "#d92d20", "#0891b2", "#a16207", "#64748b"];

function Endpoints({ data, sla }) {
    const eps = data.endpoints;
    const xs = [...new Set(eps.flatMap((e) => e.steps.map((s) => s.concurrency)))].sort((a, b) => a - b);
    const at = (e, x) => e.steps.find((s) => s.concurrency === x)?.p95_ms ?? null;
    return (
        <Card title="Endpoints" sub="Each endpoint measured on its own, so a fast one cannot hide a slow one inside the blended numbers.">
            {data.warnings.map((w, i) => <div key={i} className="banner warn" style={{ marginBottom: 10 }}><Icon n="alert" /> <div>{w}</div></div>)}
            <p className="muted" style={{ marginBottom: 12, fontSize: 12 }}>
                "What limits it" uses the same evidence rules as the overall analysis, applied to each endpoint's own latency. All endpoints share one server, so a shared limit (for example CPU) can appear on several rows.
            </p>
            <div className="grid2">
                <div className="tablewrap"><table>
                    <thead><tr><th>Endpoint</th><th>Requests</th><th>Worst p95</th><th>Safe capacity</th><th>First breach</th><th>What limits it</th></tr></thead>
                    <tbody>{eps.map((e, i) => (
                        <tr key={e.name} className={e.meets_sla_everywhere ? "" : "bad"}>
                            <td><i className="sw" style={{ background: EP_COLORS[i % 8] }} />{e.name}{e.broken && <> <Badge tone="crit">errors</Badge></>}</td>
                            <td>{e.requests}</td><td>{fmtNum(e.worst_p95_ms, 0)} ms</td>
                            <td>{e.safe_capacity || "none"}</td><td>{e.first_violating_concurrency ?? "none"}</td>
                            <td>{e.attribution?.top_suspect ? <><b>{words(e.attribution.top_suspect)}</b> <Badge>{words(e.attribution.top_kind)}</Badge></>
                                : e.meets_sla_everywhere ? <span className="muted">healthy</span> : <span className="muted">no clear metric</span>}</td>
                        </tr>))}</tbody>
                </table></div>
                <div>
                    <Legend items={eps.map((e, i) => ({ name: e.name, color: EP_COLORS[i % 8] }))} />
                    <LineChart xs={xs} unit="ms" decimals={0} refLine={{ value: sla, label: `target ${sla} ms` }}
                        series={eps.map((e, i) => ({ name: e.name, color: EP_COLORS[i % 8], values: xs.map((x) => at(e, x)) }))} />
                </div>
            </div>
        </Card>
    );
}

function Solutions({ sol, applied, setApplied }) {
    if (!sol) return <p className="muted">No suggestions were produced for this run.</p>;
    if (sol.status === "none") return <div className="banner good"><Icon n="check" /> <div>{sol.headline} {sol.next_measurements.join(" ")}</div></div>;
    if (sol.status === "measure_next") return (
        <div className="solgrid">
            <div className="banner warn"><Icon n="alert" /> <div><b>{sol.headline}</b></div></div>
            <div className="sol"><span className="rank">?</span><div><h4>What to measure next</h4>
                <ol>{sol.next_measurements.map((t, i) => <li key={i}>{t}</li>)}</ol></div></div>
        </div>
    );
    return (
        <div className="solgrid">
            <p className="muted">{sol.headline}</p>
            {sol.items.map((it) => (
                <div key={it.id} className={`sol ${applied === it.title ? "applied" : ""}`}>
                    <span className="rank">{it.rank}</span>
                    <div>
                        <h4>{it.title} <Badge tone={it.effort === "Low" ? "good" : "warn"}>{it.effort} effort</Badge> <Badge>{words(it.kind)}</Badge></h4>
                        <ol>{it.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                        <div className="why"><b>Why this applies: </b>{it.why}</div>
                        <div className="chk"><b>How to confirm it worked: </b>{it.verify}</div>
                        <div className="risk">Watch out: {it.risk}</div>
                        <label><input type="radio" name="applied" checked={applied === it.title} onChange={() => setApplied(it.title)} /> I applied this change</label>
                    </div>
                </div>
            ))}
            <p className="muted" style={{ fontSize: 12 }}>{sol.retest_hint}</p>
        </div>
    );
}

function Retest({ run, applied, setApplied, onStarted }) {
    const [label, setLabel] = useState("");
    const [busy, setBusy] = useState(false), [err, setErr] = useState(null);
    useEffect(() => { if (applied) setLabel(applied); }, [applied]);
    if (!run.live) {
        return <div className="banner info"><Icon n="retest" /> <div>Re-testing needs a <b>live test</b> because it re-runs the same target with the same settings. Start a live load test to use before/after.</div></div>;
    }
    const go = async () => {
        setBusy(true); setErr(null);
        try { const r = await postJson(`/runs/${run.id}/retest`, { fix_label: label }); onStarted(r.id, run.sla_ms); }
        catch (e) { setErr(e.message); setBusy(false); }
    };
    return (
        <div className="form">
            <p className="muted">Apply <b>one</b> change to your service, restart it, then run the identical test again. LoadLens will show the two runs side by side.</p>
            <div className="f"><label>What did you change?</label>
                <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. DB pool size 5 → 20" /></div>
            {err && <div className="banner crit">{err}</div>}
            <div><button className="btn primary" disabled={busy} onClick={go}><Icon n="retest" size={14} /> Re-run the same test</button></div>
        </div>
    );
}

export default function RunView({ id, refresh, onStarted, onOpen }) {
    const [run, setRun] = useState(null);
    const [err, setErr] = useState(null);
    const [applied, setApplied] = useState("");
    const [showReport, setShowReport] = useState(false);
    useEffect(() => { setRun(null); setErr(null); setApplied(""); api(`/runs/${id}`).then(setRun).catch((e) => setErr(e.message)); }, [id]);
    if (err) return <div className="page"><div className="banner crit">{err}</div></div>;
    if (!run) return <div className="page"><div className="empty"><span className="spin" /></div></div>;
    if (run.status === "error") return <div className="page"><div className="banner crit"><Icon n="alert" /> <div><b>This run failed.</b><br />{run.error}</div></div></div>;
    const r = run.result;
    if (!r) return <div className="page"><div className="empty">This run has no result yet.</div></div>;

    const f = r.facts, att = f.attribution || {}, steps = f.load_test_steps || [];
    const fc = r.forecast || {};
    const xs = steps.map((s) => s.concurrency);
    const gp = att.goodput?.per_step_goodput || [];
    const gpv = xs.map((x) => gp.find((g) => g.concurrency === x)?.rps ?? null);
    const worst = Math.max(...steps.map((s) => s.p95_ms), 0);
    const [h1, h2] = headline(run);
    const tone = VERDICTS[run.verdict]?.color;

    return (
        <>
            <div className="topbar">
                <div>
                    <div className="crumb">Run {run.id} · {when(run.created)}</div>
                    <h1>{run.name}</h1>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <VerdictBadge v={run.verdict} />
                    <Badge tone="info">{r.mock || run.model === "mock" ? "Built-in summary" : run.model}</Badge>
                    <button className="btn" onClick={() => { api(`/runs/${run.id}`, { method: "DELETE" }).then(() => { refresh(); onOpen(null); }); }}><Icon n="trash" size={14} /> Delete</button>
                </div>
            </div>
            <div className="page">
                {run.parent_id && <BeforeAfter before={run.parent_id} after={run.id} fixLabel={run.fix_label} sla={run.sla_ms} onOpen={onOpen} />}
                {r.model_note && <div className="banner warn"><Icon n="alert" /> <div>{r.model_note}</div></div>}

                <section className={`card hero ${run.verdict}`}>
                    <div><VerdictBadge v={run.verdict} /></div>
                    <h2>{h1}</h2>
                    <p>{h2}</p>
                    {r.diagnosis_text && run.verdict !== "invalid_test" && <p className="muted" style={{ fontSize: 13 }}><b>Explanation:</b> {r.diagnosis_text}</p>}
                </section>

                {run.verdict !== "invalid_test" && (
                    <>
                        <div className="kpis">
                            <Kpi k="Safe capacity" v={fc.safe_capacity ?? "–"} unit="concurrent" note={fc.safe_capacity_p95_ms != null ? `p95 ${fmtNum(fc.safe_capacity_p95_ms, 1)} ms at that load` : "No level met the target"} tone={fc.safe_capacity == null ? "var(--crit)" : undefined} />
                            <Kpi k="First breach" v={fc.first_violating_concurrency ?? "none"} unit={fc.first_violating_concurrency ? "concurrent" : ""} note={fc.first_violating_p95_ms != null ? `p95 ${fmtNum(fc.first_violating_p95_ms, 1)} ms` : `Within ${run.sla_ms} ms target`} />
                            <Kpi k="Worst p95" v={fmtNum(worst, 0)} unit="ms" note={`target ${run.sla_ms} ms`} tone={worst > run.sla_ms ? "var(--crit)" : "var(--good)"} />
                            <Kpi k="Peak goodput" v={att.goodput ? fmtNum(att.goodput.ceiling_rps ?? Math.max(...gpv.filter((v) => v != null), 0), 0) : "–"} unit="req/s" note="successful responses per second" />
                            <Kpi k="Confidence" v={(r.confidence || "–").toString().replace(/^./, (c) => c.toUpperCase())} note={r.verify_attempts > 1 ? "after one correction" : "evidence-gated"} />
                        </div>

                        <div className="grid2">
                            <Card title="Latency as load rises" right={<Legend items={[{ name: "p95", color: "var(--series-1)" }, { name: "p50", color: "var(--series-3)" }]} />}>
                                <LineChart xs={xs} unit="ms" decimals={1} onset={att.degradation_onset_concurrency}
                                    series={[{ name: "p95", color: "var(--series-1)", values: steps.map((s) => s.p95_ms) }, { name: "p50", color: "var(--series-3)", values: steps.map((s) => s.p50_ms) }]}
                                    refLine={{ value: run.sla_ms, label: `target ${run.sla_ms} ms` }} />
                            </Card>
                            <Card title="Successful responses per second" right={<Legend items={[{ name: "goodput (2xx/s)", color: "var(--series-2)" }]} />}>
                                <LineChart xs={xs} unit="req/s" decimals={1} onset={att.degradation_onset_concurrency}
                                    series={[{ name: "goodput", color: "var(--series-2)", values: gpv }]} />
                            </Card>
                        </div>

                        {f.endpoints && <Endpoints data={f.endpoints} sla={run.sla_ms} />}

                        <Card title="Evidence" sub="Computed from the recorded data. No model is involved in this section.">
                            <Evidence att={att} />
                        </Card>
                    </>
                )}

                <Card title="Suggested changes" sub="Chosen from a fixed knowledge base by the detected cause, never invented by the model.">
                    <Solutions sol={r.solutions} applied={applied} setApplied={setApplied} />
                </Card>

                {run.verdict !== "invalid_test" && (
                    <Card title="Verify the fix" sub="Re-run the same test and compare before and after.">
                        <Retest run={run} applied={applied} setApplied={setApplied} onStarted={onStarted} />
                    </Card>
                )}

                <Card title="Load levels" pad={false}>
                    <div className="tablewrap"><table>
                        <thead><tr><th>Concurrent</th><th>p50 (ms)</th><th>p95 (ms)</th><th>Failures</th><th>Meets target</th></tr></thead>
                        <tbody>{steps.map((s) => (
                            <tr key={s.concurrency} className={s.meets_sla ? "" : "bad"}>
                                <td>{s.concurrency}</td><td>{fmtNum(s.p50_ms, 1)}</td><td>{fmtNum(s.p95_ms, 1)}</td>
                                <td>{Math.round((s.error_rate || 0) * 100)}%</td><td>{s.meets_sla ? <Badge tone="good">Yes</Badge> : <Badge tone="crit">No</Badge>}</td>
                            </tr>))}</tbody>
                    </table></div>
                </Card>

                <Card title="Full text report" right={<button className="btn" onClick={() => setShowReport(!showReport)}>{showReport ? "Hide" : "Show"}</button>}>
                    {showReport ? <pre className="report">{r.report}</pre> : <p className="muted">The plain-text version of this analysis, for copying into a ticket or document.</p>}
                </Card>
            </div>
        </>
    );
}