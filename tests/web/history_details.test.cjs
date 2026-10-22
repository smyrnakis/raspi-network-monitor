const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.text = ''; }
  append(...children) { this.children.push(...children); }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() {
    return this.text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join('');
  }
}
const source = fs.readFileSync(path.join(__dirname,
  '../../src/home_internet_monitor/web/static/history.js'), 'utf8');
const context = vm.createContext({
  document: { createElement: tag => new Element(tag) },
  state: { timezone: 'UTC' },
});
vm.runInContext(source.slice(source.indexOf('const CATEGORY_GROUPS'), source.indexOf('const urlParameters')) +
  source.slice(source.indexOf('function historyEvent('), source.indexOf('installDefaultTime(elements.startGroup')), context);
const incident = {
  event_type: 'incident', event_id: 'internal-id', category: 'internet_down', state: 'closed',
  start: '2026-10-22T18:42:10Z', end: '2026-10-22T18:46:30Z',
  confirmed_start: '2026-10-22T18:42:30Z', confirmed_end: '2026-10-22T18:46:50Z',
  end_reason: 'recovered', duration_seconds: 260, failed_tests: [],
};
function render(overrides = {}) { return context.historyEvent({ ...incident, ...overrides }, false); }

test('resolved incident has category, lifecycle and exactly two timing rows', () => {
  const card = render();
  assert.match(card.children[0].textContent, /Internet downResolved/);
  const rows = card.children[1].children[0].children;
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children[0].textContent, 'Started');
  assert.equal(rows[1].children[0].textContent, 'Finished');
  assert.match(rows[0].textContent, /\(verified /);
  assert.match(rows[1].textContent, /\(verified /);
  assert.doesNotMatch(card.textContent, /Event ID|internal-id|End reason|Event type|Confirmed start/);
});

test('monitoring interruption never claims verified recovery', () => {
  const card = render({ state: 'interrupted', end_reason: 'monitoring_unknown', confirmed_end: null });
  assert.match(card.textContent, /Recovery unverified/);
  assert.match(card.textContent, /\(recovery unverified\)/);
  assert.match(card.textContent, /Monitoring ended before recovery could be verified/);
  assert.doesNotMatch(card.textContent, /Resolved/);
});

test('ongoing incident and monitoring gap retain clear lifecycle labels', () => {
  const ongoing = render({ state: 'open', end: null, confirmed_end: null, end_reason: null });
  assert.match(ongoing.children[0].textContent, /Ongoing/);
  assert.equal(ongoing.children[1].children[0].children[1].children[1].textContent, 'Ongoing');
  const gap = render({ event_type: 'gap', category: 'process_restart', end_reason: null });
  assert.match(gap.textContent, /Monitoring gap: Process restartClosed/);
  assert.doesNotMatch(gap.textContent, /verified|Recovery/);
});

test('verification crossing midnight includes its full date', () => {
  const observed = '2026-10-22T23:59:50Z';
  const confirmed = '2026-10-23T00:00:10Z';
  assert.equal(context.verificationTime(observed, confirmed), context.formatDate(confirmed, true));
});
