/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies the API; point it at another backend with
// OPTIMIZARR_API=http://host:port npm run dev.
const apiTarget = process.env.OPTIMIZARR_API ?? "http://localhost:8080";
// OPTIMIZARR_API_READONLY=1 answers every writing request with 404 instead of
// forwarding it - for looking at a production backend without touching it.
const readOnly = process.env.OPTIMIZARR_API_READONLY === "1";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
        // changeOrigin rewrites Host to the backend, so the backend's WebSocket
        // origin check would see a foreign origin.  X-Forwarded-Host carries the
        // browser's host along (http-proxy's xfwd leaves it out for WebSockets).
        configure: (proxy) => {
          proxy.on("proxyReqWs", (proxyReq, req) => {
            if (req.headers.host) proxyReq.setHeader("X-Forwarded-Host", req.headers.host);
          });
        },
        bypass: readOnly
          ? (req) => (["GET", "HEAD", "OPTIONS"].includes(req.method ?? "GET") ? undefined : false)
          : undefined,
      },
    },
  },
  test: {
    environment: "jsdom",
    // Lets Testing Library register its automatic cleanup after each test.
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
