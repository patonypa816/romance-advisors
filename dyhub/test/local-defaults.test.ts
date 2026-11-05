import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const read = (path: string) => readFileSync(new URL('../' + path, import.meta.url), 'utf8');

test('direct launch and container publish defaults keep the unauthenticated API local', () => {
  assert.match(read('src/index.ts'), /const HOST = process\.env\.DYHUB_HOST \?\? '127\.0\.0\.1'/);
  assert.match(read('docker-compose.yml'), /"127\.0\.0\.1:8757:8757"/);
  assert.doesNotMatch(read('docker-compose.yml'), /-\s+"8757:8757"/);
  assert.doesNotMatch(read('README.md'), /-p 8757:8757/);
});

test('container template preserves browser sandbox and both license notices', () => {
  const docker = read('Dockerfile');
  assert.match(docker, /^USER node$/m);
  assert.match(docker, /^COPY LICENSE \.\/LICENSE$/m);
  assert.match(docker, /^COPY third_party \.\/third_party$/m);
  assert.match(read('src/collector/browser.ts'), /chromiumSandbox: true/);
  assert.doesNotMatch(read('src/collector/browser.ts'), /--no-sandbox|chromiumSandbox:\s*false/);
});
