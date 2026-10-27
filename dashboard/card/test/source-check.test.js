/**
 * #1213 — the Configuration tab says what SEM reads, in plain words.
 *
 * Run: `npm test` (from dashboard/card).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sourceCheckRows, fmtWatts } from '../src/util/source-check.js';

const states = (m) => (suffix) => (suffix in m
    ? { state: String(m[suffix][0]), attributes: { unit_of_measurement: m[suffix][1] || 'W' } }
    : undefined);

test('a house buying from the grid while the battery gives', () => {
    const rows = sourceCheckRows(states({
        solar_power: [4152], grid_import_power: [310], grid_export_power: [0],
        battery_charge_power: [0], battery_discharge_power: [1.45, 'kW'],
        home_consumption_power: [5912],
    }));
    assert.deepEqual(rows, [
        { key: 'solar', w: 4152, dir: '' },
        { key: 'grid', w: 310, dir: 'buying' },
        { key: 'battery', w: 1450, dir: 'giving' },
        { key: 'home', w: 5912, dir: '' },
    ]);
});

test('sending out, battery charging', () => {
    const rows = sourceCheckRows(states({
        grid_import_power: [0], grid_export_power: [1240],
        battery_charge_power: [980], battery_discharge_power: [0],
    }));
    assert.equal(rows.find((r) => r.key === 'grid').dir, 'sending');
    assert.equal(rows.find((r) => r.key === 'battery').dir, 'charging');
});

test('a quiet meter claims no direction', () => {
    const rows = sourceCheckRows(states({ grid_import_power: [12], grid_export_power: [0] }));
    assert.equal(rows.find((r) => r.key === 'grid').dir, '');
});

test('a sensor that is out reads as missing, not 0', () => {
    const rows = sourceCheckRows((s) => (s === 'solar_power' ? { state: 'unavailable' } : undefined));
    assert.equal(rows.find((r) => r.key === 'solar').w, null);
    assert.equal(rows.find((r) => r.key === 'grid').w, null);
    assert.equal(fmtWatts(null), '—');
});

test('no battery, no battery row', () => {
    const rows = sourceCheckRows(states({ solar_power: [100] }));
    assert.equal(rows.some((r) => r.key === 'battery'), false);
});

test('watts read as a person reads them', () => {
    assert.equal(fmtWatts(4152.4), '4 152 W');
    assert.equal(fmtWatts(310), '310 W');
});
