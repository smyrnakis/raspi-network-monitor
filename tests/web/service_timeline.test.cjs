const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname,
  '../../src/home_internet_monitor/web/static/app.js'), 'utf8');
function functionSource(name, next) {
  return source.slice(source.indexOf(`function ${name}(`), source.indexOf(`function ${next}(`));
}
const context = vm.createContext({
  document: { createElement: () => ({ dataset: {}, style: {}, setAttribute() {} }) },
  formatDate: String,
  formatDuration: String,
});
vm.runInContext(functionSource('renderServiceMiniTimeline', 'startOfTimeline') +
  functionSource('impairmentColor', 'showTimelineTooltip'), context);
const hour = 3600000;
const end = 7 * 24 * hour;
function segment(status, start, finish) {
  return { status, start: new Date(start).toISOString(), end: new Date(finish).toISOString() };
}
function render(segments, shading = true) {
  const children = [];
  context.renderServiceMiniTimeline({ appendChild: node => children.push(node) }, segments, 0, end, shading);
  return children;
}

test('two-minute outage is visible in a six-hour mostly healthy block', () => {
  const segments = [segment('down', 0, 120000), segment('up', 120000, end)];
  const shaded = render(segments);
  assert.equal(shaded.length, 28);
  assert.match(shaded[0].style.background, /^hsl/);
  assert.equal(shaded[1].style.background, 'var(--online)');
  const dominant = render(segments, false);
  assert.equal(dominant[0].dataset.status, 'up');
  assert.equal(dominant[0].style.background, undefined);
});

test('degraded time counts and longer interruptions produce darker red', () => {
  const blocks = render([segment('degraded', 0, hour), segment('up', hour, 6 * hour),
    segment('down', 6 * hour, 10 * hour), segment('up', 10 * hour, end)]);
  const lightness = node => Number(node.style.background.match(/([\d.]+)%\)$/)[1]);
  assert.ok(lightness(blocks[0]) > lightness(blocks[1]));
});

test('missing and explicit unknown time remain visible', () => {
  assert.equal(render([])[0].dataset.status, 'unknown');
  const blocks = render([segment('unknown', 0, 3 * hour), segment('up', 3 * hour, 9 * hour)]);
  assert.match(blocks[0].style.backgroundImage, /gradient/);
  assert.match(blocks[1].style.backgroundImage, /gradient/);
  assert.equal(blocks[2].dataset.status, 'unknown');
});

test('an outage crossing a block boundary appears in both blocks', () => {
  const blocks = render([segment('up', 0, 6 * hour - 60000),
    segment('down', 6 * hour - 60000, 6 * hour + 60000),
    segment('up', 6 * hour + 60000, end)]);
  assert.match(blocks[0].style.background, /^hsl/);
  assert.match(blocks[1].style.background, /^hsl/);
});
