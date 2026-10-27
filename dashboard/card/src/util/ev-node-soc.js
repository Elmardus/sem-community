/**
 * What the system diagram's EV node says about the car's state of charge.
 *
 * The EV node is ONE drawing for the whole fleet, but a car's SOC belongs to
 * one charger. So the rule is the one the charger tile already follows
 * (util/charger-soc.js, #683) and docs/MULTI_CHARGER.md spells out:
 *
 *   - one charger  → the car on it: its own vehicle SOC, else the fleet
 *                    sensor (legacy single-charger installs), else the
 *                    taper detector's estimate, drawn with a leading "~";
 *   - several      → nothing. No single car is "the" EV, and a fleet surface
 *                    must not borrow one charger's reading and show it for
 *                    all of them.
 *
 * An absent reading is never drawn as 0 %: 0 % is a flat car, a real and
 * alarming measurement (see missing-value.js).
 */
import { resolveChargerSoc } from './charger-soc.js';
import { socDisplay } from './missing-value.js';

/**
 * A SOC percentage from a state object, or null when it is not a usable one:
 * absent, unavailable, unknown, not a number, or outside 0-100.
 * @param {{state?: string}|undefined} stateObj
 * @returns {number|null}
 */
export function socFromState(stateObj) {
    const raw = stateObj?.state;
    if (raw === undefined || raw === null || raw === '' ||
        raw === 'unavailable' || raw === 'unknown') return null;
    const v = parseFloat(raw);
    if (!Number.isFinite(v) || v < 0 || v > 100) return null;
    return v;
}

/**
 * The charger ids behind `sensor.sem_charger_<id>_power`. The per-charger
 * flow sensors (`..._flow_<from>_to_<to>_power`) are not chargers (#356).
 * Same discovery the EV card uses.
 * @param {string[]} entityIds all entity ids in hass.states
 * @param {string} prefix e.g. "sensor.sem_"
 * @returns {string[]}
 */
export function chargerIdsFrom(entityIds, prefix) {
    const head = `${prefix}charger_`;
    const ids = [];
    for (const eid of entityIds) {
        if (!eid.startsWith(head) || !eid.endsWith('_power')) continue;
        if (eid.includes('_flow_')) continue;
        const id = eid.slice(head.length, eid.length - '_power'.length);
        if (id) ids.push(id);
    }
    return ids;
}

/**
 * @param {Object<string, {state?: string}>} states hass.states
 * @param {string} prefix e.g. "sensor.sem_"
 * @param {string[]} chargerIds from chargerIdsFrom()
 * @returns {{soc: number|null, isEstimate: boolean}}
 */
export function evNodeSoc(states, prefix, chargerIds) {
    if (chargerIds.length !== 1) return { soc: null, isEstimate: false };
    const id = chargerIds[0];
    return resolveChargerSoc(
        socFromState(states[`${prefix}charger_${id}_vehicle_soc`]),
        socFromState(states[`${prefix}vehicle_soc`]),
        socFromState(states[`${prefix}charger_${id}_estimated_soc`]),
        chargerIds.length,
    );
}

/**
 * The text for the node: "64%", "~64%" for an estimate, or "" when unknown.
 * Same "97%" format as the battery cell.
 * @param {{soc: number|null, isEstimate?: boolean}} reading
 * @returns {string}
 */
export function evNodeSocLabel(reading) {
    const { soc, isEstimate } = reading || {};
    const shown = socDisplay(soc);
    if (!shown.known) return '';
    return `${isEstimate ? '~' : ''}${shown.label}`;
}
