import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5490; proxy /api → the local serverless-shim on :8490 so the
// browser makes same-origin calls and CORS is a non-issue in dev. Ports moved off
// the project defaults (5190/8100) so parallel forks/sessions don't collide.
// {USER 2026-06-09 "use a different localhost, this one might be used by others"}
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5490,                        // was 5190 — moved to dodge other sessions
    proxy: {
      "/api": "http://localhost:8490", // shim port — keep in sync with dev-api-server.mjs
    },
  },
  // Never emit source maps in the production bundle — they would expose readable source on the public
  // site. vite's default is already false; pinned explicitly so a future config change can't silently
  // enable it. {review 2026-06-18 security: lock build.sourcemap=false} [CONFIDENCE: CONFIRMED — vite default false].
  build: { sourcemap: false },
});
