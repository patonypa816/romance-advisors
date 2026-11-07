const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/dyhub-source.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const state = active => ({active, state: active ? 'connected' : 'stopped',
  base_url: 'http://127.0.0.1:8757', room_id: '123', counts: {}, episode_limit: 5000});
const response = value => ({ok: true, json: async () => value});
function setup(fetch) {
  const fields = {}, listeners = {}, intervals = [], cleared = [];
  const field = id => fields[id] ||= {value: id === 'source-url' ? 'http://127.0.0.1:8757' : '123',
    disabled: false, textContent: '', handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; }};
  const panel = {innerHTML: '', querySelector: selector => field(selector.slice(1))};
  const host = {prepend() {}};
  const window = {addEventListener(name, fn) { listeners[name] = fn; }, location: {reload() { window.reloaded = true; }}};
  vm.runInNewContext(source, {document: {querySelector: () => host, createElement: () => panel}, fetch, window,
    setInterval: fn => { intervals.push(fn); return 1; }, clearInterval: id => cleared.push(id)});
  return {fields, field, listeners, intervals, cleared, window,
    submit: () => field('source-form').handlers.submit({preventDefault() {}}),
    stop: () => field('source-stop').handlers.click()};
}

test('opening a tab reads the server watcher without starting another one', async () => {
  const calls = [];
  const ui = setup(async (...args) => { calls.push(args); return response(state(false)); });
  await flush();
  assert.deepEqual(calls.map(args => args[0]), ['/api/source']);
  assert.equal(ui.field('source-start').disabled, false);
  assert.equal(ui.field('source-stop').disabled, true);
});

test('repeated connect submits once and late initial read cannot overwrite it', async () => {
  let resolveRead, resolveWrite;
  const calls = [];
  const ui = setup((url, opts) => {
    calls.push(url);
    return new Promise(resolve => { if (opts?.method === 'POST') resolveWrite = resolve; else resolveRead = resolve; });
  });
  ui.submit(); ui.submit();
  assert.equal(calls.filter(url => url.endsWith('/auto')).length, 1);
  resolveWrite(response(state(true)));
  await flush();
  resolveRead(response(state(false)));
  await flush();
  assert.equal(ui.field('source-start').disabled, true);
  assert.equal(ui.field('source-stop').disabled, false);
  assert.equal(ui.field('source-room').disabled, true);
});

test('uncertain stop does not retry the mutation and a later read reconciles', async () => {
  let active = true;
  const calls = [];
  const ui = setup(async (url, opts) => {
    calls.push(url);
    if (opts?.method === 'POST') { active = false; throw new Error('lost response'); }
    return response(state(active));
  });
  await flush();
  ui.stop();
  await flush();
  assert.match(ui.field('source-note').textContent, /刷新状态确认/);
  await ui.intervals[0]();
  assert.equal(calls.filter(url => url.endsWith('/stop')).length, 1);
  assert.equal(ui.field('source-start').disabled, false);
  assert.equal(ui.field('source-stop').disabled, true);
});

test('pagehide ends polling and BFCache restoration reloads status only', async () => {
  const calls = [];
  const ui = setup(async url => { calls.push(url); return response(state(false)); });
  await flush();
  ui.listeners.pagehide();
  ui.listeners.pageshow({persisted: true});
  assert.deepEqual(ui.cleared, [1]);
  assert.equal(ui.window.reloaded, true);
  assert.deepEqual(calls, ['/api/source']);
});
