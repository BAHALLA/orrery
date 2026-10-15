import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles/index.css";
import { applyTheme, readThemeChoice } from "./theme/theme";

// Before the first render, so the page never paints in the wrong theme. (An
// inline <script> in index.html would be earlier still, but the CSP allows
// only 'self' scripts.)
applyTheme(readThemeChoice());

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element #root not found");

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
