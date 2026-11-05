import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import test from 'node:test';
import {registerSseRoute} from '../src/dispatch/sseServer.js';
import {EventBus} from '../src/pipeline/eventBus.js';

function subscription(status?: string) {
  let handler: any;
  const frames: string[] = [];
  const socket = new EventEmitter();
  let connectCalls = 0;
  const roomId = '123';
  const bus = new EventBus();
  const collector = {
    getRoomInfo: () => status ? {roomId, status} : undefined,
    async connect(id: string) { assert.equal(id, roomId); connectCalls++; return {roomId, status: 'connecting'}; },
  };
  registerSseRoute({get: (_path: string, fn: any) => { handler = fn; }} as any, bus, collector as any);
  const reply = {raw: {writableEnded: false, writeHead() {}, write(value: string) { frames.push(value); }}, hijack() {}};
  handler({query: {roomId, types: 'chat,roomStatus'}, raw: socket}, reply);
  return {frames, bus, calls: () => connectCalls, close: () => socket.emit('close')};
}

test('explicit SSE reconnect rebuilds missing, stopped, closed and error rooms', async () => {
  for (const status of [undefined, 'stopped', 'closed', 'error']) {
    const s = subscription(status);
    try {
      await Promise.resolve();
      assert.equal(s.calls(), 1, status);
      assert.ok(s.frames.some(frame => frame.includes('"type":"__connecting"')));
    } finally { s.close(); }
  }
});

test('SSE reuses live and connecting rooms without creating another collection', () => {
  for (const status of ['live', 'connecting']) {
    const s = subscription(status);
    try {
      assert.equal(s.calls(), 0, status);
      assert.ok(s.frames.some(frame => frame.includes('"type":"__connected"')));
    } finally { s.close(); }
  }
});

test('SSE filters other rooms and types, preserves the normalized avatar, and unsubscribes on close', () => {
  const s = subscription('live');
  const event: any = {id: 'synthetic', roomId: '123', platform: 'douyin', type: 'chat',
    ts: 1000, receivedAt: 1000, user: {id: 'viewer', nickname: '合成观众', avatar: 'https://p3.douyinpic.com/a.jpg'},
    data: {content: '合成建议'}};
  try {
    const before = s.frames.length;
    s.bus.publish({...event, roomId: '456'}); s.bus.publish({...event, type: 'gift'});
    assert.equal(s.frames.length, before);
    s.bus.publish(event);
    assert.equal(s.frames.length, before + 1);
    assert.equal(JSON.parse(s.frames.at(-1)!.slice(6)).user.avatar, event.user.avatar);
    s.close(); s.bus.publish(event);
    assert.equal(s.frames.length, before + 1);
  } finally { s.close(); }
});
