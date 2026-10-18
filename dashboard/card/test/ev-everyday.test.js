/**
 * The EV tab's two views: Everyday (the default) and All settings.
 *
 * Every charger showed about 35 elements and 18 controls at once. Everyday
 * keeps what is used every day open — state, SOC, power, today, solar share,
 * charge mode, charge target, Charge by — and folds the rest under More and
 * Details. All settings opens both folds. The switch is shared with the
 * Configuration tab and kept per browser (util/view-mode.js).
 *
 * The real card code runs here; lit is replaced by a stub whose html``
 * returns its parts, so a render can be read as text and its handlers called.
 *
 * Run: `npm test` (from dashboard/card).
 */
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { readFileSync } from 'node:fs';

const FAKE_LIT = `
export class LitElement {
    constructor() { this.style = {}; this.renderRoot = { querySelector: () => null, querySelectorAll: () => [] }; }
    connectedCallback() {}
    disconnectedCallback() {}
    requestUpdate() {}
    updated() {}
    get updateComplete() { return Promise.resolve(true); }
}
export const nothing = Symbol.for('sem-test-nothing');
const tag = (strings, ...values) => ({ strings, values });
export const html = tag, svg = tag;
export const css = () => '';
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
const listeners = new Map();
globalThis.addEventListener = (t, h) => { if (!listeners.has(t)) listeners.set(t, new Set()); listeners.get(t).add(h); };
globalThis.removeEventListener = (t, h) => listeners.get(t)?.delete(h);
globalThis.dispatchEvent = (ev) => { for (const h of listeners.get(ev.type) || []) h(ev); return true; };
globalThis.document = {
    addEventListener() {}, removeEventListener() {},
    documentElement: {}, head: { appendChild() {} }, createElement: () => ({}), hidden: false,
};
const registry = new Map();
globalThis.customElements = { get: (t) => registry.get(t), define: (t, c) => registry.set(t, c) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
const store = new Map();
globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
};
globalThis.navigator ??= {};

await import('../src/cards/sem-ev-status-card.js');
const { VIEW_MODE_KEY, VIEW_MODE_EVENT, setAllSettings } = await import('../src/util/view-mode.js');

// ── Reading a render ──────────────────────────────────────────────────────

const NOTHING = Symbol.for('sem-test-nothing');
const FN = '«fn»';

/** The render as text; each bound handler is kept with the tag it sits in. */
function read(tree) {
    let out = '';
    const handlers = [];
    const walk = (t) => {
        if (t == null || t === false || t === NOTHING) return;
        if (Array.isArray(t)) { t.forEach(walk); return; }
        if (typeof t === 'function') {
            handlers.push({ tag: out.slice(out.lastIndexOf('<')), fn: t });
            out += FN;
            return;
        }
        if (typeof t === 'object' && Array.isArray(t.strings)) {
            t.strings.forEach((s, i) => { out += s; if (i < t.values.length) walk(t.values[i]); });
            return;
        }
        out += String(t);
    };
    walk(tree);
    const click = (needle) => {
        const h = handlers.filter((x) => x.tag.includes(needle));
        assert.ok(h.length, `no handler on a tag with ${needle}`);
        h[0].fn({ stopPropagation() {}, target: {} });
    };
    return { text: out, handlers, click };
}

function counts(text) {
    const tags = text.match(/<(div|span|button|select|input|ha-icon|svg|b|small)\b[^>]*>/g) || [];
    return { elements: tags.length, controls: tags.filter((t) => t.includes(FN)).length };
}

// ── A home with everything: phase switching, a battery, a pause button ────

const now = Date.now();
const iso = (h) => new Date(now + h * 3600e3).toISOString();

function chargerStates(id) {
    const s = {};
    const set = (eid, state, attributes = {}) => { s[eid] = { state: String(state), attributes }; };
    set(`sensor.sem_charger_${id}_power`, 3700, { charger_name: `Charger ${id}` });
    set(`sensor.sem_charger_${id}_session_energy`, 4.2);
    set(`sensor.sem_charger_${id}_daily_energy`, 6.1);
    set(`sensor.sem_charger_${id}_vehicle_soc`, 55);
    set(`sensor.sem_charger_${id}_estimated_soc`, 58, {
        energy_accounted_soc: 61, vehicle_soc_last: 55,
        vehicle_soc_last_at: new Date(now - 20 * 60000).toISOString() });
    set(`sensor.sem_charger_${id}_commanded_current`, 16);
    set(`binary_sensor.sem_charger_${id}_connected`, 'on');
    set(`select.sem_charger_${id}_charge_mode`, 'solar_plus_battery', {
        options: ['solar_only', 'solar_plus_battery', 'min_plus_solar', 'always_max', 'off'] });
    set(`button.sem_charger_${id}_pause_charging`, 'unknown');
    set(`select.sem_charger_${id}_ev_target_type`, 'soc', { options: ['kwh', 'soc'] });
    set(`number.sem_charger_${id}_target_soc`, 60, { min: 0, max: 100, step: 5 });
    set(`number.sem_charger_${id}_target_soc_max`, 90, { min: 0, max: 100, step: 5 });
    set(`number.sem_charger_${id}_minimum_current`, 6);
    set(`number.sem_charger_${id}_ev_battery_capacity_kwh`, 60);
    set(`number.sem_charger_${id}_ev_kwh_per_100km`, 15);
    set(`time.sem_charger_${id}_target_time`, '07:00:00');
    set(`select.sem_charger_${id}_phase_mode`, 'auto', { options: ['auto', '1', '3'] });
    // removed in #277 — still in some old installs' state machines
    set(`switch.sem_charger_${id}_night_charging`, 'off');
    set(`switch.sem_charger_${id}_tariff_optimized`, 'off');
    return s;
}

function hassFor(ids, { unreachable = false } = {}) {
    const states = {};
    for (const id of ids) Object.assign(states, chargerStates(id));
    const plan = [
        { kind: 'now', when: iso(0) },
        { kind: 'ev_charge_start', when: iso(3), until: iso(5) },
        { kind: 'ev_min_reached', when: iso(5) },
    ];
    states['sensor.sem_charging_state'] = { state: 'solar', attributes: {
        ev_deadline_reachable: !unreachable,
        per_charger_plans: Object.fromEntries(ids.map((id) => [id, plan])),
        per_charger_departure: Object.fromEntries(ids.map((id) => [id, {
            default: '07:00', by_weekday: { mon: '06:15' }, next: iso(10),
            one_block: false, late_min: 30, late: null, boost_allowed: true }])),
        battery_soc: 80,
        battery_boost_preview: { default_floor: 50, capacity_kwh: 10 },
    } };
    states['sensor.sem_battery_soc'] = { state: '80', attributes: {} };
    states['sensor.sem_battery_spendable_kwh'] = {
        state: '3', attributes: { phase: 'learning', nights_sealed: 2, nights_required: 7 } };
    states['select.sem_pause_duration'] = { state: '1_hour', attributes: { options: ['30_min', '1_hour'] } };
    states['sensor.sem_energy_ev_solar_percentage'] = { state: '72', attributes: {} };
    return {
        language: 'en', config: { time_zone: 'UTC', currency: 'CHF' }, states,
        callService: () => Promise.resolve(),
        callWS: () => Promise.resolve({ rows: [] }),
    };
}

function card(ids = ['keba'], opts = {}) {
    const el = new (customElements.get('sem-ev-status-card'))();
    el.setConfig({});
    el.connectedCallback();
    el.hass = hassFor(ids, opts);
    el.moreInfo = [];
    el.dispatchEvent = (ev) => { el.moreInfo.push(ev.detail?.entityId); return true; };
    return el;
}

const section = (el, id = 'keba', idx = 0) => read(el._renderChargerSection(id, idx));

// What each view must, and must not, show. A marker is a class or a
// translation key (no semLocalize here, so keys render as themselves).
const ALWAYS = ['charger-name', 'Charger keba', 'charger-soc', '>power<', '>today<', '>solar_share<',
    'charge_target', 'range-wrap', 'charge_mode', 'charge-by-row', 'ev_charge_by'];
const MORE = ['pause_charging_for', 'ct-pause-btn', 'phase_mode', 'dep-days', 'ev_dep_one_block',
    'ev_dep_late_charge', 'boost-row'];
const DETAILS = ['plan-strip', 'soc-info-708', 'charge_mode_battery_learning_info', 'sessions-block'];

beforeEach(() => { store.clear(); listeners.clear(); });

test('Everyday is the default, with no setting stored', () => {
    const el = card();
    assert.equal(el._allSettings, false);
});

test('Everyday shows only the rows used every day', () => {
    const { text } = section(card());
    for (const m of ALWAYS) assert.ok(text.includes(m), `Everyday lost ${m}`);
    for (const m of [...MORE, ...DETAILS]) assert.ok(!text.includes(m), `Everyday shows ${m}`);
    // the settings as one line, not tiles; the commanded amps and the
    // session tile are under Details
    assert.ok(text.includes('set-once'));
    assert.ok(!text.includes('charger-settings'));
    assert.ok(!text.includes('charger-set'));
    assert.ok(!text.includes('>session<'));
    // the next departure, because a weekday has its own time
    assert.ok(text.includes('dep-next'));
    assert.match(text, /fold-btn[^>]*aria-expanded="?false/);
});

test('Everyday keeps the warning when Charge by cannot be reached', () => {
    assert.ok(!section(card()).text.includes('ev_deadline_unreachable_short'));
    assert.ok(section(card(['keba'], { unreachable: true })).text.includes('ev_deadline_unreachable_short'));
});

test('All settings shows everything, as before', () => {
    localStorage.setItem(VIEW_MODE_KEY, '1');
    const { text } = section(card());
    for (const m of [...ALWAYS, ...MORE, ...DETAILS]) assert.ok(text.includes(m), `All settings lost ${m}`);
    assert.ok(text.includes('charger-settings'), 'the tiles');
    assert.ok(text.includes('charger-set'), 'the commanded amps in the header');
    assert.ok(text.includes('>session<'));
    assert.ok(!text.includes('set-once'));
    assert.ok(!text.match(/aria-expanded="?false/), 'a fold is closed in All settings');
});

test('Everyday has less than half the elements and controls', () => {
    const every = counts(section(card()).text);
    localStorage.setItem(VIEW_MODE_KEY, '1');
    const all = counts(section(card()).text);
    assert.ok(every.elements * 2 < all.elements, JSON.stringify({ every, all }));
    assert.ok(every.controls * 2 < all.controls, JSON.stringify({ every, all }));
});

test('a fold opens and closes, for its charger only', () => {
    const el = card(['keba', 'zoe']);
    section(el).click('class="fold-btn"');      // the first fold is More
    assert.equal(el._foldOpen('keba', 'more'), true);
    assert.equal(el._foldOpen('keba', 'details'), false);
    assert.equal(el._foldOpen('zoe', 'more'), false);
    const { text } = section(el);
    for (const m of MORE) assert.ok(text.includes(m), `More open, ${m} missing`);
    for (const m of DETAILS) assert.ok(!text.includes(m), `Details closed, ${m} shown`);
    el._toggleFold('keba', 'more');
    assert.ok(!section(el).text.includes('dep-days'));
    el._toggleFold('keba', 'details');
    for (const m of DETAILS) assert.ok(section(el).text.includes(m), `Details open, ${m} missing`);
});

test('the fold button says whether it is open', () => {
    const el = card();
    const fold = read(el._renderFold('keba', 'more', 'ev_fold_more', 'x')).text;
    assert.match(fold, /<button class="fold-btn"[^>]*aria-expanded=false/);
    assert.ok(fold.includes('ev_fold_more'));
    el._toggleFold('keba', 'more');
    assert.match(read(el._renderFold('keba', 'more', 'ev_fold_more', 'x')).text, /aria-expanded=true/);
});

test('switching the view resets every fold to its default', () => {
    const el = card(['keba', 'zoe']);
    el._toggleFold('keba', 'more');
    setAllSettings(true);
    assert.equal(el._allSettings, true);
    assert.deepEqual(el._folds, {});
    assert.equal(el._foldOpen('keba', 'more'), true);
    el._toggleFold('zoe', 'details');
    assert.equal(el._foldOpen('zoe', 'details'), false);
    setAllSettings(false);
    assert.equal(el._foldOpen('zoe', 'details'), false);
    assert.equal(el._foldOpen('keba', 'more'), false);
});

test("the card's own switch stores the choice and tells every card", () => {
    const el = card();
    const other = card();
    read(el.render()).click('view-seg ');           // Everyday: already on
    assert.equal(store.get(VIEW_MODE_KEY), '0');
    const r = read(el.render());
    const allBtn = r.handlers.filter((h) => h.tag.includes('view-seg'))[1];
    allBtn.fn({});
    assert.equal(store.get(VIEW_MODE_KEY), '1');
    assert.equal(el._allSettings, true);
    assert.equal(other._allSettings, true, 'the other card did not follow');
    assert.match(read(el.render()).text, /view-seg on" type="button"\s*aria-pressed=true/);
});

test('the card stops listening when it leaves the page, and reads again on return', () => {
    const el = card();
    assert.equal(listeners.get(VIEW_MODE_EVENT)?.size, 1);
    el.disconnectedCallback();
    assert.equal(listeners.get(VIEW_MODE_EVENT)?.size, 0);
    localStorage.setItem(VIEW_MODE_KEY, '1');     // switched on another tab
    el.connectedCallback();
    assert.equal(el._allSettings, true);
    assert.equal(listeners.get(VIEW_MODE_EVENT)?.size, 1);
    el.disconnectedCallback();
});

test('Set once opens the same more-info as each tile', () => {
    const el = card();
    const r = section(el);
    const vals = r.handlers.filter((h) => h.tag.includes('set-once-val'));
    assert.equal(vals.length, 3);
    vals.forEach((h) => h.fn({}));
    assert.deepEqual(el.moreInfo, [
        'number.sem_charger_keba_ev_battery_capacity_kwh',
        'number.sem_charger_keba_minimum_current',
        'number.sem_charger_keba_ev_kwh_per_100km',
    ]);
    assert.match(r.text, /set_once[\s\S]*60 kWh[\s\S]*6 A[\s\S]*15 kWh\/100 km/);
    localStorage.setItem(VIEW_MODE_KEY, '1');
    const tiles = card();
    section(tiles).handlers.filter((h) => h.tag.includes('setting-item')).forEach((h) => h.fn({}));
    assert.deepEqual([...tiles.moreInfo].sort(), [...el.moreInfo].sort());
});

test('the help toggle and its hints work in Everyday', () => {
    const el = card();
    el._showHelp = true;
    const { text } = section(el);
    assert.ok(text.includes('ev-help-toggle on'));
    assert.ok(text.includes('tile_help_capacity'));
    assert.ok(text.includes('hint_label_surplus'));
});

test('a running pause stays beside the mode in Everyday', () => {
    const el = card();
    el.hass.states['select.sem_charger_keba_charge_mode'] = { state: 'off', attributes: {
        options: ['off', 'solar_only'], paused_until: iso(1), pause_resume_mode: 'solar_only' } };
    const { text } = section(el);
    assert.ok(text.includes('pause_status_line'));
    assert.ok(!text.includes('ct-pause-btn'), 'the button that starts a pause is under More');
});

test('two chargers: each has its own folds, both closed in Everyday', () => {
    const el = card(['keba', 'zoe']);
    const { text } = read(el.render());
    assert.equal(text.split('class="charger-section"').length - 1, 2);
    assert.ok(text.includes('Charger keba') && text.includes('Charger zoe'));
    assert.equal((text.match(/aria-expanded=false/g) || []).length, 4);
    assert.ok(!text.includes('dep-days'));
    el._toggleFold('zoe', 'more');
    const after = read(el.render()).text;
    assert.equal((after.match(/class="dep-days"/g) || []).length, 1);
    assert.equal((after.match(/aria-expanded=true/g) || []).length, 1);
});

test('the switches removed in #277 are no longer watched', () => {
    const el = card();
    const key = el._lastKey;
    el.hass.states['switch.sem_charger_keba_night_charging'] = { state: 'on', attributes: {} };
    el.hass.states['switch.sem_charger_keba_tariff_optimized'] = { state: 'on', attributes: {} };
    el.hass = { ...el.hass };
    assert.equal(el._lastKey, key);
    const src = readFileSync(new URL('../src/cards/sem-ev-status-card.js', import.meta.url), 'utf8')
        .replace(/\/\/[^\n]*/g, '');
    assert.ok(!/_night_charging`/.test(src) && !/_tariff_optimized`/.test(src));
    // a watched one still is
    el.hass.states['time.sem_charger_keba_target_time'] = { state: '06:00:00', attributes: {} };
    el.hass = { ...el.hass };
    assert.notEqual(el._lastKey, key);
});

test('every new string is in every language, and not English elsewhere', () => {
    const tables = JSON.parse(readFileSync(new URL('../../translations.json', import.meta.url), 'utf8'));
    const keys = ['ev_view_label', 'ev_view_everyday', 'ev_view_all', 'ev_fold_more', 'ev_fold_details', 'ev_set_once'];
    const src = readFileSync(new URL('../src/cards/sem-ev-status-card.js', import.meta.url), 'utf8');
    for (const k of keys) assert.ok(src.includes(`'${k}'`), `${k} unused`);
    for (const [lang, table] of Object.entries(tables)) {
        for (const k of keys) {
            assert.ok(table[k], `${lang}.${k}`);
            assert.ok(!/beginner/i.test(table[k]));
        }
        if (lang !== 'en') assert.notEqual(table.ev_view_all, tables.en.ev_view_all, lang);
    }
});
