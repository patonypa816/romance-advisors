import assert from 'node:assert/strict';
import test from 'node:test';
import {fetchLiveIdentity, parseLiveIdentity} from '../src/collector/roomMeta.js';

const room = {
  id_str: '900001', web_rid: '123', status: 2,
  title: '中文「引号」和 "English quotes"，路径 C:\\live\\new\\title',
  stream_data: JSON.stringify({data: {origin: {main: {flv: 'https://example.invalid/live?a="quoted"&b=\\x',
    sdk_params: JSON.stringify({codec: 'h264', note: '\\folder\\name "SDK" 中文'})}}}}),
};
const state = (value = room) => ({state: {roomStore: {roomInfo: {room: value}}}});
const push = (payload: string) => `<script nonce="synthetic">self.__pace_f.push(${JSON.stringify([1, payload])});</script>`;
const expected = {status: 'live', liveId: '900001', evidence: 'roomStore.roomInfo.room'};

test('pace JSON string layers preserve embedded stream/SDK JSON, literal slashes and quoted Chinese titles', () => {
  const encoded = JSON.stringify(state());
  for (const page of [push(encoded), push(JSON.stringify(encoded)),
    `<html><head></head><body>${push(encoded)}</body></html>`,
    push(`0:${encoded}\n`)]) {
    assert.deepEqual(parseLiveIdentity(page, '123'), expected);
  }
});

test('plain JSON, HTML entity and URI script envelopes keep nested strings intact', () => {
  const encoded = JSON.stringify(state());
  const entity = encoded.replaceAll('&', '&amp;').replaceAll('"', '&quot;');
  for (const page of [encoded, entity, `<script type="application/json">${entity}</script>`,
    `<script id="RENDER_DATA" type="application/json">${encodeURIComponent(encoded)}</script>`]) {
    assert.deepEqual(parseLiveIdentity(page, '123'), expected);
  }
});

test('split pace chunks are reconstructed before JSON parsing, even across backslash boundaries', () => {
  const encoded = JSON.stringify(state());
  const split = encoded.indexOf('\\"') + 1;
  assert.ok(split > 0);
  assert.deepEqual(parseLiveIdentity(push(encoded.slice(0, split)) + push(encoded.slice(split)), '123'), expected);
});

test('recommendations, room-shaped string fields and JavaScript expressions never establish identity', () => {
  const dangerous = [
    push(JSON.stringify({recommendations: [state()]})),
    push(JSON.stringify({state: {recommendations: [{roomStore: {roomInfo: {room}}}]}})),
    push(JSON.stringify({state: {title: JSON.stringify(state())}})),
    push(JSON.stringify({roomInfo: {room}})),
    `<script>self.__pace_f.push([1, (globalThis.SECRET = 'bad', ${JSON.stringify(JSON.stringify(state()))})]);</script>`,
    push(JSON.stringify(state()).slice(0, -3)),
  ];
  for (const page of dangerous) assert.equal(parseLiveIdentity(page, '123').status, 'unknown');
  assert.equal((globalThis as any).SECRET, undefined);
});

test('wrong target or conflicting roomStores remain unknown beside a valid room', () => {
  for (const change of [{web_rid: '456'}, {id_str: '900002'}, {status: 4}, {status: 3}]) {
    const html = push(JSON.stringify(state())) + push(JSON.stringify(state({...room, ...change})));
    assert.equal(parseLiveIdentity(html, '123').status, 'unknown', JSON.stringify(change));
  }
  assert.deepEqual(parseLiveIdentity(push(JSON.stringify(state())) + push(JSON.stringify(state())), '123'), expected);
});

test('oversized inputs and excessive JSON envelope nesting are bounded and unknown', () => {
  assert.equal(parseLiveIdentity('x'.repeat(8_000_001), '123').status, 'unknown');
  let nested = JSON.stringify(state());
  for (let i = 0; i < 12; i++) nested = JSON.stringify(nested);
  assert.equal(parseLiveIdentity(push(nested), '123').status, 'unknown');
});

test('fetch uses only mocked visitor responses and rejects oversized streaming HTML before buffering it', async t => {
  let canceled = false;
  let target: Response;
  t.mock.method(globalThis, 'fetch', async (input: string) => {
    if (input === 'https://live.douyin.com/123') return target;
    return new Response('', {headers: {'set-cookie': 'ttwid=SYNTHETIC_VISITOR; Path=/'}});
  });
  const encoded = new TextEncoder().encode(push(JSON.stringify(state())));
  target = new Response(new ReadableStream({start(controller) {
    const split = encoded.findIndex(value => value > 127) + 1;
    controller.enqueue(encoded.slice(0, split));
    controller.enqueue(encoded.slice(split));
    controller.close();
  }}));
  assert.deepEqual(await fetchLiveIdentity('123'), expected);
  target = new Response(new ReadableStream({pull(controller) {
    controller.enqueue(new Uint8Array(4_000_001));
  }, cancel() { canceled = true; }}));
  assert.equal((await fetchLiveIdentity('123')).status, 'unknown');
  assert.equal(canceled, true);
  canceled = false;
  target = new Response(new ReadableStream({cancel() { canceled = true; }}), {headers: {'content-length': '8000001'}});
  assert.equal((await fetchLiveIdentity('123')).status, 'unknown');
  assert.equal(canceled, true);
});
