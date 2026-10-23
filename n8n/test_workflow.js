const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const workflowPath = path.join(__dirname, '..', 'My workflow.json');
const workflow = JSON.parse(fs.readFileSync(workflowPath, 'utf8'));
const strategyNode = workflow.nodes.find((node) => node.name === 'Strategy Engine');

assert.ok(strategyNode, 'Strategy Engine node must exist');
const evaluateStrategy = new Function('$json', strategyNode.parameters.jsCode);

test('workflow stays inactive and PAPER-only', () => {
  assert.equal(workflow.active, false);
  assert.match(strategyNode.parameters.jsCode, /mode:\s*"PAPER"/);
});

test('missing and invalid change data safely produces HOLD', () => {
  for (const input of [
    { price: 10 },
    { price: 10, change_percent: null },
    { price: 10, change_percent: '' },
    { price: 10, change_percent: 'not-a-number' },
  ]) {
    assert.equal(evaluateStrategy(input).json.signal, 'HOLD');
  }
});

test('sub-threshold changes produce HOLD', () => {
  assert.equal(evaluateStrategy({ price: 10, change_percent: '0%' }).json.signal, 'HOLD');
  assert.equal(evaluateStrategy({ price: 10, change_percent: '0.99%' }).json.signal, 'HOLD');
  assert.equal(evaluateStrategy({ price: 10, change_percent: '-0.99%' }).json.signal, 'HOLD');
});

test('signals are only emitted at configured boundaries', () => {
  assert.equal(evaluateStrategy({ price: 10, change_percent: '1%' }).json.signal, 'BUY_CANDIDATE');
  assert.equal(evaluateStrategy({ price: 10, change_percent: '-1%' }).json.signal, 'SELL_CANDIDATE');
});

test('final fields are restricted to the risk-approved PAPER result', () => {
  const finalNode = workflow.nodes.find((node) => node.name === 'Edit Fields1');
  assert.ok(finalNode, 'Final result node must exist');
  assert.equal(finalNode.parameters.includeOtherFields, false);

  const assignments = finalNode.parameters.assignments.assignments;
  assert.deepEqual(assignments.map((field) => field.name).sort(), [
    'mode', 'position_value', 'price', 'quantity', 'risk_allowed', 'signal', 'symbol', 'trading_capital',
  ]);
  assert.equal(assignments.find((field) => field.name === 'mode').value, '=PAPER');
  assert.equal(assignments.find((field) => field.name === 'risk_allowed').value, '={{ $json.allowed }}');
  assert.ok(workflow.connections.If2.main[0].some((connection) => connection.node === 'Edit Fields1'));
});

test('non-buy strategy results terminate as an explicit PAPER HOLD', () => {
  const holdNode = workflow.nodes.find((node) => node.name === 'No Buy Result');
  assert.ok(holdNode, 'No Buy Result node must exist');
  assert.ok(workflow.connections.If1.main[1].some((connection) => connection.node === holdNode.name));
  const fields = Object.fromEntries(holdNode.parameters.assignments.assignments.map((field) => [field.name, field]));
  assert.equal(fields.signal.value, '=HOLD');
  assert.equal(fields.mode.value, '=PAPER');
  assert.equal(fields.quantity.value, '0');
  assert.equal(fields.position_value.value, '0');
  assert.equal(fields.risk_allowed.value, 'false');
  assert.equal(fields.strategy_signal.value, "={{ $('Strategy Engine').item.json.signal }}");
  assert.equal(fields.reason.value, '=NO_BUY_SIGNAL');
});

test('risk-rejected position ends as HOLD without bypassing the risk gate', () => {
  const rejectedNode = workflow.nodes.find((node) => node.name === 'Risk Rejected Result');
  assert.ok(rejectedNode, 'Risk Rejected Result node must exist');
  assert.ok(workflow.connections.If2.main[1].some((connection) => connection.node === rejectedNode.name));
  const fields = Object.fromEntries(rejectedNode.parameters.assignments.assignments.map((field) => [field.name, field]));
  assert.equal(fields.signal.value, '=HOLD');
  assert.equal(fields.mode.value, '=PAPER');
  assert.equal(fields.risk_allowed.value, 'false');
  assert.equal(fields.reason.value, '=POSITION_REJECTED');
  assert.equal(rejectedNode.parameters.includeOtherFields, false);
});
