import path from 'node:path';
import { defineConfig, devices } from '../../frontend/node_modules/@playwright/test/index.mjs';

export default defineConfig({
  testDir: '.',
  testMatch: 'm4-phase2.spec.ts',
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: path.resolve(__dirname, '../../playwright-report-m4-phase2'), open: 'never' }]],
  outputDir: path.resolve(__dirname, '../../test-results-m4-phase2'),
  use: {
    baseURL: 'http://127.0.0.1:8084',
    screenshot: 'only-on-failure',
    // Never retain a trace containing login tokens or session cookies.
    trace: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'python tests/e2e/serve_m4_phase2.py',
    cwd: path.resolve(__dirname, '../..'),
    url: 'http://127.0.0.1:8084/api/health',
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      HELL_INC_HOST: '127.0.0.1', HELL_INC_PORT: '8084',
      HELL_INC_ORIGIN: 'http://127.0.0.1:8084',
      HELL_INC_WORLD_ID: 'browser-m4-phase2-test-world',
    },
  },
});
