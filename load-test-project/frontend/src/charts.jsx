import React, { useEffect, useRef, useState } from "react";

function useWidth() {
    const ref = useRef(null);
    const [w, setW] = useState(640);
    useEffect(() => {
        if (!ref.current) return;
        const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.floor(e.contentRect.width))));
        ro.observe(ref.current);
        return () => ro.disconnect();
    }, []);
    return [ref, w];
}

function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

export const fmtNum = (v, d = 0) =>
    v == null || Number.isNaN(v) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });

/**
 * One y-axis, numeric x. Marks: 2px line, 8px+ markers, hairline grid.
 * props: xs[], series[{name,color,values[]}], unit, ref{value,label}, onset (x), band{from,to,label}
 */
export function LineChart({ xs, series, unit = "", height = 200, refLine, onset, band, decimals = 0, xLabel = "concurrent requests" }) {
    const [wrap, w] = useWidth();
    const [hi, setHi] = useState(null);
    const m = { l: 46, r: 14, t: 16, b: 34 };
    const iw = w - m.l - m.r, ih = height - m.t - m.b;
    const all = series.flatMap((s) => s.values.filter((v) => v != null));
    if (refLine) all.push(refLine.value);
    const ymax = niceMax(Math.max(...all, 1) * 1.05);
    const x0 = xs[0], x1 = xs[xs.length - 1];
    const X = (x) => m.l + (x1 === x0 ? iw / 2 : ((x - x0) / (x1 - x0)) * iw);
    const Y = (v) => m.t + ih - (v / ymax) * ih;
    const yt = [0, 0.25, 0.5, 0.75, 1].map((f) => f * ymax);
    const step = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(iw / 56))));

    const onMove = (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const px = ((e.clientX - r.left) / r.width) * w;
        let best = 0, bd = Infinity;
        xs.forEach((x, i) => { const d = Math.abs(X(x) - px); if (d < bd) { bd = d; best = i; } });
        setHi(best);
    };
    const onKey = (e) => {
        if (e.key === "ArrowRight") { e.preventDefault(); setHi((h) => Math.min(xs.length - 1, (h ?? -1) + 1)); }
        if (e.key === "ArrowLeft") { e.preventDefault(); setHi((h) => Math.max(0, (h ?? xs.length) - 1)); }
        if (e.key === "Escape") setHi(null);
    };
    const tipLeft = hi == null ? 0 : X(xs[hi]);
    const flip = tipLeft > w * 0.6;

    return (
        <div className="svgwrap" ref={wrap}>
            <svg width={w} height={height} style={{ display: "block", maxWidth: "100%" }} role="img" tabIndex={0}
                aria-label={`${series.map((s) => s.name).join(", ")} against ${xLabel}. Use left and right arrow keys to read values.`}
                onPointerMove={onMove} onPointerLeave={() => setHi(null)} onKeyDown={onKey} onBlur={() => setHi(null)}>
                {yt.map((v, i) => (
                    <g key={i}>
                        <line x1={m.l} x2={w - m.r} y1={Y(v)} y2={Y(v)} stroke={i === 0 ? "var(--axis)" : "var(--grid)"} strokeWidth="1" />
                        <text x={m.l - 8} y={Y(v) + 4} textAnchor="end">{fmtNum(v, ymax < 10 ? 1 : 0)}</text>
                    </g>
                ))}
                {xs.map((x, i) => i % step === 0 && <text key={x} x={X(x)} y={height - 14} textAnchor="middle">{x}</text>)}
                <text x={m.l + iw / 2} y={height - 1} textAnchor="middle">{xLabel}</text>

                {band && (
                    <g>
                        <rect x={X(band.from)} y={m.t} width={Math.max(0, X(band.to) - X(band.from))} height={ih} fill="var(--series-2)" opacity="0.10" />
                        <text className="lbl" x={X(band.from) + 6} y={m.t + 28}>{band.label}</text>
                    </g>
                )}
                {refLine && (
                    <g>
                        <line x1={m.l} x2={w - m.r} y1={Y(refLine.value)} y2={Y(refLine.value)} stroke="var(--ink-2)" strokeWidth="1.5" strokeDasharray="5 4" />
                        <text className="lbl" x={w - m.r - 4} y={Y(refLine.value) - 6} textAnchor="end">{refLine.label}</text>
                    </g>
                )}
                {onset != null && xs.includes(onset) && (
                    <g>
                        <line x1={X(onset)} x2={X(onset)} y1={m.t} y2={m.t + ih} stroke="var(--crit)" strokeWidth="1.5" />
                        <text className="lbl" x={X(onset) + (X(onset) > w * 0.72 ? -5 : 5)} y={m.t + 12} textAnchor={X(onset) > w * 0.72 ? "end" : "start"}>slowdown starts</text>
                    </g>
                )}
                {series.map((s) => (
                    <g key={s.name}>
                        <polyline fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"
                            points={xs.map((x, i) => (s.values[i] == null ? null : `${X(x)},${Y(s.values[i])}`)).filter(Boolean).join(" ")} />
                        {xs.map((x, i) => s.values[i] != null && (
                            <circle key={i} cx={X(x)} cy={Y(s.values[i])} r={hi === i ? 5 : 3.5} fill={s.color} stroke="var(--surface)" strokeWidth="2" />
                        ))}
                    </g>
                ))}
                {hi != null && <line x1={X(xs[hi])} x2={X(xs[hi])} y1={m.t} y2={m.t + ih} stroke="var(--muted)" strokeWidth="1" opacity="0.6" />}
            </svg>
            {hi != null && (
                <div className="tip" style={{ top: m.t, left: flip ? undefined : tipLeft + 12, right: flip ? w - tipLeft + 12 : undefined }}>
                    <b>{xs[hi]} concurrent</b>
                    {series.map((s) => (
                        <span key={s.name}><span>{s.name}</span><em>{fmtNum(s.values[hi], decimals)} {unit}</em></span>
                    ))}
                </div>
            )}
        </div>
    );
}

/** Small per-metric series for the evidence list: line + the level the load test slowed down at. */
export function Spark({ points, onset, color = "var(--series-1)", label }) {
    const w = 210, h = 64, p = 4;
    const xs = points.map((q) => q.concurrency), ys = points.map((q) => q.value);
    const lo = Math.min(...ys), hi = Math.max(...ys);
    const X = (x) => p + ((x - xs[0]) / Math.max(1, xs[xs.length - 1] - xs[0])) * (w - 2 * p);
    const Y = (v) => h - p - ((v - lo) / Math.max(1e-9, hi - lo)) * (h - 2 * p);
    return (
        <svg className="spark" viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label}>
            {onset != null && xs.includes(onset) && <line x1={X(onset)} x2={X(onset)} y1="0" y2={h} stroke="var(--crit)" strokeWidth="1.5" opacity="0.7" />}
            <polyline fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" points={points.map((q) => `${X(q.concurrency)},${Y(q.value)}`).join(" ")} />
            <circle cx={X(xs[xs.length - 1])} cy={Y(ys[ys.length - 1])} r="3.5" fill={color} stroke="var(--surface)" strokeWidth="2" />
        </svg>
    );
}