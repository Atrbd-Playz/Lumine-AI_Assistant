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
    // 15173/15174 rather than the Tauri-conventional 1420/1421 or Vite's own
    // 5173. Hyper-V/WSL redraws three 100-port exclusion blocks inside the
    // Windows dynamic port range (1024-15000 here) on every boot, and an
    // excluded port fails to bind with EACCES rather than EADDRINUSE: 1420 fell
    // to 1351-1450, 5173 to 5156-5255. Every observed block has landed inside
    // that range, so a port above it survives the redraw; 15173 also stays
    // below the Windows default 49152, so it holds either way round.
    // Must match build.devUrl in tauri.conf.json.
    port: 15173,
    strictPort: true,
    host: host || false,

    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 15174,
        }
      : undefined,

    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
}));