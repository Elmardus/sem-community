/**
 * The system diagram's EV node shows the car's state of charge, and only
 * when one car can honestly be meant: one charger. The EV node used to show
 * power, status and kWh but no SOC, although the charger had a vehicle SOC
 * sensor configured; every other node on the diagram showed its level.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    socFromState, chargerIdsFrom, evNodeSoc, evNodeSocLabel,
} from '../src/util/ev-node-soc.js';

const P = 'sensor.sem_';
const st = (v) => ({ state: String(v) });

test('a usable SOC is a number between 0 and 100', () => {
    assert.equal(socFromState(st('64.0')), 64);
    assert.equal(socFromState(st('0')), 0);        // a flat car is a real reading
    assert.equal(socFromState(st('100')), 100);
});

test('absent, dark or nonsense readings are no SOC', () => {
    assert.equal(socFromState(undefined), null);
    assert.equal(socFromState(st('unavailable')), null);
    assert.equal(socFromState(st('unknown')), null);
    assert.equal(socFromState(st('')), null);
    assert.equal(socFromState(st('abc')), null);
    assert.equal(socFromState(st('-3')), null);
    assert.equal(socFromState(st('101')), null);   // a kWh or a range, not a percentage
});

test('chargers are found by their power sensor, and flow sensors are not chargers', () => {
    const ids = chargerIdsFrom([
        'sensor.sem_charger_ev_charger_power',
        'sensor.sem_charger_ev_charger_session_energy',
        'sensor.sem_charger_ev_charger_flow_solar_to_ev_power',
        'sensor.sem_ev_power',
        'sensor.other_charger_x_power',
    ], P);
    assert.deepEqual(ids, ['ev_charger']);
});

test('a prefix other than the default finds that prefix\'s chargers', () => {
    assert.deepEqual(
        chargerIdsFrom(['sensor.house2_charger_a_power', 'sensor.sem_charger_b_power'], 'sensor.house2_'),
        ['a']);
});

test('one charger shows its own car', () => {
    const r = evNodeSoc({
        'sensor.sem_charger_ev_charger_vehicle_soc': st(64),
        'sensor.sem_charger_ev_charger_estimated_soc': st(50),
        'sensor.sem_vehicle_soc': st(64),
    }, P, ['ev_charger']);
    assert.deepEqual(r, { soc: 64, isEstimate: false });
    assert.equal(evNodeSocLabel(r), '64%');
});

test('one charger without its own sensor falls back to the fleet sensor', () => {
    const r = evNodeSoc({ 'sensor.sem_vehicle_soc': st(71) }, P, ['ev_charger']);
    assert.deepEqual(r, { soc: 71, isEstimate: false });
});

test('with no reading but an estimate, the estimate is shown and marked', () => {
    const r = evNodeSoc({ 'sensor.sem_charger_ev_charger_estimated_soc': st(55) }, P, ['ev_charger']);
    assert.deepEqual(r, { soc: 55, isEstimate: true });
    assert.equal(evNodeSocLabel(r), '~55%');
});

test('a car that reads unavailable falls through to the estimate, never to 0 %', () => {
    const r = evNodeSoc({
        'sensor.sem_charger_ev_charger_vehicle_soc': st('unavailable'),
        'sensor.sem_charger_ev_charger_estimated_soc': st(55),
    }, P, ['ev_charger']);
    assert.deepEqual(r, { soc: 55, isEstimate: true });
});

test('nothing known shows nothing', () => {
    const r = evNodeSoc({}, P, ['ev_charger']);
    assert.deepEqual(r, { soc: null, isEstimate: false });
    assert.equal(evNodeSocLabel(r), '');
});

test('no charger at all shows nothing, even with a fleet sensor', () => {
    const r = evNodeSoc({ 'sensor.sem_vehicle_soc': st(80) }, P, []);
    assert.equal(evNodeSocLabel(r), '');
});

test('several chargers show nothing: the node is not one car (#683, MULTI_CHARGER)', () => {
    const states = {
        'sensor.sem_charger_a_vehicle_soc': st(40),
        'sensor.sem_charger_b_vehicle_soc': st(90),
        'sensor.sem_vehicle_soc': st(40),     // the promoted first reading
    };
    const r = evNodeSoc(states, P, ['a', 'b']);
    assert.deepEqual(r, { soc: null, isEstimate: false });
    assert.equal(evNodeSocLabel(r), '');
});

test('a flat car is shown as 0%, not hidden', () => {
    assert.equal(evNodeSocLabel({ soc: 0, isEstimate: false }), '0%');
});

test('the label rounds like the battery cell does', () => {
    assert.equal(evNodeSocLabel({ soc: 64.4 }), '64%');
    assert.equal(evNodeSocLabel({ soc: 64.6 }), '65%');
});
