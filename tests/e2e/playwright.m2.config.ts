import path from 'node:path';
import { defineConfig, devices } from '../../frontend/node_modules/@playwright/test/index.mjs';

export default defineConfig({
  testDir: '.',
  testMatch: 'm2.spec.ts',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: path.resolve(__dirname, '../../playwright-report-m2'), open: 'never' }]],
  outputDir: path.resolve(__dirname, '../../test-results-m2'),
  use: {
    baseURL: 'http://127.0.0.1:8081',
    screenshot: 'only-on-failure',
    // Never retain a trace containing login tokens or session cookies.
    trace: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'python tests/e2e/serve_m2.py',
    cwd: path.resolve(__dirname, '../..'),
    url: 'http://127.0.0.1:8081/api/health',
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      HELL_INC_HOST: '127.0.0.1', HELL_INC_PORT: '8081',
      HELL_INC_ORIGIN: 'http://127.0.0.1:8081',
      HELL_INC_WORLD_ID: 'browser-m2-test-world',
    },
  },
});
