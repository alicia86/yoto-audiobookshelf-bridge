// "Move an old card": the place a player has on an MYO card with uploaded audio is saved to ABS, and a
// new Audiobookshelf series card is made for it. The old playlist is left as it is (yotobridge/transfer.py).
import { CircleCheck, Layers, Loader2, Radio, TriangleAlert } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api, errorText } from "@/lib/api"

type Series = { id: string; name: string; sequence: string; library_id: string }
type Run = {
  item_id: string
  title: string
  duration: number
  series: Series[]
  first_track: number
  last_track: number
  from: number
  to: number
  whole: boolean
  tracks: number
  exact: number
}
type Place = {
  track: number
  track_position: number
  item_id: string | null
  title?: string
  position?: number
  duration?: number
  chapter?: string
  exact?: boolean
  series?: Series[]
  finish?: { item_id: string; title: string }[]
}
type Analysis = {
  card_id: string
  title: string
  tracks: { n: number; title: string; duration: number; item_id?: string; book?: string; start?: number; exact?: boolean }[]
  books: Run[]
  player: { device_id: string; name: string; track: number | null; position: number | null; status: string | null; at: number } | null
  place: Place | null
}

function clock(s: number | null | undefined) {
  const t = Math.max(0, Math.round(s || 0))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const sec = String(t % 60).padStart(2, "0")
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`
}

const span = (a: number, b: number) => (a === b ? `Track ${a}` : `Tracks ${a}–${b}`)

export function TransferSheet(props: { cardId: string; onChanged: () => void; onClose: () => void }) {
  const { cardId, onChanged, onClose } = props
  const [a, setA] = useState<Analysis | null>(null)
  const [error, setError] = useState("")
  const [manual, setManual] = useState(false)
  const [track, setTrack] = useState(1)
  const [minutes, setMinutes] = useState("0")
  const [manualPlace, setManualPlace] = useState<Place | null>(null)
  const [finish, setFinish] = useState<Record<string, boolean>>({})
  const [series, setSeries] = useState<string | null>(null) // null = default (first series), "" = just this book
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<{ title: string; yoto_title: string; existing: boolean } | null>(null)
  const [made, setMade] = useState<string[]>([])

  const load = useCallback(async () => {
    try {
      setA(await api<Analysis>(`/api/transfer/${encodeURIComponent(cardId)}`))
      setError("")
    } catch (e) {
      setError(errorText(e))
    }
  }, [cardId])

  // keep listening for the player until the card has been read
  useEffect(() => {
    load()
  }, [load])
  const waiting = !!a && !a.place && !manual && !done
  useEffect(() => {
    if (!waiting) return
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [waiting, load])

  const place = manual ? manualPlace : a?.place ?? null

  const findManual = async () => {
    const secs = Math.round(parseFloat(minutes.replace(",", ".") || "0") * 60)
    try {
      setManualPlace(await api<Place>(`/api/transfer/${encodeURIComponent(cardId)}/where?track=${track}&position=${secs}`))
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  if (error) return <p className="pb-4 text-sm text-destructive">{error}</p>
  if (!a)
    return (
      <p className="flex items-center gap-2 pb-4 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Reading the card and matching it to Audiobookshelf…
      </p>
    )

  const unknown = a.tracks.filter((t) => !t.item_id).length
  const chosenSeries = series ?? place?.series?.[0]?.id ?? ""
  const finishIds = (place?.finish ?? []).filter((f) => finish[f.item_id] !== false).map((f) => f.item_id)
  const otherSeries = new Map<string, Series>()
  for (const r of a.books) for (const s of r.series) if (s.id !== chosenSeries) otherSeries.set(s.id, s)

  const apply = async () => {
    if (!place?.item_id) return
    setBusy(true)
    try {
      const s = place.series?.find((x) => x.id === chosenSeries)
      const r = await api<{ slot: string; title: string; yoto_title: string; existing: boolean }>(`/api/transfer/${encodeURIComponent(cardId)}/apply`, {
        method: "POST",
        body: {
          item_id: place.item_id,
          position: place.position,
          device_id: manual ? undefined : a.player?.device_id,
          finish: finishIds,
          series_id: s?.id,
          series_name: s?.name,
        },
      })
      setDone(r)
      onChanged()
      toast.success("Place saved")
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const newCard = async (s: Series) => {
    try {
      await api("/api/cards", { method: "POST", body: { series_id: s.id, library_id: s.library_id, series_name: s.name } })
      toast.success(`Playlist “${s.name}” made. Link a spare MYO card to it in the Yoto app.`)
      setMade([...made, s.id])
      onChanged()
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  if (done)
    return (
      <div className="flex flex-col gap-3 pb-4 text-sm">
        <p className="flex items-start gap-2">
          <CircleCheck className="mt-0.5 size-4 shrink-0 text-green-600" />
          {done.existing
            ? `Place saved. Your existing card “${done.yoto_title}” will carry on from it.`
            : `Place saved. New playlist “${done.yoto_title}” made: in the Yoto app, link a spare MYO card to it. It starts where “${a.title}” was left.`}
        </p>
        <p className="text-xs text-muted-foreground">“{a.title}” is unchanged; if it gets played again, its place no longer follows Audiobookshelf.</p>
        {otherSeries.size > 0 && (
          <>
            <p className="text-muted-foreground">Also on the old card. Make a card for each, then link a spare MYO card to it in the Yoto app:</p>
            {[...otherSeries.values()].map((s) => (
              <Button key={s.id} variant="secondary" className="justify-start" disabled={made.includes(s.id)} onClick={() => newCard(s)}>
                {made.includes(s.id) ? <CircleCheck /> : <Layers />} {made.includes(s.id) ? "Made" : "New card"} for {s.name}
              </Button>
            ))}
          </>
        )}
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
    )

  return (
    <div className="flex flex-col gap-4 pb-4 text-sm">
      <section>
        <p className="mb-1 font-medium">On this card ({a.tracks.length} tracks)</p>
        {a.books.length ? (
          <ul className="flex flex-col gap-1">
            {a.books.map((r) => (
              <li key={`${r.item_id}-${r.first_track}`} className="flex flex-wrap items-baseline gap-x-1.5">
                <span className="text-muted-foreground">{span(r.first_track, r.last_track)}:</span>
                <span>{r.title}</span>
                {r.series[0] && (
                  <span className="text-xs text-muted-foreground">
                    ({r.series[0].name}
                    {r.series[0].sequence ? ` #${r.series[0].sequence}` : ""})
                  </span>
                )}
                {!r.whole && (
                  <Badge variant="secondary">
                    {clock(r.from)}–{clock(r.to)}
                  </Badge>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground">None of the tracks matched a book in Audiobookshelf.</p>
        )}
        {unknown > 0 && <p className="mt-1 text-xs text-muted-foreground">{unknown} track(s) not matched to any book.</p>}
      </section>

      <section>
        <p className="mb-1 font-medium">Where the listener is</p>
        {!manual && !a.place && (
          <p className="flex items-start gap-2 text-muted-foreground">
            <Radio className="mt-0.5 size-4 shrink-0 animate-pulse" />
            Put this card in the Yoto it is usually played on. Let it play for a few seconds, then pause it. This page
            picks the spot up by itself.
          </p>
        )}
        {!manual && a.player && (
          <p className="text-xs text-muted-foreground">
            {a.player.name} read track {a.player.track ?? "?"} at {clock(a.player.position)}
            {a.player.status ? ` (${a.player.status})` : ""}.
          </p>
        )}
        {manual && (
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="h-9 max-w-full rounded-md border bg-transparent px-2"
              value={track}
              onChange={(e) => setTrack(Number(e.target.value))}
            >
              {a.tracks.map((t) => (
                <option key={t.n} value={t.n}>
                  {t.n}. {t.title}
                  {t.book ? ` (${t.book})` : ""}
                </option>
              ))}
            </select>
            <Input className="w-20" inputMode="decimal" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
            <span className="text-muted-foreground">min in</span>
            <Button size="sm" variant="secondary" onClick={findManual}>
              Find
            </Button>
          </div>
        )}
        {place &&
          (place.item_id ? (
            <div className="mt-2 rounded-md border p-2">
              <p className="font-medium">
                {place.title} at {clock(place.position)}
              </p>
              <p className="text-muted-foreground">
                {place.chapter ? `${place.chapter} · ` : ""}from track {place.track}, {clock(place.track_position)} in
              </p>
              {!place.exact && (
                <p className="mt-1 flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400">
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0" /> This track's start in the book is estimated from
                  its neighbours, so the spot may be off by a little.
                </p>
              )}
            </div>
          ) : (
            <p className="mt-2 text-destructive">Track {place.track} did not match any book, so its place can't be worked out.</p>
          ))}
        <Button
          size="sm"
          variant="link"
          className="px-0"
          onClick={() => {
            setManual(!manual)
            setManualPlace(null)
          }}
        >
          {manual ? "Use the player's reading instead" : "Choose the track by hand instead"}
        </Button>
      </section>

      {place?.item_id && (
        <>
          {!!place.finish?.length && (
            <section>
              <p className="mb-1 font-medium">Mark as finished (they come earlier on the card)</p>
              {place.finish.map((f) => (
                <label key={f.item_id} className="flex items-center gap-2 py-0.5">
                  <input
                    type="checkbox"
                    className="size-4"
                    checked={finish[f.item_id] !== false}
                    onChange={(e) => setFinish({ ...finish, [f.item_id]: e.target.checked })}
                  />
                  {f.title}
                </label>
              ))}
            </section>
          )}
          <section>
            <p className="mb-1 font-medium">The new card will play</p>
            {(place.series ?? []).map((s) => (
              <label key={s.id} className="flex items-center gap-2 py-0.5">
                <input type="radio" name="series" className="size-4" checked={chosenSeries === s.id} onChange={() => setSeries(s.id)} />
                The whole series “{s.name}”, from this book on
              </label>
            ))}
            <label className="flex items-center gap-2 py-0.5">
              <input type="radio" name="series" className="size-4" checked={chosenSeries === ""} onChange={() => setSeries("")} />
              Just “{place.title}”
            </label>
          </section>
          <Button disabled={busy} onClick={apply}>
            {busy && <Loader2 className="animate-spin" />}
            Save the place and make the new card
          </Button>
          <p className="-mt-2 text-xs text-muted-foreground">The old card and its playlist stay as they are.</p>
        </>
      )}
    </div>
  )
}
