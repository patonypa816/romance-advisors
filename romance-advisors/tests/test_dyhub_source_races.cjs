const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
function harness() {
  const nodes = new Map(), events = new Map(), requests = [];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {value: id === '#source-url' ? 'http://127.0.0.1:8757' : '123',
      disabled: false, textContent: '', handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; }});
    return nodes.get(id);
  }
  const panel = {querySelector: node}, host = {prepend() {}}, timers = new Set();
  let poll, reloads = 0;
  vm.runInNewContext(readFileSync(join(__dirname, '../static/dyhub-source.js'), 'utf8'), {
    document: {querySelector: () => host, createElement: () => panel},
    window: {addEventListener: (name, fn) => events.set(name, fn), location: {reload: () => reloads++}},
    fetch(url, options = {}) { return new Promise(resolve => requests.push({url, options, resolve})); },
    setInterval(fn) { poll = fn; timers.add(1); return 1; }, clearInterval(id) { timers.delete(id); },
  });
  const reply = async (request, value, ok = true) => {
    request.resolve({ok, json: async () => value}); await flush();
  };
  return {nodes, requests, reply, events, timers, poll: () => poll(), reloads: () => reloads,
    start: () => node('#source-form').handlers.submit({preventDefault() {}}),
    stop: () => node('#source-stop').handlers.click()};
}
const stopped = {active: false, state: 'stopped', counts: {}, episode_limit: 5000};
const active = {...stopped, active: true, state: 'connected', base_url: 'http://127.0.0.1:8757', room_id: '123'};

test('source UI never starts collecting on load and repeated start clicks send only once', async () => {
  const h = harness();
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, '/api/source');
  await h.reply(h.requests[0], stopped);
  h.start(); h.start();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].url, '/api/source/start');
  assert.equal(h.requests[1].options.method, 'POST');
  assert.deepEqual(JSON.parse(h.requests[1].options.body), {base_url: 'http://127.0.0.1:8757', room_id: '123'});
  await h.reply(h.requests[1], active);
  assert.equal(h.nodes.get('#source-start').disabled, true);
  assert.equal(h.nodes.get('#source-stop').disabled, false);
});

test('a delayed pre-stop poll cannot overwrite the completed stop result', async () => {
  const h = harness(); await h.reply(h.requests[0], active);
  h.poll(); const oldPoll = h.requests[1];
  h.stop(); const stop = h.requests[2];
  await h.reply(stop, stopped); await h.reply(oldPoll, active);
  assert.equal(h.nodes.get('#source-status').textContent, '未连接');
  assert.equal(h.nodes.get('#source-stop').disabled, true);
  assert.equal(h.nodes.get('#source-start').disabled, false);
});

test('a poll whose JSON body arrives during a new action is ignored', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  h.poll(); let finishBody;
  h.requests[1].resolve({ok: true, json: () => new Promise(resolve => { finishBody = resolve; })});
  await flush(); h.start(); finishBody(stopped); await flush();
  assert.equal(h.nodes.get('#source-start').disabled, true);
  await h.reply(h.requests[2], active);
  assert.equal(h.nodes.get('#source-status').textContent, '已连接');
});

test('leaving clears polling and history restoration reconciles state without starting a source', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  h.events.get('pagehide')(); assert.equal(h.timers.size, 0);
  h.events.get('pageshow')({persisted: true}); assert.equal(h.reloads(), 1);
  assert.equal(h.requests.length, 1);
});
