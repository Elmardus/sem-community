/**
 * #1196 — a PV string whose sensor is out must not read "0.00 kW".
 *
 * With two strings, `sensor.sem_pv_string_pv2_power` showed 0 W while string
 * 2's own sensor was unavailable. A string at night also reads 0 W, so the
 * two looked the same. SEM now publishes nothing for that string (the entity
 * holds its last value for the dark-read grace, then shows unavailable). The
 * cards dropped a string whose state was not a number, so with one of two
 * strings dark the whole per-string row went away.
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

const { semDiscoverPVStrings, semPVStringKW, semBuildPVStringsHTML } =
    await import('../src/base/sem-shared.js');
const CARDS = ['sem-solar-card', 'sem-flow-card', 'sem-system-diagram-card'];
for (const f of [...CARDS, 'sem-config-card']) await import(`../src/cards/${f}.js`);

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

const st = (state, attributes = {}) => ({ state: String(state), attributes });
const OUT = st('unavailable');
const P = 'sensor.sem_pv_string_';

// String 1 live, string 2 dark. HA drops a sensor's own attributes while it is
// unavailable; the energy sensor beside it still carries the name.
const ONE_DARK = {
    [`${P}pv1_power`]: st('1800', { string_name: 'East' }),
    [`${P}pv1_daily_energy`]: st('4.2', { string_name: 'East' }),
    [`${P}pv2_power`]: OUT,
    [`${P}pv2_daily_energy`]: st('3.1', { string_name: 'West' }),
};

// ── 1. Discovery keeps a dark string ───────────────────────────────────────

test('one of two strings dark: both stay, the dark one has no watts', () => {
    const found = semDiscoverPVStrings({ states: ONE_DARK }, 'sensor.sem_');
    assert.equal(found.length, 2, 'the per-string row went away');
    const [pv1, pv2] = found;
    assert.equal(pv1.watts, 1800);
    assert.equal(pv2.watts, null, 'not 0, not NaN');
    assert.equal(pv2.name, 'West', 'the name from the energy sensor');
    assert.equal(pv2.energyKwh, 3.1, 'the day energy still reads');
});

test('every shape of a sensor that is out has no watts', () => {
    for (const dark of ['unavailable', 'unknown', 'none', '']) {
        const found = semDiscoverPVStrings({ states: {
            ...ONE_DARK, [`${P}pv2_power`]: st(dark) } }, 'sensor.sem_');
        assert.equal(found.length, 2, dark);
        assert.equal(found[1].watts, null, dark);
    }
});

test('a real 0 W string still reads 0', () => {
    const found = semDiscoverPVStrings({ states: {
        ...ONE_DARK, [`${P}pv2_power`]: st('0', { string_name: 'West' }) } });
    assert.equal(found[1].watts, 0);
});

test('a string only restored from the registry is not a string', () => {
    const states = { ...ONE_DARK,
        [`${P}pv3_power`]: st('unavailable', { restored: true }) };
    const found = semDiscoverPVStrings({ states }, 'sensor.sem_');
    assert.deepEqual(found.map(s => s.slot), ['pv1', 'pv2']);
});

test('the config card counts the strings by the same rule', () => {
    const el = new (customElements.get('sem-config-card'))();
    el._hass = { states: { ...ONE_DARK,
        [`${P}pv3_power`]: st('unavailable', { restored: true }) } };
    assert.deepEqual(el._pvStrings().map(s => s.slot), ['pv1', 'pv2'],
        'a removed string kept its rename row and the count');
});

test('one string alone still gives no per-string row', () => {
    const found = semDiscoverPVStrings({ states: {
        [`${P}pv1_power`]: st('1800') } }, 'sensor.sem_');
    assert.deepEqual(found, []);
});

test('the name falls back to PVn when no sensor carries one', () => {
    const found = semDiscoverPVStrings({ states: {
        [`${P}pv1_power`]: st('1800'), [`${P}pv2_power`]: OUT } });
    assert.equal(found[1].name, 'PV2');
});

// ── 2. The value text ──────────────────────────────────────────────────────

test('semPVStringKW prints the dash for no watts, numbers as before', () => {
    assert.equal(semPVStringKW(null), '— kW');
    assert.equal(semPVStringKW(undefined), '— kW');
    assert.equal(semPVStringKW(NaN), '— kW');
    assert.equal(semPVStringKW(0), '0.00 kW');
    assert.equal(semPVStringKW(1234), '1.23 kW');
    assert.equal(semPVStringKW(-50), '0.05 kW');
});

test('the plain-HTML chip strip prints the dash too', () => {
    const found = semDiscoverPVStrings({ states: ONE_DARK }, 'sensor.sem_');
    const out = words(semBuildPVStringsHTML(found));
    assert.match(out, /PV2 — kW/);
    assert.match(out, /PV1 1\.80 kW/);
});

// ── 3. The three cards ─────────────────────────────────────────────────────

function render(tag, states) {
    const el = new (customElements.get(tag))();
    // The flow card needs a prefix; the others take it as their default.
    el.setConfig(tag === 'sem-flow-card' ? { entity_prefix: 'sensor.sem_' } : {});
    el._hass = { states, language: 'en', config: {} };
    return words(text(el.render()));
}

for (const tag of CARDS) {
    test(`${tag}: the dark string's chip reads "— kW", the live one as before`, () => {
        const out = render(tag, ONE_DARK);
        assert.match(out, /West — kW/, 'the dark string is shown with the dash');
        assert.match(out, /East 1\.80 kW/);
        assert.doesNotMatch(out, /West 0\.00 kW/);
        assert.doesNotMatch(out, /NaN/);
    });

    test(`${tag}: a real 0 W string still prints 0.00 kW`, () => {
        const out = render(tag, { ...ONE_DARK,
            [`${P}pv2_power`]: st('0', { string_name: 'West' }) });
        assert.match(out, /West 0\.00 kW/);
    });
}

test('sem-solar-card: the per-string section reads "— W" for the dark string', () => {
    const out = render('sem-solar-card', ONE_DARK);
    assert.match(out, /West — W 3\.10 kWh/);
    assert.match(out, /East 1\.8 kW 4\.20 kWh/);
});

// ── 4. Every reader of a string's watts handles "no watts" ─────────────────

// Each read of a `.watts` field (any name, `?.`, `['watts']`) must sit
// alone inside a formatter that knows null; destructuring `watts` is a read
// the scan cannot follow, so it fails too. `.watts` is only a PV string's.
const SAFE_READ = /^(semPVStringKW|semFormatPower)\(\s*[\w$]+\s*$/;
function wattsOffenders(src) {
    const out = [];
    let seen = 0;
    for (const m of src.matchAll(/(\??\.watts\b|\[\s*['"]watts['"]\s*\])/g)) {
        seen++;
        const before = src.slice(Math.max(0, m.index - 60), m.index);
        const after = src.slice(m.index + m[0].length, m.index + m[0].length + 2);
        const call = before.match(/(semPVStringKW|semFormatPower)\(\s*[\w$]+\s*$/);
        if (!call || !SAFE_READ.test(call[0]) || m[0] !== '.watts'
                || !/^\s*\)/.test(after)) {
            out.push(before.slice(-30) + m[0] + after);
        }
    }
    for (const m of src.matchAll(/\{[^{}]*\bwatts\b[^{}]*\}\s*(=[^=>]|\)\s*=>)/g)) {
        seen++;
        out.push(m[0]);
    }
    return { out, seen };
}

test('the watts scan catches a read past the formatters', () => {
    const probe = [
        'a(semPVStringKW(s.watts));',             // ok
        'a(semFormatPower(pv.watts));',           // ok
        'a(s.watts.toFixed(2));',
        'a(x?.watts);',
        "a(s['watts']);",
        'a(semFormatPower(s.watts / 1000));',
        'a(Math.abs(s.watts));',
        'const { watts } = s;',
        'list.map(({ watts }) => watts);',
    ].join('\n');
    const { out, seen } = wattsOffenders(probe);
    assert.equal(seen, 9);
    assert.equal(out.length, 7, out.join(' | '));
});

test('every card prints a string\'s watts through a formatter that knows null', () => {
    const dir = new URL('../src/', import.meta.url);
    const files = [];
    const walk = (d) => {
        for (const e of readdirSync(d, { withFileTypes: true })) {
            const u = new URL(e.name + (e.isDirectory() ? '/' : ''), d);
            if (e.isDirectory()) walk(u);
            else if (e.name.endsWith('.js')) files.push(u);
        }
    };
    walk(dir);
    const offenders = [];
    let seen = 0;
    for (const f of files) {
        const r = wattsOffenders(readFileSync(f, 'utf8'));
        seen += r.seen;
        offenders.push(...r.out.map(o => `${f.pathname.split('/src/')[1]}: ${o}`));
    }
    assert.ok(seen >= 5, `the scan saw ${seen} reads — it proves nothing`);
    assert.deepEqual(offenders, []);
});
