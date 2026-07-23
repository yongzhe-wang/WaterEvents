// Vite entry point — mounts the React app into #root and pulls in the global stylesheet. index.html loads
// this via <script type="module" src="/src/main.tsx">, so if this file is empty the build produces only the
// modulepreload polyfill (711 B) and the page renders blank. {restored 2026-07-01 — the src/ restructure
// committed an empty main.tsx, which shipped a white-screen build} [CONFIDENCE: CONFIRMED — prod entry was
// polyfill-only; App is the default export, styles.css is imported nowhere else].
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
