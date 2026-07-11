import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev workflow: `uv run fastapi dev` serves the API on :8000; Vite proxies it.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
