/**
 * #1198 — the figures at the top of each tab must not print 0 for a sensor
 * that is out.
 *
 * `_getState` handed back its fallback (0, and 100 for battery health) for
 * an unavailable or unknown state, and every tab printed it: a battery power
 * sensor that was out read "0 W" and the SOC "0%", the same as an idle,
 * empty pack. A state that was not a number got through as NaN. Found while
 * fixing #1170, whose battery card already shows "— W".
 *
 * The figures now read "—" when their sensor is out. A real 0 still reads 0.
 * The card draws again when a figure it shows changes: the EV tab's session
 * and the Config tab's SOC were not in its hand-made list (class 127).
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

await import('../src/cards/sem-tab-header.js');

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

function header(tab, states) {
    const el = new (customElements.get('sem-tab-header'))();
    el.setConfig({ tab });
    el.hass = { states, language: 'en', config: { currency: 'EUR' } };
    return el;
}

// Every SEM sensor a tab's figures read. `target_peak_limit` has its own
// reader (`peakLimitText`, #716); it is here so the Control tab is whole.
const READS = {
    home: ['daily_solar_energy', 'solar_power'],
    energy: ['daily_solar_energy', 'daily_home_energy', 'self_consumption_rate'],
    battery: ['battery_soc', 'battery_power', 'battery_health_score'],
    ev: ['ev_power', 'daily_ev_energy', 'session_energy'],
    control: ['target_peak_limit', 'controllable_devices_count', 'surplus_active_devices'],
    config: ['battery_soc'],
    costs: ['daily_costs', 'daily_savings', 'daily_net_cost'],
    system: ['energy_optimization_score', 'lifetime_total_savings', 'lifetime_co2_avoided'],
};

const all = (tab, state) => Object.fromEntries(
    READS[tab].map((s) => [`sensor.sem_${s}`, { state, attributes: {} }]));

// The source must list every figure a tab reads, or the tests below would
// pass on a figure nobody gave them.
test('the list of reads covers every figure the header prints', () => {
    const src = readFileSync(new URL('../src/cards/sem-tab-header.js', import.meta.url), 'utf8');
    const reads = new Set([...src.matchAll(/_getState\('([a-z0-9_]+)'/g)].map((m) => m[1]));
    const listed = new Set(Object.values(READS).flat());
    for (const r of reads) assert.ok(listed.has(r), `${r} is read but not tested`);
});

test('_getState takes no fallback: a sensor that is out is null', () => {
    const src = readFileSync(new URL('../src/cards/sem-tab-header.js', import.meta.url), 'utf8');
    assert.doesNotMatch(src, /_getState\([^)]*,/, 'a figure passes a fallback number again');
});

for (const state of ['unavailable', 'unknown', 'n/a']) {
    for (const tab of Object.keys(READS)) {
        test(`${tab} tab: a sensor that is "${state}" prints "—", not a number`, () => {
            const el = header(tab, all(tab, state));
            const figures = tab === 'home'
                ? [words(text(el._renderHomeHero()))]
                : el._getStatValues();
            for (const [i, f] of figures.entries()) {
                // The Config tab's first figure counts charger entities; the
                // heat pump has its own test below.
                if (tab === 'config' && i < 2) continue;
                if (tab === 'home') {
                    assert.match(f, /^— /, `the hero: ${f}`);
                    assert.doesNotMatch(f, /\d|NaN/, `the hero: ${f}`);
                    continue;
                }
                assert.equal(f, '—', `${tab}: ${figures.join(' | ')}`);
            }
        });
    }
}

test('a missing sensor prints "—" too', () => {
    for (const tab of Object.keys(READS)) {
        if (tab === 'home' || tab === 'config') continue;
        const figures = header(tab, {})._getStatValues();
        assert.deepEqual(figures, ['—', '—', '—'], tab);
    }
    assert.equal(header('config', {})._getStatValues()[2], '—');
    assert.match(words(text(header('home', {})._renderHomeHero())), /^— /);
});

test('config tab: the heat pump is read from its binary sensor', () => {
    const hp = (state) => header('config', state === undefined ? {}
        : { 'binary_sensor.sem_heat_pump_registered': { state, attributes: {} } })._getStatValues()[1];
    assert.equal(hp('on'), 'on', 'a registered heat pump said Off');
    assert.equal(hp('off'), 'off');
    assert.equal(hp(undefined), 'off', 'no entity: no heat pump module');
    assert.equal(hp('unavailable'), '—');
    assert.equal(hp('unknown'), '—');
    // The sensor. id does not exist; a stray one must not decide.
    const stray = header('config', {
        'sensor.sem_heat_pump_registered': { state: 'on', attributes: {} },
    })._getStatValues()[1];
    assert.equal(stray, 'off');
});

test('battery tab: a power sensor that is out is not "0 W", the SOC not "0%"', () => {
    const el = header('battery', {
        'sensor.sem_battery_power': { state: 'unavailable', attributes: {} },
        'sensor.sem_battery_soc': { state: 'unavailable', attributes: {} },
        'sensor.sem_battery_health_score': { state: '96', attributes: {} },
    });
    assert.deepEqual(el._getStatValues(), ['—', '—', '96%']);
});

test('battery tab: health that is out is not "100%"', () => {
    const el = header('battery', {
        'sensor.sem_battery_power': { state: '0', attributes: {} },
        'sensor.sem_battery_soc': { state: '40', attributes: {} },
        'sensor.sem_battery_health_score': { state: 'unavailable', attributes: {} },
    });
    assert.deepEqual(el._getStatValues(), ['40%', '0 W', '—']);
});

test('a real 0 still prints 0 on every tab', () => {
    const want = {
        energy: ['0.0 kWh', '0.0 kWh', '0%'],
        battery: ['0%', '0 W', '0%'],
        ev: ['0 W', '0.0 kWh', '0.0 kWh'],
        costs: ['0.00 EUR', '0.00 EUR', '+0.00 EUR'],
        system: ['0', '0 EUR', '0 kg'],
    };
    for (const [tab, values] of Object.entries(want)) {
        assert.deepEqual(header(tab, all(tab, '0'))._getStatValues(), values, tab);
    }
    const control = header('control', all('control', '0'))._getStatValues();
    assert.deepEqual(control.slice(1), ['0', '0']);
    assert.equal(header('config', all('config', '0'))._getStatValues()[2], '0%');
    assert.match(words(text(header('home', all('home', '0'))._renderHomeHero())), /^0\.0 kWh/);
});

test('real readings print as before', () => {
    const el = header('battery', {
        'sensor.sem_battery_power': { state: '-2400', attributes: {} },
        'sensor.sem_battery_soc': { state: '87.6', attributes: {} },
        'sensor.sem_battery_health_score': { state: '98.2', attributes: {} },
    });
    assert.deepEqual(el._getStatValues(), ['88%', '-2.4 kW', '98%']);
    const costs = header('costs', {
        'sensor.sem_daily_costs': { state: '3.456', attributes: {} },
        'sensor.sem_daily_savings': { state: '1.2', attributes: {} },
        'sensor.sem_daily_net_cost': { state: '2.256', attributes: {} },
    });
    assert.deepEqual(costs._getStatValues(), ['3.46 EUR', '1.20 EUR', '2.26 EUR']);
    // #554: a net gain reads as "+".
    const gain = header('costs', { 'sensor.sem_daily_net_cost': { state: '-1.5', attributes: {} } });
    assert.equal(gain._getStatValues()[2], '+1.50 EUR');
    const hero = words(text(header('home', {
        'sensor.sem_daily_solar_energy': { state: '12.34', attributes: {} },
        'sensor.sem_solar_power': { state: '1500', attributes: {} },
    })._renderHomeHero()));
    assert.match(hero, /^12\.3 kWh/);
    assert.match(hero, /1\.5 kW/);
});

// Class 127: a figure the header shows must be one it draws again for.
function redraws(tab, base, suffix, a, b) {
    const el = header(tab, { ...base, [`sensor.sem_${suffix}`]: { state: a, attributes: {} } });
    const before = el.__updates || 0;
    el.hass = { states: { ...base, [`sensor.sem_${suffix}`]: { state: b, attributes: {} } },
        language: 'en', config: { currency: 'EUR' } };
    return (el.__updates || 0) > before;
}

test('every figure the header shows draws it again when it changes', () => {
    for (const [tab, reads] of Object.entries(READS)) {
        const base = all(tab, '5');
        for (const s of reads) {
            if (s === 'target_peak_limit') continue;
            assert.ok(redraws(tab, base, s, '5', '7'), `${tab}: ${s} changed, no redraw`);
            assert.ok(redraws(tab, base, s, '5', 'unavailable'), `${tab}: ${s} went out, no redraw`);
        }
    }
});

test('the heat pump, the "no grid limit" flag and the currency draw it again', () => {
    const states = (extra) => ({ ...all('control', '5'), ...extra });
    const el = header('control', states({
        'sensor.sem_target_peak_limit': { state: '8.0', attributes: { peak_limit_unlimited: false } },
    }));
    let before = el.__updates || 0;
    el.hass = { states: states({
        'sensor.sem_target_peak_limit': { state: '8.0', attributes: { peak_limit_unlimited: true } },
    }), language: 'en', config: { currency: 'EUR' } };
    assert.ok((el.__updates || 0) > before, 'the flag alone changed, no redraw');

    const cfg = header('config', { 'binary_sensor.sem_heat_pump_registered': { state: 'off', attributes: {} } });
    before = cfg.__updates || 0;
    cfg.hass = { states: { 'binary_sensor.sem_heat_pump_registered': { state: 'on', attributes: {} } },
        language: 'en', config: { currency: 'EUR' } };
    assert.ok((cfg.__updates || 0) > before, 'the heat pump changed, no redraw');

    const costs = header('costs', all('costs', '5'));
    before = costs.__updates || 0;
    costs.hass = { states: all('costs', '5'), language: 'en', config: { currency: 'CHF' } };
    assert.ok((costs.__updates || 0) > before, 'the currency changed, no redraw');
});

test('a change the header does not show draws nothing', () => {
    assert.equal(redraws('battery', all('battery', '5'), 'grid_power', '5', '7'), false);
});
