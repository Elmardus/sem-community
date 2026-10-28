/**
 * (#1213) "Is this right?" after install: SEM's own readings in plain words.
 *
 * A new user knows whether the house is buying power or sending it out.
 * They do not know entity IDs. So the Configuration tab shows what SEM
 * reads now, as watts with a direction, and offers one tap when the
 * direction is wrong. A sensor that is out reads as missing, never as 0.
 */

const QUIET_W = 50;

function watts(stateObj) {
    if (!stateObj) return null;
    const s = stateObj.state;
    if (s === undefined || s === null || s === '' || s === 'unavailable' || s === 'unknown') return null;
    const v = parseFloat(s);
    if (Number.isNaN(v)) return null;
    const unit = String((stateObj.attributes || {}).unit_of_measurement || '').toLowerCase();
    return unit === 'kw' ? v * 1000 : v;
}

/** Net of two one-way sensors: + for the first, − for the second. */
function net(a, b) {
    if (a === null && b === null) return null;
    return (a || 0) - (b || 0);
}

/**
 * Rows for the check. `get(suffix)` returns hass.states['sensor.sem_<suffix>'].
 * Each row: { key, w, dir } — w is ≥ 0 or null, dir is a word key or ''.
 */
export function sourceCheckRows(get) {
    const rows = [];
    const solar = watts(get('solar_power'));
    rows.push({ key: 'solar', w: solar === null ? null : Math.max(0, solar), dir: '' });

    const grid = net(watts(get('grid_import_power')), watts(get('grid_export_power')));
    rows.push({
        key: 'grid', w: grid === null ? null : Math.abs(grid),
        dir: grid === null || Math.abs(grid) < QUIET_W ? '' : (grid > 0 ? 'buying' : 'sending'),
    });

    const batt = net(watts(get('battery_charge_power')), watts(get('battery_discharge_power')));
    if (batt !== null) {
        rows.push({
            key: 'battery', w: Math.abs(batt),
            dir: Math.abs(batt) < QUIET_W ? '' : (batt > 0 ? 'charging' : 'giving'),
        });
    }

    const home = watts(get('home_consumption_power'));
    rows.push({ key: 'home', w: home, dir: '' });
    return rows;
}

/** "4 152 W" with a thin space, or the em-dash for a missing value. */
export function fmtWatts(w) {
    if (w === null || w === undefined) return '—';
    return String(Math.round(w)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' W';
}
