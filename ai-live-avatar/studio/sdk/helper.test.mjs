import assert from 'node:assert/strict';
import test from 'node:test';
import http from 'node:http';
import https from 'node:https';
import sdk from '@cloudbase/node-sdk';
import { runOnce } from './helper.mjs';

// Even a regression in the test adapter must not turn this suite into a cloud call.
http.request = https.request = () => { throw new Error('network_forbidden_in_offline_tests'); };
const ID = 'AKIDSyntheticOnlyCredential12345';
const KEY = 'synthetic-secret-not-real-123456789';
const input = role => ({ envId: 'synthetic-cloudbase-offline', region: 'ap-shanghai',
  model: 'deepseek-v4.1-flash-1017', role, credentials: { secretId: ID, secretKey: KEY },
  messages: [{ role: 'system', content: 'Only output json.' }, { role: 'user', content: 'data' }] });
const raw = () => ({ model: input('selection').model,
  choices: [{ finish_reason: 'stop', message: { content: '{"selected_id":null,"reason":"skip"}' } }],
  usage: { prompt_tokens: 12, completion_tokens: 7, total_tokens: 19 } });

test('installed official SDK pipeline forwards JSON/thinking, one paid step, no retries or keepalive', async () => {
  const initOptions = [], requests = [];
  const wrapper = { init(options) {
    initOptions.push(options);
    const app = sdk.init(options);
    return { ai() {
      const ai = app.ai();
      // Replace only the SDK's transport. generateText/processInput/default model
      // are the real installed implementation, including rawResponses/usage.
      ai.modelRequest = async request => { requests.push(request); return raw(); };
      return ai;
    } };
  } };
  const result = await runOnce(input('selection'), wrapper);
  assert.equal(result.ok, true);
  assert.equal(initOptions[0].env, input('selection').envId);
  assert.equal(initOptions[0].secretId, ID);
  assert.equal(initOptions[0].secretKey, KEY);
  assert.equal(initOptions[0].timeout, 45000);
  assert.equal(initOptions[0].retries, 0);
  assert.equal(initOptions[0].keepalive, false);
  assert.equal(initOptions[0].debug, false);
  assert.equal(requests.length, 1);
  const request = requests[0];
  assert.equal(request.url, `https://${input('selection').envId}.api.tcloudbasegateway.com/v1/ai/cloudbase/chat/completions`);
  assert.equal(request.data.model, input('selection').model);
  assert.equal(request.stream, false);
  assert.equal(request.data.stream, false);
  assert.deepEqual(request.data.thinking, { type: 'disabled' });
  assert.deepEqual(request.data.response_format, { type: 'json_object' });
  assert.equal(request.data.max_tokens, 512);
  assert.equal(request.data.tools, undefined);
  assert.equal(request.data.maxSteps, undefined); // SDK consumes it locally
  assert.deepEqual(result.response.usage, raw().usage);
  assert.equal(result.response.choices[0].finish_reason, 'stop');
  assert(!JSON.stringify(result).includes(ID));
  assert(!JSON.stringify(result).includes(KEY));
});

test('one generateText call, role limits, no tool registry and raw usage remains unknown', async () => {
  let generated = 0;
  const fake = { init: () => ({ ai: () => ({ createModel(group) {
    assert.equal(group, 'cloudbase');
    return { async generateText(options) {
      generated++;
      assert.equal(options.maxSteps, 1);
      assert.equal(options.max_tokens, 4096);
      assert(!('tools' in options));
      const response = raw(); delete response.usage;
      return { rawResponses: [response], usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } };
    } };
  } }) }) };
  const result = await runOnce(input('reply'), fake);
  assert.equal(generated, 1);
  assert.equal(result.ok, true);
  assert(!('usage' in result.response));
});

test('SDK errors and model mismatch produce only fixed safe codes', async () => {
  const fake = make => ({ init: () => ({ ai: () => ({ createModel: () => ({ generateText: make }) }) }) });
  for (const error of [new Error(KEY), Object.assign(new Error(KEY), { code: KEY })]) {
    assert.deepEqual(await runOnce(input('reply'), fake(async () => { throw error; })), { ok: false, code: 'sdk_error' });
  }
  assert.deepEqual(await runOnce(input('reply'), fake(async () => { throw Object.assign(new Error(KEY), { code: 'EXCEED_TOKEN_QUOTA_LIMIT' }); })), { ok: false, code: 'quota_exceeded' });
  const mismatch = raw(); mismatch.model = 'deepseek-v4-flash';
  assert.deepEqual(await runOnce(input('reply'), fake(async () => ({ rawResponses: [mismatch] }))), { ok: false, code: 'model_mismatch' });
  const errorBody = { code: 'AI_MODEL_DISABLED', message: KEY };
  assert.deepEqual(await runOnce(input('reply'), fake(async () => ({ rawResponses: [errorBody] }))), { ok: false, code: 'provider_error' });
});

test('invalid input cannot use malformed environments, roles or credential schemes', async () => {
  const forbidden = { init() { assert.fail('SDK init must not be reached'); } };
  for (const change of [{ envId: '' }, { envId: 'https://invalid.example' }, { envId: 'a'.repeat(64) }, { envId: 'invalid/label' }, { envId: 'valid\n' }, { envId: null }, { region: 'ap-singapore' },
    { model: 'deepseek-v4-flash' }, { role: 'execute_tool' }]) {
    assert.equal((await runOnce({ ...input('reply'), ...change }, forbidden)).code, 'configuration_invalid');
  }
  assert.equal((await runOnce({ ...input('reply'), credentials: { apiKey: KEY } }, forbidden)).code, 'credential_invalid');
});

test('tool-call finish, truncation and invalid usage are retained for Python validation', async () => {
  for (const reason of ['length', 'tool_calls']) {
    const response = raw(); response.choices[0].finish_reason = reason;
    const fake = { init: () => ({ ai: () => ({ createModel: () => ({ generateText: async () => ({ rawResponses: [response] }) }) }) }) };
    const result = await runOnce(input('reply'), fake);
    assert.equal(result.response.choices[0].finish_reason, reason);
    assert.deepEqual(result.response.usage, response.usage);
  }
});

test('oversized result and secret echo never escape helper', async () => {
  const fake = content => ({ init: () => ({ ai: () => ({ createModel: () => ({ generateText: async () => {
    const response = raw(); response.choices[0].message.content = content;
    return { rawResponses: [response] };
  } }) }) }) });
  assert.equal((await runOnce(input('reply'), fake('x'.repeat(256 * 1024)))).code, 'response_too_large');
  assert.equal((await runOnce(input('reply'), fake(KEY))).code, 'sdk_error');
});

test('operator-selected environment is forwarded without a default account', async () => {
  let selected;
  const fake = { init(options) {
    selected = options.env;
    return { ai: () => ({ createModel: () => ({ generateText: async () => ({ rawResponses: [raw()] }) }) }) };
  } };
  const result = await runOnce({ ...input('reply'), envId: 'operator-owned-environment' }, fake);
  assert.equal(result.ok, true);
  assert.equal(selected, 'operator-owned-environment');
});
