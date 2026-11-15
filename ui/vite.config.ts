import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import { synapseVite } from "@nimblebrain/synapse/vite";

// One inlined HTML file (vite-plugin-singlefile), so the server serves the whole app as one
// `ui://todos/main` resource.
//
// `npm run dev` previews the board at /__preview against the real server: synapseVite() starts
// the local runtime over stdio, on its own database under ui/.preview/, and proxies the board's
// tool calls to it. The app name comes from the bundle's manifest.
export default defineConfig({
  plugins: [
    react(),
    viteSingleFile(),
    synapseVite({
      manifest: "../mcpb/manifest.json",
      serverCmd: "env TODOS_LOCAL_DB=.preview/todos.db uv run --project .. todosforagents",
    }),
  ],
  build: {
    outDir: "dist",
    assetsInlineLimit: Infinity,
  },
});
