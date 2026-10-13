// Chapters tab: books in ABS without chapter markers, with chapters proposed from Audible (through
// ABS's own lookup). Nothing changes in ABS until a book is applied here.
import { BookOpen, Check, ExternalLink, ListOrdered, Mic, Loader2, RefreshCw, Search, SkipForward, TriangleAlert, Undo2 } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import { BookCover } from "@/components/book-cover"
import { useOverlays } from "@/components/overlays"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, errorText, hms } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { ChapterBook, ChapterOverview } from "@/types"
import { Empty, ListSkeleton } from "@/views/continue"

type Filter = "review" | "nomatch" | "done"
const IN: Record<Filter, ChapterBook["status"][]> = {
  review: ["ready", "queued", "looking", "error"],
  nomatch: ["no_match"],
  done: ["applied", "skipped", "matches"],
}

function clock(s: number) {
  const t = Math.round(s)
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const sec = t % 60
  return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
}

function signed(s: number) {
  const a = Math.abs(Math.round(s))
  const txt = a >= 60 ? `${Math.floor(a / 60)}m ${a % 60}s` : `${a}s`
  return `${s >= 0 ? "+" : "−"}${txt}`
}

const QUALITY = {
  good: { label: "Good match", variant: "default" as const },
  check: { label: "Check", variant: "secondary" as const },
  bad: { label: "Likely wrong edition", variant: "destructive" as const },
}

type Confidence = "all" | "good" | "check" | "bad"
const CONFIDENCE: [Confidence, string][] = [
  ["all", "All"],
  ["good", "Good match"],
  ["check", "Check"],
  ["bad", "Likely wrong edition"],
]
const RANK = { good: 0, check: 1, bad: 2 }
const rank = (b: ChapterBook) => (b.proposal ? RANK[b.proposal.quality] : b.status === "error" ? 3 : 4)

const SOURCE = { abs: "from ABS", search: "found by search", manual: "set by you", "": "" }

export function ChaptersView() {
  const { openSheet, closeSheet, confirm } = useOverlays()
  const [data, setData] = useState<ChapterOverview | null>(null)
  const [error, setError] = useState("")
  const [filter, setFilter] = useState<Filter>("review")
  const [conf, setConf] = useState<Confidence>("all")
  const [retrying, setRetrying] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api<ChapterOverview>("/api/chapters"))
      setError("")
    } catch (e) {
      setError(errorText(e))
    }
  }, [])
  useEffect(() => {
    load()
  }, [load])
  const working = !!data && (data.scan.running || data.books.some((b) => b.status === "looking"))
  useEffect(() => {
    if (!working) return
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [working, load])

  const scan = async (every = false) => {
    if (
      every &&
      !(await confirm({
        title: "Check every book?",
        description:
          "Looks up every book on Audible, including books that have chapters of their own, and lists those whose chapters differ from Audible's. This takes a while on a big library. Books already applied or skipped are left alone.",
        action: "Check every book",
      }))
    )
      return
    try {
      await api("/api/chapters/scan", { method: "POST", body: { every } })
      setFilter("review")
      await load()
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  const apply = async (books: ChapterBook[]) => {
    const many = books.length > 1
    const ok = await confirm({
      title: many ? `Apply chapters to ${books.length} books?` : `Apply ${books[0].proposal?.chapters.length} chapters?`,
      description: many
        ? "Each book gets the Audible chapters shown here, replacing the chapters it has now. Books without an ASIN in ABS also get the ASIN that was found."
        : `${books[0].title} gets the Audible chapters shown here, replacing the chapters it has now.`,
      action: "Apply",
    })
    if (!ok) return
    setBusy(true)
    try {
      const r = await api<{ applied: number; results: { item_id: string; ok: boolean; error?: string }[] }>("/api/chapters/apply", {
        method: "POST",
        body: { item_ids: books.map((b) => b.item_id) },
      })
      const failed = r.results.filter((x) => !x.ok)
      if (r.applied) toast.success(`Chapters applied to ${r.applied} book${r.applied > 1 ? "s" : ""}`)
      if (failed.length) toast.error(failed[0].error || "ABS did not take the chapters")
      closeSheet()
      await load()
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const skip = async (b: ChapterBook, skipped: boolean) => {
    try {
      await api(`/api/chapters/${encodeURIComponent(b.item_id)}/skip`, { method: "POST", body: { skipped } })
      closeSheet()
      await load()
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  const lookup = async (b: ChapterBook, asin: string, region: string) => {
    try {
      const r = await api<ChapterBook>(`/api/chapters/${encodeURIComponent(b.item_id)}/lookup`, {
        method: "POST",
        body: { asin, region },
      })
      closeSheet()
      if (r.status === "ready") toast.success(`Found ${r.proposal?.chapters.length} chapters`)
      else toast.error(r.error || "No chapters found")
      setFilter(r.status === "no_match" ? "nomatch" : "review")
      await load()
    } catch (e) {
      toast.error(errorText(e))
    }
  }

  // one at a time, so a slow Audible doesn't time them all out again
  const retryFailed = async () => {
    setRetrying(true)
    let ok = 0
    for (const b of data?.books.filter((x) => x.status === "error") ?? []) {
      try {
        const r = await api<ChapterBook>(`/api/chapters/${encodeURIComponent(b.item_id)}/lookup`, { method: "POST", body: {} })
        if (r.status === "ready") ok++
      } catch {
        /* stays failed; its own error shows */
      }
      await load()
    }
    setRetrying(false)
    toast(`Found chapters for ${ok} more book${ok === 1 ? "" : "s"}`)
  }

  const showChapters = (b: ChapterBook) => {
    const p = b.proposal!
    openSheet({
      title: b.title,
      description: b.source === "audio" ? `${p.chapters.length} chapters found in the audio` : `${p.chapters.length} chapters from Audible ${b.asin}`,
      body: (
        <div className="flex flex-col gap-3 pb-2">
          <ol className="divide-y rounded-md border text-sm">
            {p.chapters.map((c, i) => (
              <li key={i} className="flex items-baseline gap-3 px-3 py-1.5">
                <span className="w-16 shrink-0 font-mono text-xs text-muted-foreground tabular-nums">{clock(c.start)}</span>
                <span className="min-w-0 flex-1 truncate">{c.title}</span>
                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{hms(c.end - c.start)}</span>
              </li>
            ))}
          </ol>
          {b.current?.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer text-muted-foreground">Chapters now ({b.current.length}{b.reason === "files" ? ", from its files" : ""})</summary>
              <ol className="mt-2 divide-y rounded-md border">
                {b.current.map((c, i) => (
                  <li key={i} className="flex items-baseline gap-3 px-3 py-1.5">
                    <span className="w-16 shrink-0 font-mono text-xs text-muted-foreground tabular-nums">{clock(c.start)}</span>
                    <span className="min-w-0 flex-1 truncate">{c.title}</span>
                    <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{hms(c.end - c.start)}</span>
                  </li>
                ))}
              </ol>
            </details>
          )}
          {b.status !== "applied" && b.status !== "matches" && (
            <Button disabled={busy} onClick={() => apply([b])}>
              <Check /> Apply these chapters
            </Button>
          )}
        </div>
      ),
    })
  }

  const changeMatch = (b: ChapterBook) => {
    openSheet({
      title: "Choose the Audible edition",
      description: `${b.title} · your audio is ${hms(b.duration)}`,
      body: <MatchForm book={b} regions={data?.regions ?? ["us"]} onLookup={(asin, region) => lookup(b, asin, region)} />,
    })
  }

  if (error) return <Empty>{error}</Empty>
  if (!data) return <ListSkeleton />

  const count = (f: Filter) => data.books.filter((b) => IN[f].includes(b.status)).length
  const failed = data.books.filter((b) => b.status === "error")
  const inReview = data.books.filter((b) => IN.review.includes(b.status))
  const confCount = (c: Confidence) => (c === "all" ? inReview.length : inReview.filter((b) => b.proposal?.quality === c).length)
  const shown = data.books
    .filter((b) => IN[filter].includes(b.status))
    .filter((b) => filter !== "review" || conf === "all" || b.proposal?.quality === conf)
    // most confident first, so the easy ones can be cleared before the hard cases
    .sort((a, b) => (filter === "review" ? rank(a) - rank(b) : 0))
  // books with chapters of their own are applied one at a time, never in bulk
  const good = data.books.filter((b) => b.status === "ready" && b.proposal?.quality === "good" && b.reason !== "own")
  const neverScanned = !data.scan.started_at

  return (
    <div className="flex flex-col gap-3">
      <Card className="gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="font-medium">Chapter markers</p>
            <p className="text-sm text-muted-foreground">
              Finds books with no chapters, or with one chapter per file, and proposes Audible's chapters. Nothing changes in Audiobookshelf until you apply a book.
            </p>
          </div>
          <Button size="sm" disabled={data.scan.running} onClick={() => scan(false)}>
            {data.scan.running ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {neverScanned ? "Scan" : "Scan again"}
          </Button>
        </div>
        {data.scan.running && (
          <div className="flex flex-col gap-1.5">
            <Progress value={data.scan.total ? (100 * data.scan.done) / data.scan.total : 0} className="h-1.5" />
            <p className="text-xs text-muted-foreground">
              {data.scan.total ? `Looking up ${data.scan.done + 1} of ${data.scan.total}` : "Listing books to check"}
            </p>
          </div>
        )}
        {!data.scan.running && (
          <Button size="sm" variant="ghost" className="self-start" onClick={() => scan(true)}>
            <Search /> Check every book against Audible
          </Button>
        )}
        {data.scan.error && <p className="text-sm text-destructive">Scan stopped: {data.scan.error}</p>}
      </Card>

      {!data.can_edit.ok && (
        <Alert>
          <TriangleAlert />
          <AlertTitle>Applying will fail with the current ABS key</AlertTitle>
          <AlertDescription>
            {data.can_edit.error ||
              `The ABS user "${data.can_edit.user}" may not edit books. Add an API key of a user with the Update permission as chapters_api_key in config.yaml and restart the bridge. Scanning and reviewing work without it.`}
          </AlertDescription>
        </Alert>
      )}

      {!neverScanned && (
        <div className="flex gap-1.5">
          {(
            [
              ["review", "To review"],
              ["nomatch", "No match"],
              ["done", "Done"],
            ] as [Filter, string][]
          ).map(([f, label]) => (
            <Button key={f} size="sm" variant={filter === f ? "default" : "secondary"} onClick={() => setFilter(f)}>
              {label} ({count(f)})
            </Button>
          ))}
        </div>
      )}

      {filter === "review" && inReview.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {CONFIDENCE.map(([c, label]) => (
            <Badge
              key={c}
              asChild
              variant={conf === c ? "default" : "outline"}
              className="cursor-pointer px-2.5 py-1 text-xs"
            >
              <button onClick={() => setConf(c)}>
                {label} ({confCount(c)})
              </button>
            </Badge>
          ))}
        </div>
      )}

      {filter === "review" && conf === "all" && failed.length > 0 && (
        <Button variant="outline" disabled={retrying} onClick={retryFailed}>
          {retrying ? <Loader2 className="animate-spin" /> : <RefreshCw />} Look up the {failed.length} failed book
          {failed.length > 1 ? "s" : ""} again
        </Button>
      )}

      {filter === "review" && (conf === "all" || conf === "good") && good.length > 1 && (
        <Button variant="outline" disabled={busy || !data.can_edit.ok} onClick={() => apply(good)}>
          <Check /> Apply all {good.length} good matches
        </Button>
      )}

      {neverScanned ? (
        <Empty>Press Scan to find books without real chapters.</Empty>
      ) : !shown.length ? (
        <Empty>
          {filter === "review" ? "Nothing to review." : filter === "nomatch" ? "Every book found a match." : "Nothing applied or skipped yet."}
        </Empty>
      ) : (
        shown.map((b) => (
          <BookRow
            key={b.item_id}
            book={b}
            busy={busy}
            scanning={data.scan.running}
            onApply={() => apply([b])}
            onChapters={() => showChapters(b)}
            onSkip={(s) => skip(b, s)}
            onChange={() => changeMatch(b)}
            onRetry={() => lookup(b, "", b.region)}
          />
        ))
      )}
    </div>
  )
}

function AudibleCover({ src }: { src: string }) {
  const [failed, setFailed] = useState(false)
  return (
    <div className="flex size-16 items-center justify-center overflow-hidden rounded-md bg-muted text-muted-foreground">
      {src && !failed ? (
        <img src={src} alt="" loading="lazy" referrerPolicy="no-referrer" className="size-full object-cover" onError={() => setFailed(true)} />
      ) : (
        <BookOpen className="size-1/3" />
      )}
    </div>
  )
}

function BookRow(props: {
  book: ChapterBook
  busy: boolean
  scanning: boolean
  onApply: () => void
  onChapters: () => void
  onSkip: (skipped: boolean) => void
  onChange: () => void
  onRetry: () => void
}) {
  const { book: b, busy, scanning, onApply, onChapters, onSkip, onChange, onRetry } = props
  const p = b.proposal
  const q = p ? QUALITY[p.quality] : null
  const pending = b.status === "queued" || b.status === "looking"
  return (
    <Card className="gap-2 p-3">
      <div className="flex gap-3">
        <div className="flex shrink-0 gap-1.5">
          <div className="flex flex-col items-center gap-0.5">
            <BookCover id={b.item_id} className="size-16" />
            <span className="text-[10px] text-muted-foreground">Yours</span>
          </div>
          {b.audible && (
            <a href={b.audible.url} target="_blank" rel="noreferrer" className="flex flex-col items-center gap-0.5">
              <AudibleCover src={b.audible.cover ?? ""} />
              <span className="text-[10px] text-muted-foreground">Audible</span>
            </a>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <p className="line-clamp-2 font-medium">{b.title}</p>
          <p className="truncate text-sm text-muted-foreground">
            {b.author}
            {b.reason === "files"
              ? ` · now ${b.num_chapters} chapters = its files`
              : b.reason === "own"
                ? ` · now ${b.num_chapters} chapters of its own`
                : " · no chapters now"}
          </p>
          <div className="mt-1 flex flex-wrap gap-1">
            {q && b.status === "ready" && <Badge variant={q.variant}>{q.label}</Badge>}
            {b.status === "applied" && <Badge variant="outline">Applied</Badge>}
            {b.status === "skipped" && <Badge variant="outline">Skipped</Badge>}
            {b.status === "matches" && <Badge variant="outline">Already right</Badge>}
          </div>
          {p && b.source === "audio" && (
            <p className="flex items-start gap-1.5 text-sm text-muted-foreground">
              <Mic className="mt-0.5 size-3.5 shrink-0" />
              <span>
                {p.chapters.length} chapters found in the audio{p.note ? ` · ${p.note}` : ""}
              </span>
            </p>
          )}
          {p && b.source !== "audio" && (
            <p className="text-sm text-muted-foreground">
              {p.chapters.length} chapters · audio {hms(p.duration)} vs Audible {hms(p.runtime)}{" "}
              <span className={cn("tabular-nums", p.quality !== "good" && "font-medium text-foreground")}>({signed(p.difference)})</span>
            </p>
          )}
          {b.audible && (
            <a href={b.audible.url} target="_blank" rel="noreferrer" className="mt-0.5 flex items-center gap-1 text-sm hover:underline">
              <span className="truncate">
                Audible: {b.audible.title}
                {b.audible.narrator && <span className="text-muted-foreground"> · read by {b.audible.narrator}</span>}
              </span>
              <ExternalLink className="size-3.5 shrink-0 text-muted-foreground" />
            </a>
          )}
          {b.asin && (
            <p className="text-xs text-muted-foreground">
              ASIN {b.asin} {SOURCE[b.asin_source]}
              {p?.branding_removed && " · Audible intro/outro removed"}
            </p>
          )}
        </div>
      </div>

      {pending &&
        (scanning ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> {b.status === "looking" ? "Looking up on Audible" : "Waiting to be looked up"}
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">Not looked up yet: the lookup was interrupted. Press Scan again to carry on.</p>
        ))}
      {b.error && <p className="text-sm text-destructive">{b.error}</p>}
      {p && b.status !== "applied" && p.warnings.length > 0 && (
        <ul className="flex flex-col gap-1 text-sm">
          {p.warnings.map((w) => (
            <li key={w} className="flex gap-1.5">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
              <span>{w}</span>
            </li>
          ))}
        </ul>
      )}

      {!pending && (
        <div className="flex flex-wrap gap-1.5">
          {(b.status === "ready" || (b.status === "error" && p)) && (
            <Button size="sm" disabled={busy} onClick={onApply}>
              <Check /> Apply
            </Button>
          )}
          {p && (
            <Button size="sm" variant="secondary" onClick={onChapters}>
              <ListOrdered /> Chapters
            </Button>
          )}
          {(b.status === "error" || b.status === "no_match") && (
            <Button size="sm" variant="secondary" onClick={onRetry}>
              <RefreshCw /> Try again
            </Button>
          )}
          {b.status !== "applied" && b.status !== "skipped" && b.status !== "matches" && (
            <Button size="sm" variant="secondary" onClick={onChange}>
              <Search /> {p ? "Other edition" : "Find edition"}
            </Button>
          )}
          {b.status === "skipped" ? (
            <Button size="sm" variant="ghost" onClick={() => onSkip(false)}>
              <Undo2 /> Undo skip
            </Button>
          ) : (
            b.status !== "applied" &&
            b.status !== "matches" && (
              <Button size="sm" variant="ghost" onClick={() => onSkip(true)}>
                <SkipForward /> Skip
              </Button>
            )
          )}
        </div>
      )}
    </Card>
  )
}

function MatchForm(props: { book: ChapterBook; regions: string[]; onLookup: (asin: string, region: string) => Promise<void> }) {
  const { book: b, regions, onLookup } = props
  const [asin, setAsin] = useState("")
  const [region, setRegion] = useState(b.region)
  const [busy, setBusy] = useState(false)
  const go = async (a: string) => {
    setBusy(true)
    await onLookup(a, region)
    setBusy(false)
  }
  const valid = /^[A-Za-z0-9]{10}$/.test(asin.trim())
  return (
    <div className="flex flex-col gap-3 pb-2">
      {b.candidates.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <p className="text-sm font-medium">Audible matches</p>
          {b.candidates.map((c) => (
            <Button
              key={c.asin}
              variant={c.asin === b.asin ? "default" : "secondary"}
              className="h-auto justify-start py-2 text-left whitespace-normal"
              disabled={busy}
              onClick={() => go(c.asin)}
            >
              {c.cover && <img src={c.cover} alt="" referrerPolicy="no-referrer" className="size-12 shrink-0 rounded object-cover" />}
              <span className="flex min-w-0 flex-col">
                <span className="font-medium">{c.title}</span>
                <span className="text-xs opacity-80">
                  {[c.author, c.narrator && `read by ${c.narrator}`, c.duration ? hms(c.duration) : "", c.asin].filter(Boolean).join(" · ")}
                </span>
              </span>
            </Button>
          ))}
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <p className="text-sm font-medium">{b.candidates.length ? "Or type an ASIN" : "Type an ASIN"}</p>
        <p className="text-xs text-muted-foreground">It is in the book's Audible web address, like audible.com/pd/…/B002V5D1CG.</p>
        <div className="flex gap-2">
          <Input value={asin} placeholder="B002V5D1CG" onChange={(e) => setAsin(e.target.value)} className="font-mono uppercase" />
          <Select value={region} onValueChange={setRegion}>
            <SelectTrigger className="w-20">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {regions.map((r) => (
                <SelectItem key={r} value={r}>
                  {r.toUpperCase()}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button disabled={!valid || busy} onClick={() => go(asin.trim().toUpperCase())}>
          {busy ? <Loader2 className="animate-spin" /> : <Search />} Look up chapters
        </Button>
      </div>
    </div>
  )
}
