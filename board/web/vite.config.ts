import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

const server = "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": { target: server },
      "/ws": { target: server, ws: true },
    },
  },
  build: {
    target: "es2022",
    // The map tab's chunk is mostly three.js (about 630 kB); it loads only when the tab is opened.
    chunkSizeWarningLimit: 700,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
