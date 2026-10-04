import path from "path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const host = process.env.TAURI_DEV_HOST;

export default defineConfig(async () => ({
  plugins: [
    react(),
    tailwindcss(),
  ],

  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },

  clearScreen: false,

  server: {
    // 5173/5174 rather than the Tauri-conventional 1420/1421: Hyper-V/WSL
    // reserves 1351-1450 on Windows, and an excluded port fails to bind with
    // EACCES rather than EADDRINUSE. Must match build.devUrl in tauri.conf.json.
    port: 5173,
    strictPort: true,
    host: host || false,

    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 5174,
        }
      : undefined,

    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
}));