// One invocation, one generateText call. No tool registry, HTTP server or cloud
// management API is exposed. Secrets enter only through the parent's stdin pipe.
import { pathToFileURL } from 'node:url';

const ENV_ID = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const REGION = 'ap-shanghai';
const MODEL = 'deepseek-v4.1-flash-1017';
const LIMIT = 256 * 1024;
const INPUT_LIMIT = 128 * 1024;
const FIXED_CODES = {
  '401': 'unauthorized', '403': 'forbidden', '429': 'rate_limited',
  AI_MODEL_DISABLED: 'model_disabled', AI_MODEL_NOT_SUPPORTED: 'model_not_supported',
  AI_MODEL_NOT_FOUND: 'model_not_found', EXCEED_CONCURRENT_REQUEST_LIMIT: 'rate_limited',
  EXCEED_TOKEN_QUOTA_LIMIT: 'quota_exceeded', ETIMEDOUT: 'timeout', ESOCKETTIMEDOUT: 'timeout',
};

function failure(code) { return { ok: false, code }; }
function validCredential(value, secretId = false) {
  return typeof value === 'string' && (secretId ? /^AKID[A-Za-z0-9]{8,124}$/ : /^[!-~]{16,256}$/).test(value);
}

export async function runOnce(input, sdk) {
  if (!input || typeof input.envId !== 'string' || input.envId !== input.envId.trim() || !ENV_ID.test(input.envId) || input.region !== REGION || input.model !== MODEL
      || !['selection', 'reply'].includes(input.role)) return failure('configuration_invalid');
  const { secretId, secretKey } = input.credentials || {};
  if (!validCredential(secretId, true) || !validCredential(secretKey)) return failure('credential_invalid');
  if (!Array.isArray(input.messages) || input.messages.length !== 2
      || input.messages[0]?.role !== 'system' || input.messages[1]?.role !== 'user'
      || input.messages.some(message => typeof message.content !== 'string')) return failure('response_invalid');
  try {
    const app = sdk.init({ env: input.envId, region: REGION, secretId, secretKey,
      timeout: 45000, retries: 0, keepalive: false, debug: false });
    const model = app.ai().createModel('cloudbase');
    const result = await model.generateText({
      model: MODEL, messages: input.messages, maxSteps: 1,
      thinking: { type: 'disabled' }, response_format: { type: 'json_object' },
      max_tokens: input.role === 'selection' ? 512 : 4096,
      temperature: input.role === 'selection' ? 0.1 : 0.7,
    });
    // SDK usage fills missing values with zero. Only its original rawResponse is
    // evidence for provider usage/model/finish_reason; never emit SDK prompts.
    if (result?.error || !Array.isArray(result?.rawResponses) || result.rawResponses.length !== 1) return failure('sdk_error');
    const raw = result.rawResponses[0];
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return failure('response_invalid');
    if (raw.error != null || (raw.code != null && raw.code !== 'NORMAL')) return failure('provider_error');
    if (raw.model !== undefined && raw.model !== MODEL) return failure('model_mismatch');
    const choices = Array.isArray(raw.choices) ? raw.choices.map(choice => ({
      finish_reason: choice?.finish_reason,
      message: { content: choice?.message?.content },
    })) : null;
    const response = { choices };
    if (raw.model !== undefined) response.model = raw.model;
    if (raw.usage !== undefined) {
      response.usage = raw.usage;
      if (raw.usage && typeof raw.usage === 'object' && !Array.isArray(raw.usage)) {
        response.usage = Object.fromEntries(['prompt_tokens', 'completion_tokens', 'total_tokens']
          .filter(key => Object.hasOwn(raw.usage, key)).map(key => [key, raw.usage[key]]));
      }
    }
    const rid = raw.request_id ?? raw.requestId;
    if (typeof rid === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(rid)
        && !rid.includes(secretId) && !rid.includes(secretKey)) response.request_id = rid;
    const envelope = { ok: true, response };
    const serialized = JSON.stringify(envelope);
    if (Buffer.byteLength(serialized) > LIMIT) return failure('response_too_large');
    if (serialized.includes(secretId) || serialized.includes(secretKey)) return failure('sdk_error');
    return envelope;
  } catch (error) {
    // AI.handleResponseData can put an entire response in Error.message. Match
    // only known code literals, and never forward the message/stack/raw error.
    const direct = typeof error?.code === 'string' ? FIXED_CODES[error.code] : undefined;
    if (direct) return failure(direct);
    const message = typeof error?.message === 'string' ? error.message : '';
    for (const [known, code] of Object.entries(FIXED_CODES)) {
      if (/^[A-Z_]+$/.test(known) && message.includes(`错误码：${known}，`)) return failure(code);
    }
    return failure('sdk_error');
  }
}

async function main() {
  // SDK diagnostics may contain request data. The child owns stdout exclusively;
  // stderr is also discarded by the Python parent, not persisted or displayed.
  for (const name of ['log', 'warn', 'error', 'debug', 'info']) console[name] = () => {};
  let envelope = failure('sdk_error');
  try {
    const chunks = [];
    let size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > INPUT_LIMIT) throw new Error('input_limit');
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    // Dynamic import occurs only after input has been received, never on Python
    // app startup or configured(). No credential file path crosses into Node.
    const imported = await import('@cloudbase/node-sdk');
    envelope = await runOnce(input, imported.default ?? imported);
  } catch (_) { /* fixed envelope only */ }
  const raw = JSON.stringify(envelope);
  const output = Buffer.byteLength(raw) <= LIMIT ? raw : JSON.stringify(failure('response_too_large'));
  await new Promise(resolve => process.stdout.write(output, resolve));
  process.exit(0); // close any SDK-owned socket/timer; do not keep a daemon alive
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) await main();
