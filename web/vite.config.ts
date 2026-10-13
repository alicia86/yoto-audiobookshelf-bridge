import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The bridge (aiohttp) serves the built page: index.html at / and everything else under /static/.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/",
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: { outDir: "../yotobridge/static", emptyOutDir: true },
  server: { proxy: { "/api": process.env.BRIDGE ?? "http://127.0.0.1:18091", "/s": process.env.BRIDGE ?? "http://127.0.0.1:18091" } },
})
