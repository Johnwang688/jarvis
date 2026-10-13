import { createRoot } from "react-dom/client";
import App from "./App";
import { StoreProvider } from "./state/store";
import "katex/dist/katex.min.css";
import "./theme.css";

createRoot(document.getElementById("root")!).render(
  <StoreProvider>
    <App />
  </StoreProvider>,
);
