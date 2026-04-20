import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The transport config in ``src/config.ts`` points directly at the bot
// start URL (absolute origin), so no dev proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: true,
  },
});
