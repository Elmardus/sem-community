/**
 * (#1224) One "Use this" click on half of a split grid pair saves the whole
 * pair, in one write. Saving only the clicked half read the other half as
 * 0 W: a home that sends power to the grid looked like it never does.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { proposalWrite, writeIsSaved } from '../src/util/detect-rows.js';

const IMP = 'grid_import_power_entity';
const EXP = 'grid_export_power_entity';
const pair = { [IMP]: ['sensor.imp', 'sensor.imp2'], [EXP]: ['sensor.exp'] };
const impRow = { config_key: IMP, entity: 'sensor.imp', action: 'set_option', pair };
const expRow = { config_key: EXP, entity: 'sensor.exp', action: 'set_option', pair };

test('a click on either half saves both', () => {
    const both = { [IMP]: 'sensor.imp', [EXP]: 'sensor.exp' };
    assert.deepEqual(proposalWrite(impRow, 'sensor.imp', {}), both);
    assert.deepEqual(proposalWrite(expRow, 'sensor.exp', {}), both);
});

test('an alternative pairs with the other half', () => {
    assert.deepEqual(proposalWrite(impRow, 'sensor.imp2', {}),
        { [IMP]: 'sensor.imp2', [EXP]: 'sensor.exp' });
});

test("the other half keeps the user's earlier choice from this device", () => {
    const saved = { [IMP]: 'sensor.imp2' };
    assert.deepEqual(proposalWrite(expRow, 'sensor.exp', saved),
        { [IMP]: 'sensor.imp2', [EXP]: 'sensor.exp' });
});

test("a saved half from another device is replaced, never mixed in", () => {
    const saved = { [IMP]: 'sensor.other_meter' };
    assert.deepEqual(proposalWrite(expRow, 'sensor.exp', saved),
        { [IMP]: 'sensor.imp', [EXP]: 'sensor.exp' });
});

test('one entity is never both halves', () => {
    const odd = { [IMP]: ['sensor.x'], [EXP]: ['sensor.x'] };
    assert.equal(proposalWrite({ config_key: IMP, pair: odd }, 'sensor.x', {}), null);
});

test("a pair without the row's own key saves nothing", () => {
    assert.equal(proposalWrite({ config_key: 'battery_soc_sensor', pair }, 'sensor.soc', {}), null);
});

test('a role outside a pair saves its own key only', () => {
    assert.deepEqual(proposalWrite({ config_key: 'battery_soc_sensor' }, 'sensor.soc', {}),
        { battery_soc_sensor: 'sensor.soc' });
    assert.equal(proposalWrite({ config_key: null }, 'sensor.soc', {}), null);
    assert.equal(proposalWrite({ config_key: 'k' }, undefined, {}), null);
});

test('a half saved alone is not "in use": the pair is not whole', () => {
    const write = proposalWrite(expRow, 'sensor.exp', { [IMP]: 'sensor.imp' });
    assert.equal(writeIsSaved(write, { [IMP]: 'sensor.imp' }), false);
    assert.equal(writeIsSaved(write, { [IMP]: 'sensor.imp', [EXP]: 'sensor.exp' }), true);
});

test('nothing to write is never "in use"', () => {
    // a service-shaped proposal (no entity, no option key) used to read as
    // "in use": undefined === undefined
    assert.equal(writeIsSaved(null, {}), false);
    assert.equal(writeIsSaved(proposalWrite({ config_key: null }, undefined, {}), {}), false);
});

test('the card saves what proposalWrite says, in one call', () => {
    const src = readFileSync(new URL('../src/cards/sem-config-card.js', import.meta.url), 'utf8');
    const rows = src.slice(src.indexOf('const useButton'), src.indexOf('const prober'));
    assert.match(rows, /this\._saveOptions\(write, fieldKey\)/);
    assert.doesNotMatch(rows, /_saveOption\(p\.config_key/,
        'a button that writes one key saves half a pair');
    assert.equal((rows.match(/proposalWrite\(p, /g) || []).length, 2,
        'the row and each alternative ask proposalWrite');
});
