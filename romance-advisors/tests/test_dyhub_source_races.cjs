const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
function harness(search = "") {
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
    window: {addEventListener: (name, fn) => events.set(name, fn), location: {search, reload: () => reloads++}},
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
  assert.equal(h.requests[1].url, '/api/source/auto');
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
  assert.equal(h.nodes.get('#source-status').textContent, '接收通道已连接');
});

test('leaving clears polling and history restoration reconciles state without starting a source', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  h.events.get('pagehide')(); assert.equal(h.timers.size, 0);
  h.events.get('pageshow')({persisted: true}); assert.equal(h.reloads(), 1);
  assert.equal(h.requests.length, 1);
});


test('operation errors survive status polls and explicit retry clears them', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  h.start(); await h.reply(h.requests[1], {error: '请先开始一期，再连接本机评论来源。'}, false);
  assert.match(h.nodes.get('#source-note').textContent, /请先开始一期/);
  assert.equal(h.nodes.get('#source-start').disabled, false);
  h.poll(); await h.reply(h.requests[2], stopped);
  assert.match(h.nodes.get('#source-note').textContent, /请先开始一期/);
  h.start(); await h.reply(h.requests[3], active);
  assert.doesNotMatch(h.nodes.get('#source-note').textContent, /请先开始一期/);
});

test('connected channel and received real comments are reported separately', async () => {
  const h = harness(); await h.reply(h.requests[0], active);
  assert.equal(h.nodes.get('#source-status').textContent, '接收通道已连接');
  assert.match(h.nodes.get('#source-note').textContent, /尚未收到真实评论/);
  h.poll(); await h.reply(h.requests[1], {...active, counts: {accepted: 2, ignored: 4}});
  assert.match(h.nodes.get('#source-note').textContent, /已收到 2 条真实评论/);
  h.poll(); await h.reply(h.requests[2], {...active, state: 'error', error_code: 'room_connect_failed'});
  assert.match(h.nodes.get('#source-note').textContent, /确认抖音已开播/);
  assert.equal(h.nodes.get('#source-stop').disabled, false);
});

test('live room links normalize to numeric room IDs without any external request', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  h.nodes.get('#source-room').value = ' https://live.douyin.com/123456789/?from=web ';
  h.start();
  assert.equal(h.requests[1].url, '/api/source/auto');
  assert.equal(JSON.parse(h.requests[1].options.body).room_id, '123456789');
  await h.reply(h.requests[1], stopped);
  h.nodes.get('#source-room').value = 'live.douyin.com/987654';
  h.start();
  assert.equal(JSON.parse(h.requests[2].options.body).room_id, '987654');
});

test('profile links and malformed room IDs stay local and leave a readable correction', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  for (const value of ['https://www.douyin.com/user/123', 'abc123', 'https://live.douyin.com.evil/123', '1'.repeat(33)]) {
    h.nodes.get('#source-room').value = value;
    h.start();
    assert.equal(h.requests.length, 1);
    assert.match(h.nodes.get('#source-note').textContent, /纯数字房间号/);
    assert.equal(h.nodes.get('#source-start').disabled, false);
  }
});

test('a failed status read keeps operation errors visible and recovers on the next poll', async () => {
  const h = harness(); await h.reply(h.requests[0], stopped);
  h.start(); await h.reply(h.requests[1], {error: '请先开始一期。'}, false);
  h.poll(); await h.reply(h.requests[2], {}, false);
  assert.match(h.nodes.get('#source-note').textContent, /请先开始一期/);
  assert.match(h.nodes.get('#source-note').textContent, /暂时无法读取/);
  h.poll(); await h.reply(h.requests[3], stopped);
  assert.match(h.nodes.get('#source-note').textContent, /请先开始一期/);
  assert.doesNotMatch(h.nodes.get('#source-note').textContent, /暂时无法读取/);
});


test('broadcast preview renders only bounded received text while the debug source keeps legacy scope', async () => {
  const h = harness();
  const comments = Array.from({length: 45}, (_, i) => ({nickname: '<观众>', text: `<img src=x onerror=alert(1)>${i}`}));
  await h.reply(h.requests[0], {...active, counts: {accepted: 0, received: 45}, recent_comments: comments});
  assert.match(h.nodes.get('#source-note').textContent, /已收到 45 条真实评论/);
  assert.equal(h.nodes.get('#source-recent-wrap').hidden, false);
  const preview = h.nodes.get('#source-recent');
  assert.equal(preview.textContent.split('\n').length, 40);
  assert.match(preview.textContent, /<img src=x onerror=alert\(1\)>44/);
  assert.equal(preview.innerHTML, undefined);
  const debug = harness('?debug=1');
  await debug.reply(debug.requests[0], stopped);
  debug.start();
  assert.equal(JSON.parse(debug.requests[1].options.body).scope, undefined);
});

test('automatic room watcher is visible and can be paused before a source opens', async () => {
  const h = harness();
  const auto = {enabled:true,room_id:'321',base_url:'http://127.0.0.1:8757',state:'waiting',paused:false};
  await h.reply(h.requests[0], {...stopped,auto});
  assert.equal(h.requests.length,1);
  assert.equal(h.nodes.get('#source-status').textContent,'等候抖音开播');
  assert.equal(h.nodes.get('#source-room').value,'321');
  assert.equal(h.nodes.get('#source-stop').disabled,false);
  h.stop();assert.equal(h.requests[1].url,'/api/source/stop');
  await h.reply(h.requests[1], {...stopped,auto:{...auto,paused:true,state:'paused'}});
  assert.equal(h.nodes.get('#source-start').textContent,'恢复自动接收');
  h.start();assert.equal(h.requests[2].url,'/api/source/auto');
});
