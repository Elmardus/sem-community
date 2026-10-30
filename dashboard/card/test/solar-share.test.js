/** (#1046) A solar share is the split of the flows, never flow ÷ meter. */
import { test } from 'node:test';
import assert from 'node:assert';
import { solarSharePct } from '../src/util/solar-share.js';

test('the split of the flows', () => {
    assert.equal(solarSharePct(3, 1), 75);
    assert.equal(solarSharePct(2, 0), 100);
    assert.equal(solarSharePct(0, 5), 0);
});

test('a meter that counted more does not move it', () => {
    // 3 kWh solar + 1 kWh grid in the flows; the meter said 8 kWh charged
    const dailyCharge = 8;
    assert.equal(Math.round(3 / dailyCharge * 100), 38, 'the old number');
    assert.equal(solarSharePct(3, 1), 75);
});

test('no flows: no split known', () => {
    assert.equal(solarSharePct(0, 0), null);
    assert.equal(solarSharePct(0), null);
    assert.equal(solarSharePct(undefined, null), null);
});

test('junk and negative values do not count', () => {
    assert.equal(solarSharePct(2, -1), 100);
    assert.equal(solarSharePct('2', 'unavailable'), 100);
    assert.equal(solarSharePct(-1, 2), 0);
});
