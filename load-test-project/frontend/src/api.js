export const api = (path, opts) =>
    fetch(`/api${path}`, opts).then(async (r) => {
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            const d = j.detail;
            throw new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${r.status}`);
        }
        return r.json();
    });

export const postJson = (path, body) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const words = (s) => (s || "").replace(/_/g, " ");

export const VERDICTS = {
    identified: { label: "Cause identified", tone: "warn", color: "#f0722a" },
    capacity_limited_unattributed: { label: "Limit found, cause unknown", tone: "warn", color: "#d98a00" },
    weak_evidence: { label: "Weak evidence", tone: "warn", color: "#d98a00" },
    unexplained: { label: "Unexplained slowdown", tone: "warn", color: "#d98a00" },
    no_degradation: { label: "Healthy in tested range", tone: "good", color: "#12a150" },
    invalid_test: { label: "Invalid test", tone: "crit", color: "#d92d20" },
    insufficient_data: { label: "Not enough data", tone: "", color: "#94a3b8" },
};

export const when = (t) => new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });