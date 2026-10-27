// Calls to the bridge. A 401 means the bridge has a ui_pin: ask for it once, remember it, retry.
const PIN_KEY = "pin"

function getPin(): string | null {
  try {
    return localStorage.getItem(PIN_KEY)
  } catch {
    return null
  }
}

function setPin(pin: string) {
  try {
    localStorage.setItem(PIN_KEY, pin)
  } catch {
    /* private mode: ask again next time */
  }
}

let askPin: () => Promise<string | null> = async () => window.prompt("Bridge PIN")
export function onPinRequest(fn: () => Promise<string | null>) {
  askPin = fn
}

// body: JSON-encoded, unless it is a Blob (a file, sent as it is)
export async function api<T = unknown>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const raw = opts.body instanceof Blob
  const headers: Record<string, string> = { "Content-Type": raw ? (opts.body as Blob).type || "application/octet-stream" : "application/json" }
  const pin = getPin()
  if (pin) headers["X-Pin"] = pin
  const r = await fetch(path, {
    method: opts.method ?? "GET",
    headers,
    body: raw ? (opts.body as Blob) : opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })
  if (r.status === 401) {
    const text = await r.text()
    if (text.includes("PIN required") || text.includes("pin")) {
      const p = await askPin()
      if (p) {
        setPin(p)
        return api<T>(path, opts)
      }
    }
    throw new Error(text || "Unauthorized")
  }
  const text = await r.text()
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    data = text
  }
  if (!r.ok) {
    const err = data && typeof data === "object" && "error" in data ? String((data as { error: unknown }).error) : ""
    throw new Error(err || text || r.statusText)
  }
  return data as T
}

export const cover = (id: string) => `/api/cover/${encodeURIComponent(id)}`

export function hms(s: number | null | undefined) {
  const t = Math.round(s || 0)
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  return h ? `${h}h ${m}m` : `${m}m`
}

export const SPEEDS = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2]
export const speedLabel = (s: number) => `${s}×`

export function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e)
}
