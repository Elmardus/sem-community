/**
 * (#1046) Solar's percent of what the flows say came in.
 *
 * Top and bottom both come from the flows. A meter total (the battery's
 * measured charge) also counts cycles the flows missed: an inverter that
 * reads dark leaves the draw unassigned, so a flow divided by it reads solar
 * low. Same rule as `solar_share_pct` in the integration.
 *
 * Returns null when the flows hold nothing: no split is known.
 */
export function solarSharePct(solar, ...others) {
    const pos = (x) => {
        const n = Number(x);
        return Number.isFinite(n) && n > 0 ? n : 0;
    };
    const s = pos(solar);
    const total = others.reduce((sum, x) => sum + pos(x), s);
    return total > 0 ? (s / total) * 100 : null;
}
