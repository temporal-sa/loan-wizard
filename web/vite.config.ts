import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The wizard talks only to FastAPI, never to Temporal directly. Every `/api`
// call is proxied to the API on :8000, with the `/api` prefix stripped so it
// maps onto the FastAPI routes (`/applications/...`).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        // 127.0.0.1 (not "localhost") so the proxy always hits the IPv4 API and
        // never an IPv6 process that happens to share the port.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
