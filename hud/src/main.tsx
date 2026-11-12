import { createRoot } from "react-dom/client";
import App from "./App";
import { StoreProvider } from "./state/store";
import "katex/dist/katex.min.css";
// Bundled fonts (owner D-9): latin only, the weights theme.css uses. Imported before theme.css.
import "@fontsource/plus-jakarta-sans/latin-400.css";
import "@fontsource/plus-jakarta-sans/latin-600.css";
import "@fontsource/plus-jakarta-sans/latin-700.css";
import "@fontsource/geist-mono/latin-400.css";
import "@fontsource/geist-mono/latin-500.css";
import "@fontsource/geist-mono/latin-600.css";
import "./theme.css";
import { installSparkle } from "./lib/sparkle";

installSparkle();

createRoot(document.getElementById("root")!).render(
  <StoreProvider>
    <App />
  </StoreProvider>,
);
