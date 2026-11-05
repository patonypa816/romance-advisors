import assert from 'node:assert/strict';
import test from 'node:test';
import protobuf from 'protobufjs';
import { normalize } from '../src/pipeline/normalizer.js';

// Independent wire schema: verifies the imported decoder's public avatar field
// without any live room, stored account, browser, or downloaded avatar image.
const schema = protobuf.parse(`
  syntax = "proto3";
  message Picture { repeated string urls = 1; }
  message Viewer {
    uint64 uid = 1;
    string name = 3;
    Picture thumb = 9;
    Picture medium = 10;
    Picture large = 11;
  }
  message Comment { Viewer viewer = 2; string text = 3; }
`).root.lookupType('Comment');

function comment(pictures: Record<string, { urls: string[] }>) {
  const payload = schema.encode(schema.fromObject({
    viewer: { uid: '9007199254740993', name: '合成测试观众', ...pictures },
    text: '合成游戏建议',
  })).finish();
  const event = normalize({ method: 'WebcastChatMessage', msgId: 'synthetic-avatar-message', payload },
    { roomId: 'synthetic-room' });
  assert.ok(event);
  return JSON.parse(JSON.stringify(event));
}

test('chat carries thumbnail URL and exact viewer ID through the JSON boundary', () => {
  const event = comment({ thumb: { urls: ['https://example.invalid/avatar-thumb.png'] } });
  assert.equal(event.user.avatar, 'https://example.invalid/avatar-thumb.png');
  assert.equal(event.user.id, '9007199254740993');
  assert.equal(event.user.nickname, '合成测试观众');
  assert.equal(event.data.content, '合成游戏建议');
  assert.equal(event.type, 'chat');
});

test('thumbnail takes priority over medium and large avatars', () => {
  const event = comment({
    thumb: { urls: ['https://example.invalid/thumb.png'] },
    medium: { urls: ['https://example.invalid/medium.png'] },
    large: { urls: ['https://example.invalid/large.png'] },
  });
  assert.equal(event.user.avatar, 'https://example.invalid/thumb.png');
});

test('empty or invalid thumbnail falls back to medium then large', () => {
  assert.equal(comment({
    thumb: { urls: [] }, medium: { urls: ['https://example.invalid/medium.png'] },
  }).user.avatar, 'https://example.invalid/medium.png');
  assert.equal(comment({
    thumb: { urls: ['data:image/png;base64,synthetic'] },
    large: { urls: ['https://example.invalid/large.png'] },
  }).user.avatar, 'https://example.invalid/large.png');
});

test('missing avatar remains absent without losing the comment', () => {
  const event = comment({});
  assert.equal(Object.hasOwn(event.user, 'avatar'), false);
  assert.equal(event.data.content, '合成游戏建议');
});
