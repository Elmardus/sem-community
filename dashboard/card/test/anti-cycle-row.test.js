/**
 * (#914) The anti-cycle row must not invent numbers.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { MISSING, antiCyclePlaceholder, antiCycleBounds, antiCycleValue } from '../src/util/load-sections.js';

test('placeholder shows what the live device holds', () => {
    assert.equal(antiCyclePlaceholder({ min_on_effective_min: 10 }, 'min_on_effective_min'), '10');
    assert.equal(antiCyclePlaceholder({ min_off_effective_min: 5.0 }, 'min_off_effective_min'), '5');
});

test('no live device is a dash, never "5" and never "0"', () => {
    assert.equal(antiCyclePlaceholder({ min_on_effective_min: null }, 'min_on_effective_min'), MISSING);
    assert.equal(antiCyclePlaceholder({}, 'min_on_effective_min'), MISSING);
    assert.equal(antiCyclePlaceholder(undefined, 'min_on_effective_min'), MISSING);
});

test('a non-numeric effective value is absent, not NaN', () => {
    assert.equal(antiCyclePlaceholder({ min_on_effective_min: 'n/a' }, 'min_on_effective_min'), MISSING);
});

test('bounds come from the published attribute', () => {
    assert.deepEqual(antiCycleBounds({ anti_cycle_bounds: { min: 0, max: 120, step: 1 } }), { min: 0, max: 120 });
});

test('an absent attribute is null — no range the card made up', () => {
    assert.equal(antiCycleBounds({}), null);
    assert.equal(antiCycleBounds(undefined), null);
    assert.equal(antiCycleBounds({ anti_cycle_bounds: { min: 'x', max: 120 } }), null);
});

// (#1128) min/max on an input limit its arrows, not typing.
const RANGE = { min: 0, max: 120 };

test('a typed value past the range is the end of it', () => {
    assert.equal(antiCycleValue('500', RANGE), 120);
    assert.equal(antiCycleValue('121', RANGE), 120);
    assert.equal(antiCycleValue('5', { min: 10, max: 120 }), 10);
});

test('a value in the range is sent as typed', () => {
    assert.equal(antiCycleValue('1', RANGE), 1);
    assert.equal(antiCycleValue('15', RANGE), 15);
    assert.equal(antiCycleValue('120', RANGE), 120);
});

test('empty, not a number, or under 1 sends nothing', () => {
    for (const raw of ['', null, undefined, '0', '0.5', '-3', 'abc']) {
        assert.equal(antiCycleValue(raw, RANGE), null, String(raw));
    }
});

test('no published range: the value as typed, never a range made up here', () => {
    assert.equal(antiCycleValue('500', null), 500);
    assert.equal(antiCycleValue('0', null), null);
});

test('a range with a missing end is no range', () => {
    assert.equal(antiCycleBounds({ anti_cycle_bounds: { min: 0, max: null } }), null);
    assert.equal(antiCycleBounds({ anti_cycle_bounds: { min: 0 } }), null);
});

test('the limit never makes the "off" value', () => {
    assert.equal(antiCycleValue('15', { min: 0, max: 0 }), null);
});
