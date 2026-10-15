import { ArrowRightLeft, CloudDownload, CloudOff, Gauge, Layers, Library as LibraryIcon, Plane, Plus, Recycle, RefreshCw, Trash2, TriangleAlert, Wifi } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { BookCover } from "@/components/book-cover"
import { IconDefaults, IconPreview, IconSheet } from "@/components/icon-styles"
import { useOverlays } from "@/components/overlays"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import type { Picked } from "@/lib/actions"
import { api, errorText, hms, SPEEDS, speedLabel } from "@/lib/api"
import type { CardSlot, Choice, IconSettings, Status } from "@/types"
import { Empty, ListSkeleton } from "@/views/continue"
import { TransferSheet } from "@/views/transfer"

type Playlist = { cardId: string; title: string; bridge?: boolean }

function YotoAccount() {
  const reconnect = async () => {
    try {
      await api("/api/yoto/login", { method: "POST" })
      toast("Sign-in link ready at the top of the page")
      window.scrollTo({ top: 0, behavior: "smooth" })
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  return (
    <div className="mt-2 flex items-center justify-between gap-2 border-t pt-3 text-sm text-muted-foreground">
      <span>Yoto account</span>
      <Button size="sm" variant="ghost" onClick={reconnect}>
        <RefreshCw /> Reconnect Yoto
      </Button>
    </div>
  )
}

export function CardsView(props: {
  pickBook: (label: string) => Promise<Picked | null>
  newCard: (c: Choice) => void
  reload: () => void // remounts this view; actions that finish after a pick may outlive the instance that started them
}) {
  const { pickBook, newCard, reload } = props
  const { openSheet, closeSheet, confirm } = useOverlays()
  const [cards, setCards] = useState<CardSlot[] | null>(null)
  const [error, setError] = useState("")
  const [iconSettings, setIconSettings] = useState<IconSettings | null>(null)
  useEffect(() => {
    api<Status>("/api/status")
      .then((s) => setIconSettings(s.icons ?? null))
      .catch(() => {})
  }, [])
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    try {
      const c = await api<CardSlot[]>("/api/cards")
      setCards(c)
      // keep refreshing while an offline copy is being made
      const building = c.some((x) => x.offline?.status === "building")
      if (building && !timer.current) timer.current = setInterval(load, 4000)
      if (!building && timer.current) {
        clearInterval(timer.current)
        timer.current = null
      }
    } catch (e) {
      setError(errorText(e))
    }
  }, [])
  useEffect(() => {
    load()
    return () => {
      if (timer.current) clearInterval(timer.current)
    }
  }, [load])

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    try {
      await fn()
      if (ok) toast.success(ok)
      reload()
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  const startNewCard = async () => {
    const p = await pickBook("Pick the book (or a book of the series or collection) for the new card")
    if (p) newCard(p.choice)
  }

  const reassign = async (slot: string) => {
    const p = await pickBook("Pick the new book (or a book of the series or collection) for this card")
    if (p) run(() => api(`/api/cards/${slot}`, { method: "PUT", body: p.choice }), `Card now plays ${p.label}`)
  }

  const repurpose = async () => {
    let lists: Playlist[]
    try {
      lists = (await api<Playlist[]>("/api/yoto/playlists")).filter((p) => !p.bridge)
    } catch (e) {
      toast.error(errorText(e))
      return
    }
    openSheet({
      title: "Reuse a MYO card",
      description: "Pick the playlist your card is linked to. Its current audio will be replaced by a stream from Audiobookshelf; the card itself keeps working, no re-linking.",
      body: (
        <div className="flex flex-col gap-2 pb-2">
          {lists.length ? (
            lists.map((p) => (
              <Button key={p.cardId} variant="secondary" className="justify-start" onClick={() => repurposePick(p)}>
                {p.title}
              </Button>
            ))
          ) : (
            <p className="text-sm text-muted-foreground">No MYO playlists found.</p>
          )}
          <Button variant="ghost" onClick={closeSheet}>
            Cancel
          </Button>
        </div>
      ),
    })
  }

  const repurposePick = async (p: Playlist) => {
    closeSheet()
    const picked = await pickBook(`Pick the book (or a book of the series or collection) for “${p.title}”`)
    if (!picked) return
    const ok = await confirm({
      title: `Replace “${p.title}”?`,
      description: `The playlist's current audio is removed from your Yoto account and the card will play “${picked.label}” from Audiobookshelf instead.`,
      action: "Replace and use this card",
      destructive: true,
    })
    if (ok) run(() => api("/api/cards/convert", { method: "POST", body: { ...picked.choice, yoto_card_id: p.cardId, confirm: true } }), "Card ready")
  }

  // An old MYO card with uploaded audio: save the listener's place to ABS, then make it a series card
  const moveOldCard = async () => {
    let lists: Playlist[]
    try {
      lists = (await api<Playlist[]>("/api/yoto/playlists")).filter((p) => !p.bridge)
    } catch (e) {
      toast.error(errorText(e))
      return
    }
    openSheet({
      title: "Move an old card to Audiobookshelf",
      description: "Pick the old card's playlist. Its place is saved to Audiobookshelf and a new card is made; the old one stays as it is.",
      body: (
        <div className="flex flex-col gap-2 pb-2">
          {lists.length ? (
            lists.map((p) => (
              <Button key={p.cardId} variant="secondary" className="justify-start" onClick={() => moveOldCardPick(p)}>
                {p.title}
              </Button>
            ))
          ) : (
            <p className="text-sm text-muted-foreground">No MYO playlists found.</p>
          )}
          <Button variant="ghost" onClick={closeSheet}>
            Cancel
          </Button>
        </div>
      ),
    })
  }

  const moveOldCardPick = (p: Playlist) =>
    openSheet({
      title: `Move “${p.title}”`,
      body: (
        <TransferSheet
          key={p.cardId}
          cardId={p.cardId}
          onChanged={reload}
          onClose={() => {
            closeSheet()
            reload()
          }}
        />
      ),
    })

  const makeOffline = (card: CardSlot) =>
    openSheet({
      title: "Make an offline copy?",
      description:
        "Copy the book onto the card as regular Yoto tracks, so it plays without internet. Until you press “Back to streaming”, the card plays the copy and its position is synced to Audiobookshelf whenever the Yoto is online.",
      body: (
        <OfflineSheet
          speed={card.speed || 1}
          group={card.series_id ? card.group_kind || "series" : ""}
          onCancel={closeSheet}
          onStart={(speed, mode) => {
            closeSheet()
            run(() => api(`/api/cards/${card.slot}/offline`, { method: "POST", body: { speed, mode } }), "Making offline copy…")
          }}
        />
      ),
    })

  const goOnline = async (slot: string) => {
    const ok = await confirm({
      title: "Back to streaming?",
      description: "The card will resume from Audiobookshelf's position on the next tap, and stop working without internet.",
      action: "Back to streaming",
    })
    if (ok) run(() => api(`/api/cards/${slot}/online`, { method: "POST" }), "Card streams again")
  }

  const setSpeed = (slot: string, speed: number) =>
    run(
      () => api(`/api/cards/${slot}/speed`, { method: "PUT", body: { speed } }),
      speed ? `This card now plays at ${speedLabel(speed)}` : "This card now plays at the player's speed",
    )

  const chooseIcon = (card: CardSlot) =>
    iconSettings &&
    openSheet({
      title: "Pixel icon for this card",
      description: "What the Yoto's display shows while this card plays.",
      body: (
        <IconSheet
          key={card.slot}
          card={card}
          settings={iconSettings}
          onDone={(msg) => {
            closeSheet()
            if (msg) toast.success(msg)
            reload()
          }}
        />
      ),
    })

  const remove = async (slot: string) => {
    const ok = await confirm({
      title: "Forget this card?",
      description: "The Yoto playlist stays in your account but stops playing.",
      action: "Forget",
      destructive: true,
    })
    if (ok) run(() => api(`/api/cards/${slot}`, { method: "DELETE" }))
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-2">
        <Button onClick={startNewCard}>
          <Plus /> New card
        </Button>
        <Button variant="outline" onClick={repurpose}>
          <Recycle /> Reuse a MYO card
        </Button>
      </div>
      <Button variant="outline" className="self-start" onClick={moveOldCard}>
        <ArrowRightLeft /> Move an old card (keeps the place)
      </Button>
      {error ? (
        <Empty>{error}</Empty>
      ) : !cards ? (
        <ListSkeleton />
      ) : !cards.length ? (
        <Empty>
          {"No bridge cards yet.\n“New card” makes a Yoto playlist you link to a blank MYO card.\n“Reuse a MYO card” turns a card you already have into a bridge card."}
        </Empty>
      ) : (
        cards.map((c) => (
          <Card key={c.slot} className="flex-row gap-3 p-3">
            <BookCover id={c.item_id || undefined} className="size-16 shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                {c.series_id && (c.group_kind === "collection" ? <LibraryIcon className="size-4 shrink-0" /> : <Layers className="size-4 shrink-0" />)}
                <p className="truncate font-medium">{c.series_id ? c.series_name : c.title || "No book assigned"}</p>
              </div>
              {c.series_id && (
                <p className="truncate text-sm text-muted-foreground">
                  <Badge variant="secondary" className="mr-1">
                    {c.group_kind === "collection" ? "Collection" : "Series"}
                  </Badge>
                  now on: {c.title}
                </p>
              )}
              <p className="truncate text-xs text-muted-foreground">Yoto playlist: {c.yoto_title || c.yoto_card_id}</p>
              <OfflineLine card={c} />
              <div className="mt-2 flex flex-wrap gap-1.5">
                <Button size="sm" variant="outline" onClick={() => reassign(c.slot)}>
                  Change book
                </Button>
                {c.item_id &&
                  (c.offline && c.offline.status !== "failed" ? (
                    <Button size="sm" variant="outline" onClick={() => goOnline(c.slot)}>
                      <Wifi /> {c.offline.status === "building" ? "Cancel offline" : "Back to streaming"}
                    </Button>
                  ) : (
                    <Button size="sm" variant="outline" onClick={() => makeOffline(c)}>
                      <CloudDownload /> Make offline
                    </Button>
                  ))}
                {c.item_id && <CardSpeed speed={c.speed} onChange={(v) => setSpeed(c.slot, v)} />}
                {c.item_id && iconSettings?.enabled && (
                  <Button size="sm" variant="outline" onClick={() => chooseIcon(c)} aria-label="Pixel icon of this card">
                    <IconPreview slot={c.slot} className="size-5 rounded-sm" /> Icon
                  </Button>
                )}
                <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => remove(c.slot)}>
                  <Trash2 /> Forget
                </Button>
              </div>
            </div>
          </Card>
        ))
      )}
      {iconSettings && <IconDefaults settings={iconSettings} onChange={setIconSettings} />}
      <YotoAccount />
    </div>
  )
}

type OfflineMode = "next" | "book" | "series"

function OfflineSheet({
  speed: initial,
  group,
  onStart,
  onCancel,
}: {
  speed: number
  group: "" | "series" | "collection"
  onStart: (s: number, mode: OfflineMode) => void
  onCancel: () => void
}) {
  const [speed, setSpeed] = useState(initial)
  const [mode, setMode] = useState<OfflineMode>("next")
  const options = SPEEDS.includes(initial) ? SPEEDS : [...SPEEDS, initial].sort((a, b) => a - b)
  const bookHours = Math.round(5 * speed * 10) / 10
  const modes: { value: OfflineMode; title: string; text: string }[] = [
    {
      value: "next",
      title: "Next 5 hours",
      text: group
        ? "From where it was left, running on into the next books."
        : "From where it was left, cut at chapters.",
    },
    { value: "book", title: "Whole book", text: "One chapter per track, from the start of the book." },
  ]
  if (group)
    modes.push({
      value: "series",
      title: `Whole ${group}`,
      text: "Every book from book 1 to the end, each track as long as it takes to fit in 99 tracks. The Yoto still starts where the listener is.",
    })
  return (
    <div className="flex flex-col gap-3 pb-2 text-sm">
      <div className="flex flex-col gap-2" role="radiogroup" aria-label="What to copy">
        {modes.map((m) => (
          <button
            key={m.value}
            type="button"
            role="radio"
            aria-checked={mode === m.value}
            onClick={() => setMode(m.value)}
            className={`rounded-md border px-3 py-2 text-left transition-colors ${
              mode === m.value ? "border-primary bg-primary/5" : "hover:bg-muted"
            }`}
          >
            <span className="font-medium">{m.title}</span>
            <span className="block text-muted-foreground">{m.text}</span>
          </button>
        ))}
      </div>
      <div className="flex items-center justify-between gap-3">
        <span>Speed of the copy</span>
        <Select value={String(speed)} onValueChange={(v) => setSpeed(Number(v))}>
          <SelectTrigger size="sm" className="h-8 gap-1 px-2" aria-label="Speed of the offline copy">
            <Gauge />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map((s) => (
              <SelectItem key={s} value={String(s)}>
                {speedLabel(s)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <p className="text-muted-foreground">
        The speed is built into the copy and can't be changed on the Yoto.{" "}
        {mode === "next"
          ? speed !== 1 && `At ${speedLabel(speed)} the 5 hours hold about ${bookHours} hours of the book.`
          : ""}
      </p>
      <p className="text-muted-foreground">
        This uploads that audio to Yoto.{mode !== "next" && " On the first play the Yoto is moved on to where the book was left."}
      </p>
      <Button onClick={() => onStart(speed, mode)}>
        <CloudDownload /> Make offline
      </Button>
      <Button variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
    </div>
  )
}

function CardSpeed({ speed, onChange }: { speed: number; onChange: (v: number) => void }) {
  const options = speed && !SPEEDS.includes(speed) ? [...SPEEDS, speed].sort((a, b) => a - b) : SPEEDS
  return (
    <Select value={String(speed || 0)} onValueChange={(v) => onChange(Number(v))}>
      <SelectTrigger size="sm" className="h-8 gap-1 px-2" aria-label="Playback speed of this card">
        <Gauge />
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="0">Player's speed</SelectItem>
        {options.map((s) => (
          <SelectItem key={s} value={String(s)}>
            {speedLabel(s)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function OfflineLine({ card }: { card: CardSlot }) {
  const o = card.offline
  if (!o) return null
  if (o.status === "building")
    return (
      <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
        <CloudDownload className="size-4 shrink-0 animate-pulse" /> Making offline copy: {o.step}…
        {o.on_card ? ` ${o.on_card} of ${o.tracks} tracks on the card, the rest stream.` : ""}
      </p>
    )
  if (o.status === "failed")
    return (
      <p className="mt-1 flex items-start gap-1.5 text-sm text-destructive">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" /> Offline copy failed: {o.error}. The card still streams.
      </p>
    )
  const span =
    o.mode === "book"
      ? "whole book"
      : o.mode === "series"
        ? `${o.from_title} to the end${o.to_title && o.to_title !== o.from_title ? ` of ${o.to_title}` : ""}${
            o.books_left_off ? ` (${o.books_left_off} more didn't fit)` : ""
          }`
        : o.from_title && o.to_title && o.from_title !== o.to_title
          ? `${o.from_title} ${hms(o.start)} → ${o.to_title} ${hms(o.end)}`
          : `${hms(o.start)} → ${hms(o.end)}`
  return (
    <div className="mt-1 text-sm text-muted-foreground">
      <p className="flex items-center gap-1.5">
        <Plane className="size-4 shrink-0" /> Offline copy: {span}{o.speed && o.speed !== 1 ? ` at ${speedLabel(o.speed)}` : ""} ({o.tracks} tracks)
      </p>
      <p className="flex items-center gap-1.5 text-xs">
        <CloudOff className="size-3.5 shrink-0" /> Play it once on Wi-Fi so the Yoto downloads it.
        {o.last_event_at ? ` Yoto last at ${hms(o.last_pos)}.` : ""}
      </p>
    </div>
  )
}
