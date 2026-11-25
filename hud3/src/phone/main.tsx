import { createRoot } from "react-dom/client";
import "@fontsource/plus-jakarta-sans/latin-400.css";
import "@fontsource/plus-jakarta-sans/latin-600.css";
import "@fontsource/plus-jakarta-sans/latin-700.css";
import "@fontsource/geist-mono/latin-400.css";
import "@hud/theme.css";
import "../hud3.css";
import { setSurface } from "../api";
import { PhoneApp, takePairingCode } from "./PhoneApp";
import "./phone.css";

setSurface("phone");
const pairingCode = takePairingCode(location, history);
createRoot(document.getElementById("root")!).render(<PhoneApp pairingCode={pairingCode} />);
