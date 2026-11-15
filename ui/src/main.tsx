import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppProvider } from "@nimblebrain/synapse/react";
// Establish the root-height chain (html/body/#root) before React mounts so the
// AppFrame shell fills the pane with no first-paint jump. AppFrame also injects
// this on render; the entry import just applies it earlier. (synapse ≥ 0.11)
import "@nimblebrain/synapse/ui/base";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProvider name="todos" version="0.1.0" forwardKeys>
      <App />
    </AppProvider>
  </StrictMode>,
);
