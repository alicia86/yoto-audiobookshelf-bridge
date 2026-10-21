// Pixel icon styles: a sheet to pick a card's style (with previews of each, drawn by the bridge from
// the chapter the card is at) and the default style for every card without its own.
import { ArrowLeft, Bot, ImagePlus, ListOrdered, ShieldCheck, Sparkles, Square, Wand2, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, errorText } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { AIJob, AISettings, CardSlot, IconSettings } from "@/types"

const label = (s: IconSettings, id: string) => s.styles.find((x) => x.id === id)?.label ?? id

// The card's icon, drawn 3x bigger with sharp pixels
export function IconPreview({ slot, style, bust, className }: { slot: string; style?: string; bust?: number; className?: string }) {
  const [failed, setFailed] = useState(false)
  const q = new URLSearchParams()
  if (style) q.set("style", style)
  if (bust) q.set("v", String(bust))
  if (failed)
    return <div className={cn("flex size-12 items-center justify-center rounded bg-muted text-[10px] text-muted-foreground", className)}>none</div>
  return (
    <img
      src={`/api/cards/${slot}/icon.png?${q}`}
      alt=""
      className={cn("size-12 rounded bg-black [image-rendering:pixelated]", className)}
      onError={() => setFailed(true)}
    />
  )
}

// A picture from the phone, scaled down in the browser (photos are megabytes; the Yoto shows 16x16)
async function shrink(file: File): Promise<Blob> {
  const img = await createImageBitmap(file)
  const side = Math.min(img.width, img.height)
  const canvas = document.createElement("canvas")
  canvas.width = canvas.height = 64
  canvas.getContext("2d")!.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, 64, 64)
  return new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not read the picture"))), "image/png"))
}

export function IconSheet({ card, settings, onDone }: { card: CardSlot; settings: IconSettings; onDone: (msg?: string) => void }) {
  const [busy, setBusy] = useState(false)
  const [perChapter, setPerChapter] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const bust = useRef(Date.now()).current
  const playing = (r: { playing?: boolean }) => (r.playing ? " The Yoto shows it once this card stops playing." : "")

  const pick = async (style: string) => {
    setBusy(true)
    try {
      const r = await api<{ playing?: boolean }>(`/api/cards/${card.slot}/icon`, { method: "PUT", body: { style } })
      onDone((style ? `Icons: ${label(settings, style)}.` : "Icons: the default style.") + playing(r))
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const upload = async (f: File | undefined) => {
    if (!f) return
    setBusy(true)
    try {
      const r = await api<{ playing?: boolean }>(`/api/cards/${card.slot}/icon/image`, { method: "PUT", body: await shrink(f) })
      onDone("Icons: your picture." + playing(r))
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  if (perChapter) return <ChapterPictures card={card} settings={settings} onBack={() => setPerChapter(false)} onDone={onDone} />

  const options = settings.styles.filter((s) => s.id !== "custom" || card.icon_custom || card.icon_chapters)
  const current = card.icon_style || ""
  return (
    <div className="flex flex-col gap-3 pb-2">
      <div className="grid grid-cols-2 gap-2">
        <StyleButton active={current === ""} disabled={busy} onClick={() => pick("")} title="Default" note={label(settings, settings.style)}>
          <IconPreview slot={card.slot} style={settings.style} bust={bust} />
        </StyleButton>
        {options.map((s) => (
          <StyleButton key={s.id} active={current === s.id} disabled={busy} onClick={() => pick(s.id)} title={s.label}>
            <IconPreview slot={card.slot} style={s.id} bust={bust} />
          </StyleButton>
        ))}
      </div>
      <input ref={file} type="file" accept="image/*" className="hidden" onChange={(e) => upload(e.target.files?.[0])} />
      <Button variant="outline" disabled={busy} onClick={() => file.current?.click()}>
        <ImagePlus /> {card.icon_custom ? "Choose a different picture" : "Use a picture of your own"}
      </Button>
      <Button variant="outline" disabled={busy} onClick={() => setPerChapter(true)}>
        <ListOrdered /> Pictures per chapter{card.icon_chapters ? ` (${card.icon_chapters})` : ""}
      </Button>
      <p className="text-xs text-muted-foreground">
        Previews show the chapter this card is at. Theme icons come from Yoto's icon library, matched to chapter titles; chapters without a telling title
        (like “Chapter 7”) show the cover and number instead.
      </p>
    </div>
  )
}

type ChapterRow = { item_id: string; number: number; title: string; picture: string }

// A picture for each chapter of the book the card is on. Chapters without one show the card's
// picture, or the default style when the card has none.
function ChapterPictures({ card, settings, onBack, onDone }: {
  card: CardSlot
  settings: IconSettings
  onBack: () => void
  onDone: (msg?: string) => void
}) {
  const [rows, setRows] = useState<ChapterRow[] | null>(null)
  const [job, setJob] = useState<AIJob | null>(card.ai_job ?? null)
  const [busy, setBusy] = useState(false)
  const [changed, setChanged] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const target = useRef<ChapterRow | null>(null)
  const load = async () => {
    try {
      setRows(await api<ChapterRow[]>(`/api/cards/${card.slot}/icon/chapters`))
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.slot])

  // while AI pictures are being made: follow the job and show each picture as it lands
  const running = job?.status === "running"
  useEffect(() => {
    if (!running) return
    const t = setInterval(async () => {
      try {
        const cards = await api<CardSlot[]>("/api/cards")
        const j = cards.find((c) => c.slot === card.slot)?.ai_job ?? null
        setJob(j)
        await load()
        if (j && j.status !== "running") {
          setChanged(true)
          if (j.status === "failed") toast.error(`AI pictures stopped: ${j.error}`)
        }
      } catch {
        /* try again on the next tick */
      }
    }, 3000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, card.slot])

  const startAI = async (overwrite: boolean, target: "chapters" | "card" = "chapters") => {
    try {
      setJob(await api<AIJob>(`/api/cards/${card.slot}/icon/ai`, { method: "POST", body: { overwrite, target } }))
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  const stopAI = async () => {
    try {
      await api(`/api/cards/${card.slot}/icon/ai`, { method: "DELETE" })
      setJob((j) => (j ? { ...j, status: "cancelled" } : j))
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  const send = async (row: ChapterRow, f?: File) => {
    setBusy(true)
    try {
      const path = `/api/cards/${card.slot}/icon/chapters/${encodeURIComponent(row.item_id)}/${row.number}`
      await api(path, f ? { method: "PUT", body: await shrink(f) } : { method: "DELETE" })
      setChanged(true)
      await load()
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const done = rows?.filter((r) => r.picture).length ?? 0
  return (
    <div className="flex flex-col gap-2 pb-2">
      <input
        ref={file}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          e.target.value = ""
          if (f && target.current) send(target.current, f)
        }}
      />
      <p className="text-xs text-muted-foreground">
        Chapters of {card.title || "this book"}. Chapters without a picture show the card's picture, or the default style. {done ? `${done} chosen.` : ""}
      </p>
      {settings.ai?.enabled && !!rows?.length && (
        <div className="flex flex-col gap-1.5 rounded-md bg-muted/60 p-2 text-xs">
          {running ? (
            <div className="flex items-center gap-2">
              <Wand2 className="size-4 shrink-0 animate-pulse" />
              <span className="min-w-0 flex-1">
                {job!.total ? `${job!.done} of ${job!.total}: ` : ""}
                {job!.step}
              </span>
              <Button size="sm" variant="ghost" onClick={stopAI}>
                <Square /> Stop
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              <Button size="sm" variant="secondary" disabled={busy || done === rows.length} onClick={() => startAI(false)}>
                <Wand2 /> Make AI pictures{done ? " for the rest" : ""}
              </Button>
              {done > 0 && (
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => startAI(true)}>
                  Redo all
                </Button>
              )}
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => startAI(false, "card")}>
                <Wand2 /> {card.icon_custom ? "New card picture" : "Card picture"}
              </Button>
            </div>
          )}
          <p className="text-muted-foreground">
            {settings.ai.mode === "claude"
              ? `Claude draws a 16x16 picture for each chapter from the book's description and the chapter's title${settings.ai.transcribe ? " and opening" : ""}. “Card picture” draws one for the whole card.`
              : settings.ai.mode === "images"
                ? "Listens to the start of each chapter and draws a picture for it."
                : "Listens to the start of each chapter and picks the best fitting icon from Yoto's library."}{" "}
            {job?.status === "done" && job.skipped ? `${job.skipped} chapters had nothing kid-safe to show and were left as they were.` : ""}
          </p>
        </div>
      )}
      {!rows ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : !rows.length ? (
        <p className="text-sm text-muted-foreground">This book has no chapters.</p>
      ) : (
        <div className="flex max-h-[50vh] flex-col gap-1 overflow-y-auto">
          {rows.map((r) => (
            <div key={`${r.item_id}:${r.number}`} className="flex items-center gap-2 rounded-md border p-1.5 text-sm">
              {r.picture ? (
                <img
                  src={`/api/cards/${card.slot}/icon/chapters/${encodeURIComponent(r.item_id)}/${r.number}?v=${r.picture}`}
                  alt=""
                  className="size-9 shrink-0 rounded bg-black [image-rendering:pixelated]"
                />
              ) : (
                <div className="flex size-9 shrink-0 items-center justify-center rounded bg-muted text-xs text-muted-foreground">{r.number}</div>
              )}
              <span className="min-w-0 flex-1 truncate">{r.title}</span>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  target.current = r
                  file.current?.click()
                }}
              >
                <ImagePlus /> {r.picture ? "Change" : "Choose"}
              </Button>
              {r.picture && (
                <Button size="icon" variant="ghost" className="size-8" disabled={busy} aria-label="Remove this picture" onClick={() => send(r)}>
                  <X />
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
      <Button variant={changed ? "default" : "ghost"} onClick={() => (changed ? onDone("Chapter pictures saved.") : onBack())}>
        {changed ? "Done" : (<><ArrowLeft /> Back</>)}
      </Button>
    </div>
  )
}

function StyleButton({ active, disabled, onClick, title, note, children }: {
  active: boolean
  disabled: boolean
  onClick: () => void
  title: string
  note?: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 rounded-lg border p-2 text-left text-sm disabled:opacity-60",
        active ? "border-primary ring-2 ring-primary/40" : "hover:bg-accent",
      )}
    >
      {children}
      <span className="min-w-0">
        <span className="block leading-tight">{title}</span>
        {note && <span className="block text-xs leading-tight text-muted-foreground">{note}</span>}
      </span>
    </button>
  )
}

// The default for every card without its own style, and the theme icons' kid-safe switch
export function IconDefaults({ settings, onChange }: { settings: IconSettings; onChange: (s: IconSettings) => void }) {
  const save = async (body: { style?: string; allow_all?: boolean }, msg: string) => {
    try {
      const r = await api<{ style: string; allow_all: boolean }>("/api/icons", { method: "POST", body })
      onChange({ ...settings, ...r })
      toast.success(msg)
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  return (
    <div className="mt-2 flex flex-col gap-2 border-t pt-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <Sparkles className="size-4" /> Pixel icons on cards
        </span>
        <Select value={settings.style} onValueChange={(v) => save({ style: v }, `Cards without their own style: ${label(settings, v)}`)}>
          <SelectTrigger size="sm" className="h-8 max-w-[60%] gap-1 px-2" aria-label="Default pixel icon style">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {settings.styles
              .filter((s) => s.id !== "custom")
              .map((s) => (
                <SelectItem key={s.id} value={s.id}>
                  {s.label}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <ShieldCheck className="size-4" /> Theme icons
        </span>
        <Select
          value={settings.allow_all ? "all" : "safe"}
          onValueChange={(v) => save({ allow_all: v === "all" }, v === "all" ? "Theme icons may be anything" : "Theme icons are kid-safe only")}
        >
          <SelectTrigger size="sm" className="h-8 gap-1 px-2" aria-label="Which theme icons may be used">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="safe">Kid-safe only</SelectItem>
            <SelectItem value="all">Allow every icon</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {settings.ai && <ClaudeKey ai={settings.ai} onChange={(ai) => onChange({ ...settings, ai })} />}
      {!settings.enabled && <p className="text-xs text-muted-foreground">Icons are switched off in config.yaml (chapter_icons).</p>}
    </div>
  )
}

// Your own Claude API key: Claude then draws the AI chapter pictures (billed to your Anthropic account)
function ClaudeKey({ ai, onChange }: { ai: AISettings; onChange: (ai: AISettings) => void }) {
  const [key, setKey] = useState("")
  const [busy, setBusy] = useState(false)
  const c = ai.claude
  const save = async (body: { key?: string; model?: string }, msg: string) => {
    setBusy(true)
    try {
      onChange(await api<AISettings>("/api/ai/claude", { method: "POST", body }))
      setKey("")
      toast.success(msg)
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }
  if (!c.available) return null
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <Bot className="size-4" /> Claude pictures
        </span>
        {c.key_set ? (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            key {c.key_hint}
            {c.from_config ? (
              " (config.yaml)"
            ) : (
              <Button size="sm" variant="ghost" className="h-7 px-2" disabled={busy} onClick={() => save({ key: "" }, "Claude key removed")}>
                Remove
              </Button>
            )}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">off</span>
        )}
      </div>
      {!c.key_set && (
        <div className="flex gap-1.5">
          <Input type="password" autoComplete="off" placeholder="Claude API key (sk-ant-…)" value={key} onChange={(e) => setKey(e.target.value)} className="h-8" />
          <Button size="sm" disabled={busy || !key.trim()} onClick={() => save({ key }, "Claude key saved")}>
            Save
          </Button>
        </div>
      )}
      {c.key_set && !c.from_config && (
        <Select value={c.model} onValueChange={(v) => save({ model: v }, "Claude model changed")}>
          <SelectTrigger size="sm" className="h-8 gap-1 px-2" aria-label="Claude model for pictures">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {c.models.map((m) => (
              <SelectItem key={m.id} value={m.id}>
                {m.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <p className="text-xs text-muted-foreground">
        With your own key from console.anthropic.com, Claude draws the AI chapter pictures (Icon → Pictures per chapter). Each picture is billed to your Anthropic
        account. The key stays on the bridge.
      </p>
    </div>
  )
}
