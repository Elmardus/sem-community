/**
 * (#1084) The Why? card: rows, the device chips, the test-mode tag and
 * the "Earlier" fold.
 *
 * The real card code runs; lit is replaced by a stub that keeps the
 * template parts, and a template is flattened to text to read it.
 *
 * Run: `npm test` (from dashboard/card).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

const FAKE_LIT = `
export class LitElement {
    constructor() { this.style = {}; }
    connectedCallback() {}
    disconnectedCallback() {}
    requestUpdate() {}
    updated() {}
}
export const html = (strings, ...values) => ({ tpl: true, strings: [...strings], values });
export const css = () => '', svg = html;
export const nothing = Symbol('nothing');
`;
const LOADER = `
const FAKE = ${JSON.stringify('data:text/javascript,' + encodeURIComponent(FAKE_LIT))};
export async function resolve(specifier, context, next) {
    if (specifier === 'lit' || specifier.startsWith('lit/')) return { url: FAKE, shortCircuit: true };
    return next(specifier, context);
}`;
register('data:text/javascript,' + encodeURIComponent(LOADER));

globalThis.window = globalThis;
globalThis.document = {
    addEventListener() {}, removeEventListener() {}, documentElement: {},
    head: { appendChild() {} }, createElement: () => ({}),
};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.dispatchEvent = () => true;
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
const store = new Map();
globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
};
const registry = new Map();
globalThis.customElements = { get: (t) => registry.get(t), define: (t, c) => registry.set(t, c) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });

const rows = await import('../src/util/why-rows.js');
await import('../src/cards/sem-why-card.js');
const Card = customElements.get('sem-why-card');

const WORDS = {
    why_title: 'Why did SEM act?', why_all: 'All', why_observer: 'test mode: not sent',
    why_earlier_today: 'Earlier today ({n})', why_earlier: 'Earlier ({n})',
    why_empty: 'No changes yet.', why_kind_charger: 'Wallbox', why_kind_battery: 'Battery',
    why_kind_load: 'Device', why_in_import: 'Import', why_in_export: 'Export',
    why_in_surplus: 'Surplus', why_in_target: 'Target', why_verb_off: 'off',
    why_verb_on: 'on', why_verb_stop: 'stop', why_verb_normal: 'auto',
    why_verb_limit_discharge: 'limit discharge',
};
const t = (k) => WORDS[k] || k;

/** Today in UTC, at a given hour — the card is told the home is in UTC. */
const at = (h, m = 0, dayOffset = 0) => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() + dayOffset);
    d.setUTCHours(h, m, 0, 0);
    return d.toISOString();
};
const REC = (id, kind, extra = {}) => ({
    time: at(12), device_id: id, device_kind: kind, name: id, old: null,
    new: 'on', reason_code: 'start', text: '', inputs: {}, observer: false, ...extra,
});

function records() {
    return [
        REC('box', 'charger', { time: at(19, 23), old: 'charge 8 A', new: 'stop',
            text: 'No sun left.', inputs: { solar_w: 0, soc: 72, import_price: 0.31 } }),
        REC('b1', 'battery', { time: at(17, 45), old: 'normal', new: 'limit_discharge 1500 W',
            text: 'Selling earns less than nothing.', inputs: { export_rate: -0.02, solar_w: 3100 } }),
        REC('pool', 'load', { time: at(14, 10), name: 'Pool pump', old: 'off', new: 'on',
            observer: true, text: '1.4 kW of sun to spare.', inputs: { surplus_w: 1400 } }),
        REC('box', 'charger', { time: at(11), new: 'charge 6 A' }),
        REC('pool', 'load', { time: at(10), name: 'Pool pump', new: 'off' }),
        REC('box', 'charger', { time: at(9), new: 'charge 10 A' }),
    ];
}

/** A template tree, flattened to its text (events and nothing dropped). */
function text(node) {
    if (node == null || typeof node === 'symbol' || typeof node === 'function') return '';
    if (Array.isArray(node)) return node.map(text).join('');
    if (node.tpl) {
        let out = '';
        node.strings.forEach((s, i) => { out += s; if (i < node.values.length) out += text(node.values[i]); });
        return out;
    }
    return String(node);
}
const rowCount = (s) => (s.match(/class="row"/g) || []).length;

function card(recs) {
    globalThis.semLocalize = (k) => t(k);
    const c = new Card();
    c.setConfig({});
    c._hass = { states: {}, language: 'en', config: { time_zone: 'UTC', currency: 'CHF' } };
    c._records = recs;
    return c;
}

// ── helpers ─────────────────────────────────────────────────────────────

test('a change reads old → new, a charge step is only its amps', () => {
    assert.equal(rows.changeLabel(records()[0], t), '8 A → stop');
    assert.equal(rows.changeLabel(records()[1], t), 'auto → limit discharge 1500 W');
    assert.equal(rows.changeLabel(REC('x', 'load', { new: 'on' }), t), 'on');
    assert.equal(rows.commandLabel('mystery_verb', t), 'mystery verb');
});

test('a device named only by its id takes the kind word', () => {
    assert.equal(rows.deviceName(records()[0], t), 'Wallbox');
    assert.equal(rows.deviceName(records()[0], t, true), 'Wallbox box');
    assert.equal(rows.deviceName(records()[2], t), 'Pool pump');
});

test('chips: one per device with rows, in row order', () => {
    const chips = rows.whyChips(records(), t);
    assert.deepEqual(chips.map((c) => c.name), ['Wallbox', 'Battery', 'Pool pump']);
    assert.deepEqual(chips.map((c) => c.key), ['charger:box', 'battery:b1', 'load:pool']);
    assert.equal(rows.filterRecords(records(), 'load:pool').length, 2);
    assert.equal(rows.filterRecords(records(), '').length, 6);
});

test('inputs: sun and SOC with icons, prices and surplus in words', () => {
    const chips = rows.inputChips(records()[0].inputs, t, 'CHF');
    assert.deepEqual(chips.map((c) => c.text), ['0 W', '72 %', 'Import 0.31 CHF/kWh']);
    assert.ok(chips[0].icon && chips[1].icon && !chips[2].icon);
    assert.deepEqual(rows.inputChips({ surplus_w: 1400, export_rate: -0.02 }, t).map((c) => c.text),
        ['Export -0.02/kWh', 'Surplus 1.4 kW']);
    assert.deepEqual(rows.inputChips(null, t), []);
});

test('Everyday keeps four rows open, the rest go in the fold', () => {
    const { shown, earlier } = rows.splitRows(records());
    assert.equal(shown.length, 4);
    assert.equal(earlier.length, 2);
});

test('the fold says "today" only when every folded row is from today', () => {
    const now = new Date();
    assert.equal(rows.earlierKey(records().slice(4), now, 'UTC'), 'why_earlier_today');
    assert.equal(rows.earlierKey([REC('x', 'load', { time: at(9, 0, -1) })], now, 'UTC'), 'why_earlier');
});

test('time: HH:MM today, a weekday before', () => {
    assert.equal(rows.rowTime(at(19, 23), 'en', new Date(), 'UTC'), '19:23');
    assert.match(rows.rowTime(at(19, 23, -2), 'en', new Date(), 'UTC'), /^[A-Z][a-z]{2} 19:23$/);
});

test('the read stamp moves when a newest decision moves', () => {
    const s = (time) => ({ 'sensor.sem_charging_state': { attributes: { per_charger_last_decision: { box: { time } } } } });
    assert.notEqual(rows.lastDecisionStamp(s('a')), rows.lastDecisionStamp(s('b')));
    assert.equal(rows.lastDecisionStamp({}), '0|');
});

// ── the card ────────────────────────────────────────────────────────────

test('the card renders the newest rows with reason and inputs', () => {
    const out = text(card(records()).render());
    assert.match(out, /Why did SEM act\?/);
    assert.equal(rowCount(out), 4);
    assert.match(out, /8 A → stop/);
    assert.match(out, /No sun left\./);
    assert.match(out, /Import 0\.31 CHF\/kWh/);
    assert.match(out, /Earlier today \(2\)/);
    assert.match(out, /aria-expanded=false/);
});

test('only an observer row carries the test-mode tag', () => {
    const out = text(card(records()).render());
    assert.equal((out.match(/test mode: not sent/g) || []).length, 1);
    const pool = out.split('class="row"').slice(1).find((r) => r.includes('Pool pump'));
    assert.match(pool, /test mode: not sent/);
});

test('a chip filters the rows to one device', () => {
    const c = card(records());
    c._setChip('load:pool');
    const out = text(c.render());
    assert.equal(rowCount(out), 2);
    assert.doesNotMatch(out, /8 A → stop/);
    assert.doesNotMatch(out, /Earlier/);
});

test('a chip whose device left the log falls back to All', () => {
    const c = card(records());
    c._setChip('load:gone');
    assert.equal(rowCount(text(c.render())), 4);
});

test('the fold opens the rest; All settings opens it by default', () => {
    const c = card(records());
    c._toggleFold();
    let out = text(c.render());
    assert.equal(rowCount(out), 6);
    assert.match(out, /aria-expanded=true/);
    const d = card(records());
    d._applyViewMode(true);
    out = text(d.render());
    assert.equal(rowCount(out), 6);
    // switching back closes it again
    d._toggleFold();
    d._applyViewMode(false);
    assert.equal(rowCount(text(d.render())), 4);
});

test('an empty log says so; it reads the records over the websocket', async () => {
    const out = text(card([]).render());
    assert.match(out, /No changes yet\./);
    const c = card(null);
    const calls = [];
    c._hass.callWS = async (msg) => { calls.push(msg); return { records: records() }; };
    await c._load();
    assert.deepEqual(calls, [{ type: 'solar_energy_management/decision_log' }]);
    assert.equal(c._records.length, 6);
});

test('two named chargers: each row and chip shows its own name, prices in CHF', () => {
    const recs = [
        REC('c1', 'charger', { name: 'Garage', time: at(19), old: 'charge 8 A', new: 'stop',
            inputs: { import_price: 0.31, export_rate: 0.06 } }),
        REC('c2', 'charger', { name: 'Carport', time: at(18), new: 'charge 6 A' }),
    ];
    assert.deepEqual(rows.whyChips(recs, t).map((c) => c.name), ['Garage', 'Carport']);
    const out = text(card(recs).render());
    assert.match(out, /class="name">Garage</);
    assert.match(out, /class="name">Carport</);
    assert.doesNotMatch(out, /Wallbox/);
    assert.match(out, /Import 0\.31 CHF\/kWh/);
    assert.match(out, /Export 0\.06 CHF\/kWh/);
    assert.doesNotMatch(out, /€/);
});

test('two chargers named only by id: the kind word plus the id', () => {
    const recs = [REC('c1', 'charger', { name: 'c1' }), REC('c2', 'charger', { name: 'c2' })];
    assert.deepEqual(rows.whyChips(recs, t).map((c) => c.name), ['Wallbox c1', 'Wallbox c2']);
});
