import { test } from 'node:test';
import assert from 'node:assert/strict';

import { demandName } from '../src/util/demand-name.js';

// #1053 — the energy plan card printed "ev_charger_1" and "battery" (the
// part of a demand id after the colon) as names on a Dutch dashboard.
const KINDS = {
    ev: 'energy_plan_kind_ev', load: 'energy_plan_kind_load',
    battery: 'energy_plan_kind_battery', comfort: 'energy_plan_kind_comfort',
};
const t = (k) => `«${k}»`;

test('the name the user gave wins', () => {
    assert.equal(demandName('Wallbox Pulsar', 'ev:ev_charger_1', 'ev', KINDS, t),
                 'Wallbox Pulsar');
});

test('no name: the kind in the user language, never the id', () => {
    for (const [id, kind, want] of [
        ['ev:ev_charger_1', undefined, '«energy_plan_kind_ev»'],
        ['load:energy_dashboard_shelly_441793', undefined, '«energy_plan_kind_load»'],
        ['arbitrage:battery', 'battery', '«energy_plan_kind_battery»'],
        ['comfort:living', 'comfort', '«energy_plan_kind_comfort»'],
        [null, 'ev', '«energy_plan_kind_ev»'],
    ]) {
        assert.equal(demandName(null, id, kind, KINDS, t), want);
    }
});

test('an empty name is no name', () => {
    assert.equal(demandName('', 'ev:x', undefined, KINDS, t), '«energy_plan_kind_ev»');
});

test('an unknown kind and prefix fall back to a load, not to the id', () => {
    assert.equal(demandName(null, 'arbitrage:battery', undefined, KINDS, t),
                 '«energy_plan_kind_load»');
    assert.equal(demandName(null, undefined, undefined, KINDS, t),
                 '«energy_plan_kind_load»');
});
