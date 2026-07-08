import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// Build-Ziel ist das static/-Verzeichnis des Orchestrators: der Server
// liefert die UI unter /app aus (main.py), ein Server-Deploy aktualisiert
// damit alle Clients (Windows-Shell, Browser) gleichzeitig.
export default defineConfig({
  base: "/app/",
  plugins: [svelte()],
  build: {
    outDir: "../orchestrator/src/orchestrator/static/app",
    emptyOutDir: true,
  },
  server: {
    // Dev-Modus: Vite auf :5173, API-Aufrufe gehen an den lokal laufenden
    // Orchestrator durch (inkl. WebSocket).
    proxy: {
      "/v1": { target: "http://localhost:8000", ws: true },
    },
  },
});
