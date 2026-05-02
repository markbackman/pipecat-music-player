import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The transport config in ``src/config.ts`` points directly at the bot
// start URL (absolute origin), so no dev proxy is needed.
//
// `resolve.dedupe` forces a single copy of each listed package across
// the entire module graph. Required here because:
//
// - `@pipecat-ai/client-js` and `-react` are `file:` deps pointing at
//   the local pipecat-client-web workspace. Transitive consumers
//   (`@pipecat-ai/voice-ui-kit`) ship their own resolution; without
//   dedupe they'd import a second copy, `PipecatClientProvider` would
//   publish to one React context and our `usePipecatClient` would
//   read from another, so `UIAgentClient` never gets built.
// - The local pipecat-client-web workspace has its own React 18 in
//   `node_modules` (from its devDependencies); without dedupe that
//   copy coexists with the app's React 19 and hooks fail with
//   "Invalid hook call".
// - `jotai` is used internally by `@pipecat-ai/client-react` and
//   relies on React context, so it has the same single-copy rule.
export default defineConfig({
  plugins: [react()],
  resolve: {
    dedupe: [
      "react",
      "react-dom",
      "jotai",
      "@pipecat-ai/client-js",
      "@pipecat-ai/client-react",
    ],
  },
  server: {
    allowedHosts: true,
  },
});
