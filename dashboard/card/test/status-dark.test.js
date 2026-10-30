/**
 * #1197 — the battery status said "Idle" while every battery power sensor
 * was dark.
 *
 * SEM now publishes no battery status while it cannot read any battery
 * power, and no grid status while the meter is dark (the entity holds its
 * last word for the dark-read grace, then shows unavailable). The cards had
 * two problems with that:
 *
 * 1. A card skips its redraw while SEM is out, so it does not flash zeros.
 *    It asked ONE entity: the battery card asked the battery status, the
 *    Home card and the diagram the solar power. That entity can be out on
 *    its own, and then the card stopped drawing and kept old figures that
 *    looked live. It now skips only when every entity it reads is out.
 * 2. With the status out, the battery and grid cards worked the status out
 *    from powers that read 0 by fallback, and said "Idle". They now print
 *    the dash when neither the status nor a power is known.
 *
 * The real card code runs here; lit is replaced by a stub that keeps the
 * template parts (CI runs `node --test` with no node_modules).
 *
 * Run: `npm test` (from dashboard/card).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { readFileSync, readdirSync } from 'node:fs';

const FAKE_LIT = `
export class LitElement {
    constructor() { this.style = {}; this.renderRoot = { querySelector: () => null, querySelectorAll: () => [], getElementById: () => null }; }
    connectedCallback() {}
    disconnectedCallback() {}
    requestUpdate() {}
    updated() {}
    get updateComplete() { return Promise.resolve(true); }
}
export const html = (strings, ...values) => ({ tpl: true, strings: [...strings], values });
export const css = () => '', svg = html;
export const nothing = Symbol('nothing');
export const unsafeSVG = (s) => s;
export const repeat = () => '';
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
    hidden: false, visibilityState: 'visible',
};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
const registry = new Map();
globalThis.customElements = { get: (t) => registry.get(t), define: (t, c) => registry.set(t, c) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.navigator ??= {};
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};
const observer = class { observe() {} disconnect() {} };
globalThis.ResizeObserver = observer;
globalThis.IntersectionObserver = observer;

const { allOut } = await import('../src/util/missing-value.js');
for (const f of ['sem-battery-card', 'sem-grid-card', 'sem-home-status-card',
    'sem-system-diagram-card', 'sem-charger-status-card', 'sem-control-card',
    'sem-config-card']) {
    await import(`../src/cards/${f}.js`);
}

// Template parts → the text a person would read.
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
const words = (s) => s.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

const st = (state) => ({ state: String(state), attributes: {} });
const OUT = st('unavailable');

// A card whose redraws are counted: the hass push either asks for one or
// is skipped.
function counted(tag, config = {}) {
    const el = new (customElements.get(tag))();
    el.setConfig(config);
    el.draws = 0;
    el._scheduleUpdate = () => { el.draws++; };
    return el;
}
const push = (el, states) => { el.hass = { states, language: 'en', config: {} }; };

// ── 1. allOut: SEM out, not one sensor ─────────────────────────────────────

test('allOut is true only when every entity that exists is out', () => {
    const states = { 'a': OUT, 'b': st('unknown'), 'c': st('5') };
    assert.equal(allOut(states, ['a', 'b']), true);
    assert.equal(allOut(states, ['a', 'b', 'c']), false, 'one reading is enough');
    assert.equal(allOut(states, ['a', 'missing']), true, 'an absent entity is skipped');
    assert.equal(allOut(states, ['missing']), false, 'nothing there is not "out"');
    assert.equal(allOut(states, []), false);
    assert.equal(allOut(states, [null, 'c']), false, 'an unmapped id is skipped');
});

// ── 2. The battery card ────────────────────────────────────────────────────

// Every battery power sensor dark: SEM publishes no status and no power; the
// counters beside them still read.
const BATTERY_DARK = {
    'sensor.sem_battery_status': OUT,
    'sensor.sem_battery_power': OUT,
    'sensor.sem_battery_charge_power': OUT,
    'sensor.sem_battery_discharge_power': OUT,
    'sensor.sem_battery_soc': OUT,
    'sensor.sem_battery_cycles_estimated': st('312.4'),
    'sensor.sem_daily_battery_charge_energy': st('4.2'),
};
const BATTERY_LIVE = {
    ...BATTERY_DARK,
    'sensor.sem_battery_status': st('Charging'),
    'sensor.sem_battery_power': st('1500'),
    'sensor.sem_battery_charge_power': st('1500'),
    'sensor.sem_battery_discharge_power': st('0'),
    'sensor.sem_battery_soc': st('60'),
};

test('the battery card draws again when the battery goes dark', () => {
    const el = counted('sem-battery-card');
    push(el, BATTERY_LIVE);
    assert.equal(el.draws, 1);
    push(el, BATTERY_DARK);
    assert.equal(el.draws, 2, 'the card kept the last figures as if live');
});

test('the battery card says nothing is known, not "Idle", while dark', () => {
    const el = counted('sem-battery-card');
    push(el, BATTERY_DARK);
    const all = words(text(el.render()));
    assert.doesNotMatch(all, /\bidle\b/i, all);
    assert.match(all, /status —/i, all);
    assert.match(all, /power — W/i, all);
});

test('a real idle pack still says "Idle"', () => {
    const el = counted('sem-battery-card');
    push(el, { ...BATTERY_LIVE, 'sensor.sem_battery_status': st('Idle'),
        'sensor.sem_battery_power': st('0'), 'sensor.sem_battery_charge_power': st('0') });
    const all = words(text(el.render()));
    assert.match(all, /status idle/i, all);
});

test('a status out with the power read follows the power', () => {
    const el = counted('sem-battery-card');
    push(el, { ...BATTERY_LIVE, 'sensor.sem_battery_status': OUT,
        'sensor.sem_battery_power': st('-2000'), 'sensor.sem_battery_charge_power': st('0'),
        'sensor.sem_battery_discharge_power': st('2000') });
    const all = words(text(el.render()));
    assert.match(all, /status discharging/i, all);
});

test('the battery card still skips while SEM itself is out', () => {
    const el = counted('sem-battery-card');
    push(el, BATTERY_LIVE);
    const out = Object.fromEntries(Object.keys(BATTERY_LIVE).map(k => [k, OUT]));
    push(el, out);
    assert.equal(el.draws, 1, 'every entity out: no redraw, no flash of zeros');
});

test('one battery of two dark is not SEM out', () => {
    const el = counted('sem-battery-card');
    const two = {
        ...BATTERY_LIVE,
        'sensor.sem_battery_b1_power': st('1500'), 'sensor.sem_battery_b1_soc': st('60'),
        'sensor.sem_battery_b2_power': OUT, 'sensor.sem_battery_b2_soc': OUT,
    };
    push(el, two);
    assert.deepEqual(el._batteries, ['b1', 'b2']);
    const fleetOut = Object.fromEntries(Object.keys(BATTERY_LIVE).map(k => [k, OUT]));
    push(el, { ...two, ...fleetOut });
    assert.equal(el.draws, 2, 'battery 1 still reads, so the card draws');
});

// ── 3. The grid card ───────────────────────────────────────────────────────

function gridStatus(states) {
    const el = counted('sem-grid-card');
    push(el, states);
    const raw = text(el.render());
    const m = words(raw);
    return m;
}

test('the grid card says nothing is known, not "Idle", while the meter is dark', () => {
    const m = gridStatus({
        'sensor.sem_grid_status': OUT, 'sensor.sem_grid_power': OUT,
        'sensor.sem_grid_import_power': OUT, 'sensor.sem_grid_export_power': OUT,
    });
    assert.doesNotMatch(m, /\bidle\b/i, m);
    assert.match(m, /—/, m);
});

test('the grid card still says "Idle" for a quiet meter', () => {
    const m = gridStatus({
        'sensor.sem_grid_status': st('Idle'), 'sensor.sem_grid_power': st('0'),
        'sensor.sem_grid_import_power': st('0'), 'sensor.sem_grid_export_power': st('0'),
    });
    assert.match(m, /\bidle\b/i, m);
});

test('a grid status out with the powers read follows the powers', () => {
    const m = gridStatus({
        'sensor.sem_grid_status': OUT, 'sensor.sem_grid_power': st('900'),
        'sensor.sem_grid_import_power': st('900'), 'sensor.sem_grid_export_power': st('0'),
    });
    assert.match(m, /\bimporting\b/i, m);
});

// ── 4. The same skip on the other cards ────────────────────────────────────

test('the Home card draws again while only the solar power is out', () => {
    const el = counted('sem-home-status-card');
    const live = { 'sensor.sem_solar_power': st('800'), 'sensor.sem_battery_soc': st('60'),
        'sensor.sem_autarky_rate': st('70') };
    push(el, live);
    assert.equal(el.draws, 1);
    push(el, { ...live, 'sensor.sem_solar_power': OUT, 'sensor.sem_battery_soc': st('59') });
    assert.equal(el.draws, 2, 'its first sensor out froze the whole card');
    push(el, { 'sensor.sem_solar_power': OUT, 'sensor.sem_battery_soc': OUT,
        'sensor.sem_autarky_rate': OUT });
    assert.equal(el.draws, 2, 'every entity out: still skipped');
});

test('the diagram draws again while only the solar power is out', () => {
    const el = counted('sem-system-diagram-card');
    el._startTickIfIdle = () => {};
    const live = { 'sensor.sem_solar_power': st('800'), 'sensor.sem_battery_power': st('500'),
        'sensor.sem_grid_import_power': st('0'), 'sensor.sem_home_consumption_power': st('300') };
    push(el, live);
    assert.equal(el.draws, 1);
    push(el, { ...live, 'sensor.sem_solar_power': OUT, 'sensor.sem_battery_power': st('-400') });
    assert.equal(el.draws, 2, 'a dark inverter froze the battery, grid and home');
    push(el, Object.fromEntries(Object.keys(live).map(k => [k, OUT])));
    assert.equal(el.draws, 2, 'every entity out: still skipped');
});

test('the charger card draws again while one charger of two is out', () => {
    const el = counted('sem-charger-status-card');
    const live = { 'sensor.sem_charger_a_power': st('0'), 'sensor.sem_charger_b_power': st('3000') };
    push(el, live);
    push(el, live);                      // the first push finds the chargers
    const before = el.draws;
    push(el, { ...live, 'sensor.sem_charger_a_power': OUT, 'sensor.sem_charger_b_power': st('3200') });
    assert.equal(el.draws, before + 1, 'the first charger out froze the card');
    push(el, { 'sensor.sem_charger_a_power': OUT, 'sensor.sem_charger_b_power': OUT });
    assert.equal(el.draws, before + 1, 'every charger out: still skipped');
});

test('the diagram in entities mode never skips on the user\'s own entities', () => {
    // They do not go out with SEM; one dark inverter must not freeze it.
    const el = counted('sem-system-diagram-card', {
        entities: { solar: { entity: 'sensor.inv_pv' }, battery: { entity: 'sensor.inv_batt' } },
    });
    el._startTickIfIdle = () => {};
    push(el, { 'sensor.inv_pv': st('800'), 'sensor.inv_batt': st('500') });
    push(el, { 'sensor.inv_pv': OUT, 'sensor.inv_batt': OUT });
    assert.equal(el.draws, 2, 'the diagram kept the old flows as if live');
});

test('the Control and Configuration cards print the dash, not SOC 0 %', () => {
    const states = { 'sensor.sem_battery_soc': OUT, 'sensor.sem_battery_status': OUT };
    const control = counted('sem-control-card');
    push(control, states);
    const c = words(text(control.render()));
    assert.match(c, /SOC — · —/, c);
    assert.doesNotMatch(c, /SOC 0%/, c);
    const config = counted('sem-config-card');
    push(config, states);
    assert.doesNotMatch(config._batteryZonesSubtitle(), /0%/);
    assert.match(config._batteryZonesSubtitle(), /—$/);
    push(control, { 'sensor.sem_battery_soc': st('0'), 'sensor.sem_battery_status': st('Idle') });
    assert.match(words(text(control.render())), /SOC 0% · idle/i, 'a real empty pack');
});

// ── 5. No card asks one entity whether SEM is out ──────────────────────────

// A `set hass` that returns on one entity reading unavailable is the shape
// that froze these cards. Every out-skip goes through `allOut`.
const SRC = new URL('../src/', import.meta.url);
function hassSetters(rel) {
    const src = readFileSync(new URL(rel, SRC), 'utf8');
    const out = [];
    const re = /\n( *)set hass\(/g;
    let m;
    while ((m = re.exec(src))) {
        const end = src.indexOf(`\n${m[1]}}\n`, m.index + 1);
        out.push(src.slice(m.index, end));
    }
    return out;
}

test('no card skips its redraw on one entity being out', () => {
    const files = ['base/sem-lit-base.js',
        ...readdirSync(new URL('cards/', SRC)).map(f => `cards/${f}`)];
    let setters = 0;
    for (const rel of files) {
        for (const body of hassSetters(rel)) {
            setters++;
            assert.doesNotMatch(body, /'unavailable'|"unavailable"|'unknown'|"unknown"|isMissing\(/,
                `${rel}: set hass asks one entity whether SEM is out — use allOut`);
        }
    }
    assert.ok(setters >= 20, `the scan found only ${setters} hass setters`);
});
