/**
 * #1221 — the Energy Impact card shows the CO2 sensor's own figure.
 *
 * The card worked "CO2 today" out again: (solar → home + solar → EV) ×
 * 128 g/kWh. `sensor.sem_daily_co2_avoided` counts all solar used on site,
 * also what went into the battery, and adds the live grid value when that
 * is on. On a day the battery charged from the sun the card showed less
 * than the sensor beside it.
 *
 * The hero is now the sensor's state; the line under it is the kWh and the
 * kg per kWh the sensor publishes (`solar_used_kwh`, `co2_kg_per_kwh_today`).
 * A sensor that is out prints "—", not 0 (class 164). The card draws again
 * when only those attributes change (class 127).
 *
 * The real card code runs here; lit is replaced by a stub that keeps the
 * template parts (CI runs `node --test` with no node_modules).
 *
 * Run: `npm test` (from dashboard/card).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { readFileSync } from 'node:fs';

const FAKE_LIT = `
export class LitElement {
    constructor() { this.style = {}; this.renderRoot = { querySelector: () => null, querySelectorAll: () => [] }; }
    connectedCallback() {}
    disconnectedCallback() {}
    requestUpdate() { this.__updates = (this.__updates || 0) + 1; }
    updated() {}
    getRootNode() { return {}; }
    get updateComplete() { return Promise.resolve(true); }
}
export const html = (strings, ...values) => ({ tpl: true, strings: [...strings], values });
export const css = () => '', svg = html;
export const nothing = Symbol('nothing');
export const unsafeSVG = (s) => s;
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
    addEventListener() {}, removeEventListener() {},
    documentElement: {}, head: { appendChild() {} }, createElement: () => ({}),
};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = () => 0;
const registry = new Map();
globalThis.customElements = { get: (t) => registry.get(t), define: (t, c) => registry.set(t, c) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.navigator ??= {};

await import('../src/cards/sem-energy-impact-card.js');

function text(part) {
    if (part == null || typeof part === 'symbol' || part === false) return '';
    if (Array.isArray(part)) return part.map(text).join('');
    if (part && part.tpl) {
        let out = '';
        part.strings.forEach((s, i) => {
            out += s;
            if (i < part.values.length) out += text(part.values[i]);
        });
        return out;
    }
    return String(part);
}

/** The text inside the first element with this class. */
function inClass(rendered, cls) {
    const m = text(rendered).match(new RegExp(`class="${cls}"[^>]*>([^<]*)<`));
    return m ? m[1].trim() : null;
}
function allInClass(rendered, cls) {
    return [...text(rendered).matchAll(new RegExp(`class="${cls}"[^>]*>([^<]*)<`, 'g'))]
        .map((m) => m[1].trim());
}

const S = (state, attributes = {}) => ({ state: String(state), attributes });

// The reporter's shape: a sunny day, 6 kWh of solar into the battery. The
// flows the old card read say 11.7 kWh; the sensor counts 17.7 kWh.
function sunnyDay(prefix = 'sensor.sem_') {
    return {
        [`${prefix}flow_solar_to_home_energy`]: S(9.2),
        [`${prefix}flow_solar_to_ev_energy`]: S(2.5),
        [`${prefix}flow_solar_to_battery_energy`]: S(6.0),
        [`${prefix}daily_co2_avoided`]: S(2.27, {
            solar_used_kwh: 17.7, co2_kg_per_kwh_today: 0.128,
            co2_live: false, co2_fallback_kg_per_kwh: 0.128,
        }),
        [`${prefix}yearly_co2_avoided`]: S(412.3),
        [`${prefix}lifetime_co2_avoided`]: S(1880.4),
        [`${prefix}yearly_trees_equivalent`]: S(18.7),
        [`${prefix}lifetime_trees_equivalent`]: S(85.5),
    };
}

function card(states, config = {}) {
    const el = new (customElements.get('sem-energy-impact-card'))();
    el.setConfig({ type: 'custom:sem-energy-impact-card', ...config });
    el.hass = { states, language: 'en' };
    return el;
}

test('the hero is the sensor, not two flows × 128 g/kWh', () => {
    const out = card(sunnyDay()).render();
    assert.equal(inClass(out, 'tile-hero'), '2.27 kg');
    // the old card: (9.2 + 2.5) × 0.128 = 1.5 kg
    assert.notEqual(inClass(out, 'tile-hero'), '1.5 kg');
});

test('the line under it is the kWh and the rate the sensor used', () => {
    const out = card(sunnyDay()).render();
    assert.equal(inClass(out, 'tile-detail'), '17.7 kWh × 128 g/kWh');
});

test('with the live grid value on, the line shows the day\'s rate', () => {
    const st = sunnyDay();
    st['sensor.sem_daily_co2_avoided'] = S(1.68, {
        solar_used_kwh: 17.7, co2_kg_per_kwh_today: 0.0951, co2_live: true,
    });
    const out = card(st).render();
    assert.equal(inClass(out, 'tile-hero'), '1.68 kg');
    assert.equal(inClass(out, 'tile-detail'), '17.7 kWh × 95 g/kWh');
});

test('a real 0 at night stays 0', () => {
    const st = sunnyDay();
    st['sensor.sem_daily_co2_avoided'] = S(0, { solar_used_kwh: 0, co2_kg_per_kwh_today: 0.128 });
    const out = card(st).render();
    assert.equal(inClass(out, 'tile-hero'), '0.00 kg');
    assert.equal(inClass(out, 'tile-detail'), '0.0 kWh × 128 g/kWh');
});

for (const state of ['unavailable', 'unknown', 'n/a']) {
    test(`a sensor that is "${state}" prints "—", and no line under it`, () => {
        const st = sunnyDay();
        for (const k of Object.keys(st)) {
            if (!k.includes('flow_')) st[k] = S(state);
        }
        const out = card(st).render();
        assert.equal(inClass(out, 'tile-hero'), '—');
        assert.equal(inClass(out, 'tile-detail'), '');
        for (const v of allInClass(out, 'stat-val')) assert.equal(v, '—');
        assert.doesNotMatch(text(out), /NaN/);
    });
}

test('a sensor with no detail attributes (an older SEM) shows no line', () => {
    const st = sunnyDay();
    st['sensor.sem_daily_co2_avoided'] = S(2.27);
    const out = card(st).render();
    assert.equal(inClass(out, 'tile-hero'), '2.27 kg');
    assert.equal(inClass(out, 'tile-detail'), '');
});

test('the year and lifetime figures read their sensors', () => {
    const out = card(sunnyDay()).render();
    assert.deepEqual(allInClass(out, 'stat-val'), ['412 kg', '1880 kg', '86']);
    assert.deepEqual(allInClass(out, 'tile-hero'), ['2.27 kg', '19'], 'today, trees this year');
});

test('a configured prefix reads its own sensor', () => {
    const out = card(sunnyDay('sensor.sem2_'), { entity_prefix: 'sensor.sem2_' }).render();
    assert.equal(inClass(out, 'tile-hero'), '2.27 kg');
    assert.equal(inClass(out, 'tile-detail'), '17.7 kWh × 128 g/kWh');
});

test('a configured prefix draws again on its own attributes', () => {
    const st = sunnyDay('sensor.sem2_');
    const el = card(st, { entity_prefix: 'sensor.sem2_' });
    let scheduled = 0;
    el._scheduleUpdate = () => { scheduled++; };
    const day = st['sensor.sem2_daily_co2_avoided'];
    el.hass = { states: { ...st, 'sensor.sem2_daily_co2_avoided': S(day.state, {
        ...day.attributes, solar_used_kwh: 17.8 }) }, language: 'en' };
    assert.equal(scheduled, 1);
});

test('the figures print the unit the sensor shows in', () => {
    const st = sunnyDay();
    st['sensor.sem_daily_co2_avoided'] = S(5.0, {
        ...st['sensor.sem_daily_co2_avoided'].attributes, unit_of_measurement: 'lb' });
    st['sensor.sem_yearly_co2_avoided'] = S(908.9, { unit_of_measurement: 'lb' });
    const out = card(st).render();
    assert.equal(inClass(out, 'tile-hero'), '5.00 lb');
    assert.deepEqual(allInClass(out, 'stat-val'), ['909 lb', '1880 kg', '86']);
});

test('the card draws again when only the detail attributes change', () => {
    const st = sunnyDay();
    const el = card(st);
    let scheduled = 0;
    el._scheduleUpdate = () => { scheduled++; };
    el.hass = { states: { ...st }, language: 'en' };
    assert.equal(scheduled, 0, 'nothing changed');
    const day = st['sensor.sem_daily_co2_avoided'];
    el.hass = { states: { ...st, 'sensor.sem_daily_co2_avoided': S(day.state, {
        ...day.attributes, solar_used_kwh: 17.8 }) }, language: 'en' };
    assert.equal(scheduled, 1, 'the kWh moved without the state');
    el.hass = { states: { ...st, 'sensor.sem_daily_co2_avoided': S(day.state, {
        ...day.attributes, solar_used_kwh: 17.8, co2_kg_per_kwh_today: 0.1 }) }, language: 'en' };
    assert.equal(scheduled, 2, 'the rate moved without the state');
    el.hass = { states: { ...st, 'sensor.sem_daily_co2_avoided': S(day.state, {
        ...day.attributes, solar_used_kwh: 17.8, co2_kg_per_kwh_today: 0.1, co2_source: 'x' }) },
        language: 'en' };
    assert.equal(scheduled, 2, 'an attribute the card does not show');
});

test('the card source keeps no CO2 formula of its own', () => {
    const src = readFileSync(new URL('../src/cards/sem-energy-impact-card.js', import.meta.url), 'utf8');
    const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
    assert.doesNotMatch(code, /0\.128|\b128\b/, 'a fixed grid rate');
    assert.doesNotMatch(code, /flow_solar_to/, 'a flow read to rebuild the figure');
});
