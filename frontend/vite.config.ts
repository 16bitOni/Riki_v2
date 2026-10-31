import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the UI on :5173 proxies /api to the backend on :8000 (same origin, so the auth cookie just works).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } } },
});
