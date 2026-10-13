import { AlertTriangle, ExternalLink, Link2 } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api, errorText } from "@/lib/api"
import type { Status } from "@/types"

export function Banners({ status, refresh }: { status: Status; refresh: () => void }) {
  const [pasted, setPasted] = useState("")
  const login = async () => {
    try {
      await api("/api/yoto/login", { method: "POST" })
      refresh()
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  const finish = async () => {
    try {
      await api("/api/yoto/finish", { method: "POST", body: { url: pasted } })
      toast.success("Yoto connected")
      refresh()
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  const out = []
  if (!status.yoto_client_id_set) {
    out.push(
      <Alert key="setup">
        <Link2 />
        <AlertTitle>Set up Yoto</AlertTitle>
        <AlertDescription>
          <p>
            Add <code className="font-mono">yoto_client_id</code> (from dashboard.yoto.dev) to config.yaml and restart the bridge.
          </p>
        </AlertDescription>
      </Alert>,
    )
  } else if (status.login_pending) {
    out.push(
      <Alert key="pending">
        <Link2 />
        <AlertTitle>Sign in to Yoto</AlertTitle>
        <AlertDescription className="gap-3">
          <Button asChild>
            <a href={status.login_pending.url} target="_blank" rel="noopener">
              Open the Yoto sign-in page <ExternalLink />
            </a>
          </Button>
          <p>Sign in and allow access. Yoto then sends you to a page saying “Yoto connected”, and this page updates by itself.</p>
          <p className="text-xs">If you end up on a page that does not load, copy its whole address from the address bar and paste it here:</p>
          <Input placeholder="https://… ?code=…" value={pasted} onChange={(e) => setPasted(e.target.value)} />
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" onClick={finish}>
              Finish
            </Button>
            <Button size="sm" variant="ghost" onClick={login}>
              Start over
            </Button>
          </div>
        </AlertDescription>
      </Alert>,
    )
  } else if (!status.yoto_logged_in) {
    out.push(
      <Alert key="connect">
        <Link2 />
        <AlertTitle>Connect your Yoto account</AlertTitle>
        <AlertDescription className="gap-3">
          <p>Needed to make cards and to play on a Yoto from here.</p>
          <Button onClick={login}>Connect Yoto</Button>
        </AlertDescription>
      </Alert>,
    )
  } else if (status.error) {
    out.push(
      <Alert key="error" variant="destructive">
        <AlertTriangle />
        <AlertTitle>Yoto</AlertTitle>
        <AlertDescription>{status.error}</AlertDescription>
      </Alert>,
    )
  }
  if (status.yoto_logged_in && !status.login_pending && status.icon_upload_error) {
    out.push(
      <Alert key="icons">
        <Link2 />
        <AlertTitle>Chapter icons for offline copies are off</AlertTitle>
        <AlertDescription className="gap-3">
          <p>
            Yoto refused the icon upload. Tick <code className="font-mono">user:icons:manage</code> under Scopes for your app at dashboard.yoto.dev, then reconnect.
          </p>
          <Button size="sm" onClick={login}>
            Reconnect Yoto
          </Button>
        </AlertDescription>
      </Alert>,
    )
  }
  for (const [i, p] of (status.problems ?? []).entries()) {
    out.push(
      <Alert key={`p${i}`} variant="destructive">
        <AlertTriangle />
        <AlertTitle>Problem</AlertTitle>
        <AlertDescription>{p}</AlertDescription>
      </Alert>,
    )
  }
  return out.length ? <div className="flex flex-col gap-3 px-4 pt-3">{out}</div> : null
}
