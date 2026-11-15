import { defineConfig } from "vitest/config";

// A standalone vitest config — deliberately NOT the app's vite.config.ts, whose
// synapseVite() plugin spawns the dev/preview server (noise and a hang under the
// test runner). The suite mounts components, so the environment is jsdom.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
