import assert from 'node:assert/strict';
import test from 'node:test';
import protobuf from 'protobufjs';
import { normalize } from '../src/pipeline/normalizer.js';

// Independent wire fixture: field numbers are repeated here deliberately so a
// wrong production schema cannot make its own encoder/decoder test pass.
const fixture = protobuf.parse(`
  syntax = "proto3";
  message FixtureGrade { int64 rank = 6; }
  message FixtureUser {
    uint64 uid = 1;
    string name = 3;
    uint32 unrelatedLevel = 6;
    FixtureGrade wealth = 23;
  }
  message FixtureMember {
    FixtureUser viewer = 2;
    int64 total = 3;
    bool adminChange = 5;
    uint64 entry = 9;
    uint64 operation = 10;
  }
  message FixtureGiftInfo { int64 ident = 5; string label = 16; }
  message FixtureGift {
    int64 ident = 2;
    int64 groupQuantity = 4;
    int64 repeats = 5;
    int64 combos = 6;
    FixtureUser viewer = 7;
    int32 ended = 9;
    int64 group = 11;
    FixtureGiftInfo info = 15;
  }
  message FixtureChat { FixtureUser viewer = 2; string text = 3; }
`).root;

function encodeFixture(type: protobuf.Type, body: Record<string, any>): Uint8Array {
  const writer = protobuf.Writer.create();
  type.resolveAll();
  for (const field of type.fieldsArray) {
    if (!Object.hasOwn(body, field.name)) continue;
    const value = body[field.name];
    if (field.resolvedType instanceof protobuf.Type) {
      writer.uint32((field.id << 3) | 2).bytes(encodeFixture(field.resolvedType, value));
    } else if (field.type === 'string') {
      writer.uint32((field.id << 3) | 2).string(value);
    } else {
      // Proto3 encoders may omit 0/false. Write explicitly supplied defaults so
      // the fixture also exercises the decoder's real wire-field presence.
      writer.uint32(field.id << 3);
      if (field.type === 'bool') writer.bool(value);
      else if (field.type === 'int64') writer.int64(value);
      else if (field.type === 'uint64') writer.uint64(value);
      else if (field.type === 'int32') writer.int32(value);
      else if (field.type === 'uint32') writer.uint32(value);
      else throw new Error(`Unsupported fixture type: ${field.type}`);
    }
  }
  return writer.finish();
}

function event(type: string, method: string, body: Record<string, unknown>) {
  const payload = encodeFixture(fixture.lookupType(type), body);
  const result = normalize({ method, msgId: 'fixture-message', payload }, { roomId: 'fixture-room' });
  assert.ok(result);
  // SSE JSON is the public boundary; undefined must remain absent on the wire.
  return JSON.parse(JSON.stringify(result));
}

test('member decodes nested wealth field 23/6 and action/admin/entry tags', () => {
  const actual = event('FixtureMember', 'WebcastMemberMessage', {
    viewer: { uid: '123', name: '测试观众', unrelatedLevel: 99, wealth: { rank: 11 } },
    total: 20, adminChange: false, entry: 0, operation: 1,
  });
  assert.equal(actual.user.payGradeLevel, 11);
  assert.equal(actual.user.level, undefined);
  assert.deepEqual(actual.data, { memberCount: 20, action: 1, isSetToAdmin: false, enterType: 0 });
});

test('missing grade and member flags stay unknown despite protobuf prototype defaults', () => {
  for (const viewer of [
    { uid: '123', unrelatedLevel: 99 },
    { uid: '123', wealth: {} },
    { uid: '123', wealth: { rank: -1 } },
    { uid: '123', wealth: { rank: '9007199254740992' } },
  ]) {
    const actual = event('FixtureMember', 'WebcastMemberMessage', { viewer });
    assert.equal(Object.hasOwn(actual.user, 'payGradeLevel'), false);
    assert.deepEqual(actual.data, {});
  }
});

test('explicit zero grade and member action remain known without becoming entrance', () => {
  const actual = event('FixtureMember', 'WebcastMemberMessage', {
    viewer: { uid: '123', wealth: { rank: 0 } }, adminChange: true, operation: 0, entry: 6,
  });
  assert.equal(actual.user.payGradeLevel, 0);
  assert.deepEqual(actual.data, { action: 0, isSetToAdmin: true, enterType: 6 });
});

test('gift identifiers stay exact strings and counters are passed without multiplying', () => {
  const actual = event('FixtureGift', 'WebcastGiftMessage', {
    ident: '9007199254740993', group: '9007199254740995', info: { label: '测试礼物' },
    viewer: { uid: '123', wealth: { rank: 12 } },
    groupQuantity: 2, repeats: 3, combos: 4, ended: 0,
  });
  assert.equal(actual.data.giftId, '9007199254740993');
  assert.equal(actual.data.groupId, '9007199254740995');
  assert.equal(actual.data.giftName, '测试礼物');
  assert.equal(actual.data.groupCount, 2);
  assert.equal(actual.data.repeatCount, 3);
  assert.equal(actual.data.comboCount, 4);
  assert.equal(actual.data.repeatEnd, false);
  assert.equal(actual.user.payGradeLevel, 12);
});

test('gift absent counters/group/end stay unknown; explicit end is boolean', () => {
  const unknown = event('FixtureGift', 'WebcastGiftMessage', { ident: 1 });
  for (const key of ['repeatCount', 'comboCount', 'groupCount', 'groupId', 'repeatEnd']) {
    assert.equal(Object.hasOwn(unknown.data, key), false);
  }
  const ended = event('FixtureGift', 'WebcastGiftMessage', { ident: 1, ended: 1, repeats: 0, group: 0 });
  assert.equal(ended.data.repeatEnd, true);
  assert.equal(ended.data.repeatCount, 0);
  assert.equal(ended.data.groupId, '0');
  const malformed = event('FixtureGift', 'WebcastGiftMessage', {
    ident: 1, ended: 2, repeats: -1, combos: '9007199254740992',
  });
  assert.equal(Object.hasOwn(malformed.data, 'repeatEnd'), false);
  assert.equal(Object.hasOwn(malformed.data, 'repeatCount'), false);
  assert.equal(Object.hasOwn(malformed.data, 'comboCount'), false);
});

test('chat preserves content and viewer identity without mistaking ordinary level for wealth', () => {
  const actual = event('FixtureChat', 'WebcastChatMessage', {
    viewer: { uid: '123', name: '测试观众', unrelatedLevel: 99 }, text: '原评论内容',
  });
  assert.equal(actual.type, 'chat');
  assert.equal(actual.platform, 'douyin');
  assert.equal(actual.roomId, 'fixture-room');
  assert.equal(actual.id, 'fixture-message');
  assert.deepEqual(actual.data, { content: '原评论内容' });
  assert.deepEqual(actual.user, { id: '123', nickname: '测试观众' });
});
