import assert from 'node:assert/strict';
import test from 'node:test';
import {parseLiveIdentity} from '../src/collector/roomMeta.js';
import {EventBus} from '../src/pipeline/eventBus.js';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const page = (room: any) => JSON.stringify({roomStore: {roomInfo: {room}}});
const live = {id_str: '900001', status: 2, web_rid: '123'};

test('broadcast identity is the strict target roomStore platform room, even with reordered fields', () => {
  for (const html of [page(live), page({...live, title: '直播标题'}).replaceAll('"', '&quot;'),
    page({title: '演示', status: 2, id_str: '900001', web_rid: '123'}), page(live).replaceAll('"', '\\"')]) {
    assert.deepEqual(parseLiveIdentity(html, '123'), {
      status: 'live', liveId: '900001', evidence: 'roomStore.roomInfo.room'});
  }
  assert.equal(parseLiveIdentity(page({...live, status: 4}), '123').status, 'ended');
});

test('recommendations, unknown status, web_rid aliases, mismatched rooms and conflicting identities are unknown', () => {
  for (const html of [JSON.stringify({room: live}), '<html>验证或登录页面</html>',
    page({...live, status: 3}), page({...live, id_str: '123'}), page({...live, web_rid: '456'}),
    page({...live, id_str: 900001}), page(live) + page({...live, id_str: '900002'}),
    page(live).slice(0, -2)]) {
    assert.equal(parseLiveIdentity(html, '123').status, 'unknown', html);
  }
});

test('health identity is local, free and does not connect a room', async () => {
  // cookieStore loads at module import. A blank temporary cwd keeps this API
  // regression wholly isolated from the operator's real runtime credentials.
  const directory = mkdtempSync(join(tmpdir(), 'dyhub-identity-test-'));
  const previous = process.cwd();
  delete process.env.DYHUB_COOKIE;
  let buildApi;
  try {
    process.chdir(directory);
    ({buildApi} = await import('../src/api/server.js'));
  } finally { process.chdir(previous); }
  let connects = 0;
  const app = buildApi({collector: {getRooms: () => [], connect: () => {connects++;}} as any,
    bus: new EventBus(), wsClientCount: () => 0,
    webhookDispatcher: {stats: () => ({})} as any, startedAt: Date.now()});
  try {
    const response = await app.inject({method: 'GET', url: '/api/health'});
    assert.equal(response.statusCode, 200);
    assert.deepEqual(response.json(), {app: 'dyhub', pid: process.pid, features: {live_status: true}});
    assert.equal(connects, 0);
    assert.equal((await app.inject({method: 'GET', url: '/api/rooms/abc/live-status'})).statusCode, 400);
    assert.equal((await app.inject({method: 'GET', url: '/api/rooms/123/live-status', remoteAddress: '192.0.2.1'})).statusCode, 403);
  } finally { await app.close(); rmSync(directory, {recursive: true, force: true}); }
});
