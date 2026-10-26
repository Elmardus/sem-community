/**
 * (#1211 follow-up) `show_ev_soc: false` hides the car's state of charge at the
 * diagram's EV node.
 *
 * SEM models one car per charger (docs/KNOWN_LIMITATIONS.md, "One vehicle per
 * charger"). A home whose two cars share one charger would see one car's
 * figure while the other is plugged in, with no way to tell the diagram so.
 * The option defaults to on, so nothing changes for anyone who does not set it.
 *
 * The real card code runs here; lit is replaced by a stub (CI runs
 * `node --test` with no node_modules).
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

const registry = new Map();
globalThis.window = globalThis;
globalThis.document = {
    addEventListener() {}, removeEventListener() {},
    documentElement: {}, head: { appendChild() {} }, createElement: () => ({}),
};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.dispatchEvent = () => true;
globalThis.customElements = { get: (t) => registry.get(t), define: (t, c) => registry.set(t, c) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.navigator ??= {};

await import('../src/cards/sem-system-diagram-card.js');

// One charger, its car at 64 %.
const STATES = {
    'sensor.sem_charger_ev_charger_power': { state: '0' },
    'sensor.sem_charger_ev_charger_vehicle_soc': { state: '64' },
    'sensor.sem_vehicle_soc': { state: '64' },
};

function card(config) {
    const el = new (customElements.get('sem-system-diagram-card'))();
    el.setConfig(config);
    return el;
}

test('by default the EV node shows the car\'s state of charge', () => {
    assert.equal(card({ entity_prefix: 'sensor.sem_' })._evSocLabel({ states: STATES }), '64%');
});

test('show_ev_soc: true is the same as the default', () => {
    assert.equal(card({ entity_prefix: 'sensor.sem_', show_ev_soc: true })._evSocLabel({ states: STATES }), '64%');
});

test('show_ev_soc: false hides it', () => {
    assert.equal(card({ entity_prefix: 'sensor.sem_', show_ev_soc: false })._evSocLabel({ states: STATES }), '');
});

test('show_ev_soc: false hides it in entities mode too', () => {
    const config = { entities: { ev: { entity: 'sensor.wallbox_w', state_of_charge: 'sensor.car_soc' } } };
    const states = { 'sensor.car_soc': { state: '64' } };
    assert.equal(card(config)._evSocLabel({ states }), '64%');
    assert.equal(card({ ...config, show_ev_soc: false })._evSocLabel({ states }), '');
});

test('the option hides only the figure, not the EV node (that is show_ev)', () => {
    const el = card({ entity_prefix: 'sensor.sem_', show_ev_soc: false });
    assert.equal(el._showEv, true);
    assert.equal(el._showEvSoc, false);
});

test('only an explicit false turns it off', () => {
    for (const v of [undefined, null, true, 0, '', 'false']) {
        assert.equal(card({ entity_prefix: 'sensor.sem_', show_ev_soc: v })._showEvSoc, true,
            `show_ev_soc: ${JSON.stringify(v)} must not hide it`);
    }
});
