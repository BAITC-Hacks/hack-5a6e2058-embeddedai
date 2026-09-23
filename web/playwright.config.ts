import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "../test-results",
  workers: 1,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:3037", viewport: {width: 1440, height: 1080}, trace: "retain-on-failure" },
  webServer: {
    command: "uv run --frozen python scripts/e2e_server.py",
    cwd: "..",
    url: "http://127.0.0.1:3037/health",
    reuseExistingServer: false,
    gracefulShutdown: {signal: "SIGTERM", timeout: 3000},
  },
});
