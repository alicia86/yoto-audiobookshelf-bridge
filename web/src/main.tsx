import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import App from "@/App"
import { OverlayProvider } from "@/components/overlays"
import { Toaster } from "@/components/ui/sonner"
import "@/index.css"

// Follow the phone's light/dark setting (shadcn's dark styles key off a .dark class).
const dark = window.matchMedia("(prefers-color-scheme: dark)")
const applyTheme = () => document.documentElement.classList.toggle("dark", dark.matches)
applyTheme()
dark.addEventListener("change", applyTheme)

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <OverlayProvider>
      <App />
      <Toaster position="top-center" richColors />
    </OverlayProvider>
  </StrictMode>,
)
