import { createRoot } from "react-dom/client";
import App from "./App";
import { StoreProvider } from "./state/store";
import "katex/dist/katex.min.css";
// Bundled fonts (owner D-9): latin only, the weights the HUD uses (Monaco draws bold tokens at 700). Imported before theme.css.
import "@fontsource/plus-jakarta-sans/latin-400.css";
import "@fontsource/plus-jakarta-sans/latin-600.css";
import "@fontsource/plus-jakarta-sans/latin-700.css";
import "@fontsource/geist-mono/latin-400.css";
import "@fontsource/geist-mono/latin-500.css";
import "@fontsource/geist-mono/latin-600.css";
import "@fontsource/geist-mono/latin-700.css";
import "./theme.css";
import { installSparkle } from "./lib/sparkle";
import { startAurora } from "./aurora";

installSparkle();
// The moving aurora behind #root (AG-2). Never throws: without WebGL2 the still CSS wash stays.
try { startAurora(); } catch { /* the still wash is the fallback */ }

createRoot(document.getElementById("root")!).render(
  <StoreProvider>
    <App />
  </StoreProvider>,
);
