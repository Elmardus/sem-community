/**
 * #1053 — the name a plan row is shown under.
 *
 * A plan demand id is SEM's key: `ev:<charger id>`, `load:<device id>`,
 * `arbitrage:battery`. The energy plan card printed the part after the
 * colon, so a Dutch card read "ev_charger_1" and "battery" as names. The
 * backend now sends the name the user gave (`label`); without one, the row
 * is named by its kind in the user's language. Never by the id.
 *
 * `kindLabels` maps a kind (`ev`, `load`, `battery`, `comfort`) to its
 * translation key; `t` is the card's translator.
 */
export function demandName(label, id, kind, kindLabels, t) {
    if (label) return label;
    const prefix = String(id || '').split(':')[0];
    const key = kindLabels[kind] || kindLabels[prefix] || kindLabels.load;
    return t(key);
}
