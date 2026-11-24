import { createRoot } from "react-dom/client";
import "@fontsource/plus-jakarta-sans/latin-400.css";
import "@fontsource/plus-jakarta-sans/latin-600.css";
import "@fontsource/plus-jakarta-sans/latin-700.css";
import "@fontsource/geist-mono/latin-400.css";
import "@hud/theme.css";
import "./hud3.css";
import { installAppearance } from "@hud/lib/appearanceApply";
import { startAurora } from "@hud/aurora";
import App from "./App";

try { installAppearance(); } catch { /* Keep the theme defaults. */ }
try { startAurora(); } catch { /* Keep the still CSS wash. */ }

createRoot(document.getElementById("root")!).render(<App />);
