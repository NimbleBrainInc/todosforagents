import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppProvider } from "@nimblebrain/synapse/react";
// Establish the root-height chain (html/body/#root) before React mounts so the
// AppFrame shell fills the pane with no first-paint jump. AppFrame also injects
// this on render; the entry import just applies it earlier. (synapse ≥ 0.11)
import "@nimblebrain/synapse/ui/base";
import App from "./App";
// The bundle manifest carries the release version; tests/test_mcpb.py holds it equal to the package's.
import manifest from "../../mcpb/manifest.json";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProvider name="todos" version={manifest.version} forwardKeys>
      <App />
    </AppProvider>
  </StrictMode>,
);
