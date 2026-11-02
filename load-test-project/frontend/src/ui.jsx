import React from "react";
import { VERDICTS } from "./api.js";

const P = {
    play: "M8 5v14l11-7z", plus: "M12 5v14M5 12h14", clock: "M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z",
    retest: "M20 12a8 8 0 10-2.3 5.7M20 5v5h-5", check: "M5 12l5 5 9-10", alert: "M12 8v5M12 16.5v.5M12 3l10 18H2z",
    bolt: "M13 2L4 14h7l-1 8 9-12h-7z", trash: "M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13", stop: "M6 6h12v12H6z",
};
export const Icon = ({ n, size = 16 }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d={P[n]} />
    </svg>
);

export const Badge = ({ tone = "", children }) => <span className={`badge ${tone}`}>{children}</span>;
export const VerdictBadge = ({ v }) => {
    const x = VERDICTS[v] || { label: v || "–", tone: "" };
    return <Badge tone={x.tone}>{x.label}</Badge>;
};

export const Card = ({ title, sub, right, children, pad = true }) => (
    <section className="card">
        {(title || right) && (
            <header>
                <div><h3>{title}</h3>{sub && <small>{sub}</small>}</div>
                {right}
            </header>
        )}
        <div className={pad ? "body" : ""}>{children}</div>
    </section>
);

export const Kpi = ({ k, v, unit, note, tone }) => (
    <div className="card kpi">
        <div className="k">{k}</div>
        <div className="v" style={tone ? { color: tone } : null}>{v}{unit && <small>{unit}</small>}</div>
        {note && <div className="n">{note}</div>}
    </div>
);

export const Legend = ({ items }) => (
    <span className="legend">{items.map((i) => <span key={i.name}><i style={{ borderColor: i.color }} />{i.name}</span>)}</span>
);