import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget =
    env.VITE_KG_AGENT_API_TARGET ||
    env.KG_AGENT_API_TARGET ||
    "http://127.0.0.1:9721";

  return {
    base: "/webui/",
    plugins: [react()],
    build: {
      outDir: resolve(__dirname, "../kg_agent/api/webui"),
      emptyOutDir: true,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (!id.includes("node_modules")) {
              return undefined;
            }
            if (id.includes("node_modules/cytoscape")) {
              return "graph-vendor";
            }
            if (
              id.includes("node_modules/react-router") ||
              id.includes("node_modules/@remix-run")
            ) {
              return "router-vendor";
            }
            if (
              id.includes("node_modules/@tanstack/react-query") ||
              id.includes("node_modules/zustand")
            ) {
              return "state-vendor";
            }
            if (
              id.includes("node_modules/react/") ||
              id.includes("node_modules/react-dom/")
            ) {
              return "react-vendor";
            }
            return "vendor";
          },
        },
      },
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/agent": {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: true,
    },
  };
});
