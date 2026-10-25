/**
 * #1170 — a battery whose power sensor is out must not read "0 W, Idle".
 *
 * With two batteries, `sensor.sem_battery_b2_power` showed 0 W while
 * battery 2's own sensor was unavailable. An idle pack also reads 0 W, so the
 * two looked the same. SEM now publishes nothing for that battery's power and
 * status (the entity holds its last value for the dark-read grace, then shows
 * unavailable). The card read an unavailable power as 0 and an empty status
 * as "idle", so it still drew the same lie.
 *
 * The real card code runs here; lit is replaced by a stub that keeps the
 * template parts (CI runs `node --test` with no node_modules).
 *
 * Run: `npm test` (from dashboard/card).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

const FAKE_LIT = `
export class LitElement {
    constructor() { this.style = {}; this.renderRoot = { querySelector: () => null, querySelectorAll: () => [] }; }
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
const registry = new Map();
globalThis.customElements = { get: (t) => registry.get(t), define: (t, c) => registry.set(t, c) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.navigator ??= {};

await import('../src/cards/sem-battery-card.js');

// Template parts → the text a person would read (attribute values included,
// which only makes the "no 0 W" checks stricter).
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

function card(states) {
    const el = new (customElements.get('sem-battery-card'))();
    el.setConfig({});
    el._hass = { states, language: 'en', config: {} };
    return el;
}

const UNIT = (bid, power, status, soc = '60') => ({
    [`sensor.sem_battery_${bid}_power`]: { state: power, attributes: {} },
    [`sensor.sem_battery_${bid}_status`]: { state: status, attributes: {} },
    [`sensor.sem_battery_${bid}_soc`]: { state: soc, attributes: {} },
    [`sensor.sem_battery_${bid}_capacity_kwh`]: { state: '10', attributes: {} },
});

test('a dark battery reads "— W" with no status, not "0 W" and idle', () => {
    const el = card({ ...UNIT('b1', '1500', 'charging'), ...UNIT('b2', 'unavailable', 'unavailable', 'unknown') });
    const raw = text(el._renderBatterySection('b2', 1));
    const row = words(raw);
    assert.match(row, /— W/, row);
    assert.doesNotMatch(row, /(^|[^0-9.])0 W/, row);
    assert.doesNotMatch(row, /\bidle\b/, row);
    assert.match(raw, /class="battery-section-status"[^>]*>\s*—\s*</,
        'the status cell itself is the dash');
});

test('the other battery still reads its own power and status', () => {
    const el = card({ ...UNIT('b1', '1500', 'charging'), ...UNIT('b2', 'unavailable', 'unavailable') });
    const row = words(text(el._renderBatterySection('b1', 0)));
    assert.match(row, /1\.5 kW/, row);
    assert.match(row, /\bcharging\b/, row);
});

test('a real 0 W idle pack still says 0 W and idle', () => {
    const el = card({ ...UNIT('b1', '1500', 'charging'), ...UNIT('b2', '0', 'idle') });
    const row = words(text(el._renderBatterySection('b2', 1)));
    assert.match(row, /(^|[^0-9.])0 W/, row);
    assert.match(row, /\bidle\b/, row);
});

test('a power with no status still says what it does', () => {
    // The status entity can be out while the power reads (it is not read
    // yet after a restart); the row follows the number.
    const el = card({ ...UNIT('b1', '0', 'idle'), ...UNIT('b2', '-2000', 'unavailable') });
    const row = words(text(el._renderBatterySection('b2', 1)));
    assert.match(row, /-2\.0 kW/, row);
    assert.match(row, /\bdischarging\b/, row);
});

test('the fleet power cell reads "— W" while the fleet sensor is out', () => {
    const el = card({
        'sensor.sem_battery_power': { state: 'unavailable', attributes: {} },
        'sensor.sem_battery_soc': { state: '55', attributes: {} },
        'sensor.sem_battery_status': { state: 'Idle', attributes: {} },
    });
    const all = words(text(el.render()));
    assert.match(all, /power — W/, 'the fleet power cell');
});
