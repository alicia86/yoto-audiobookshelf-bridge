import { Layers, Loader2, Play, Plus, Search } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { BookCover } from "@/components/book-cover"
import { useOverlays } from "@/components/overlays"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { groupChoice, groupsOf, playOn } from "@/lib/actions"
import { api, errorText, hms } from "@/lib/api"
import type { Book, BookInfo, Choice, Device, Group, Library } from "@/types"
import { Empty } from "@/views/continue"

export type Pick = { label: string; onBook: (b: Book) => void; cancel: () => void }

export function LibraryView(props: {
  devices: Device[]
  refresh: () => void
  pick: Pick | null
  newCard: (c: Choice) => void
}) {
  const { devices, refresh, pick, newCard } = props
  const { openSheet, closeSheet } = useOverlays()
  const [libs, setLibs] = useState<Library[] | null>(null)
  const [lib, setLib] = useState<string>("")
  const [query, setQuery] = useState("")
  const [debounced, setDebounced] = useState("")
  const [books, setBooks] = useState<Book[] | null>(null)
  const [error, setError] = useState("")
  const [searchError, setSearchError] = useState("")
  const [slow, setSlow] = useState(false)

  useEffect(() => {
    api<Library[]>("/api/libraries").then(
      (l) => {
        setLibs(l)
        setLib((cur) => cur || l[0]?.id || "")
      },
      (e) => setError(errorText(e)),
    )
  }, [])
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 300)
    return () => clearTimeout(t)
  }, [query])
  useEffect(() => {
    if (!lib) return
    let stale = false
    setBooks(null)
    setSearchError("")
    setSlow(false)
    const slowTimer = setTimeout(() => !stale && setSlow(true), 5000)
    api<{ items: Book[] }>(`/api/books?library=${encodeURIComponent(lib)}&q=${encodeURIComponent(debounced)}`).then(
      (r) => !stale && setBooks(r.items),
      // A failed search stays below the search box, so the next search can still replace it.
      (e) => {
        if (stale) return
        setBooks([])
        setSearchError(errorText(e) || "Search failed, try again")
      },
    ).finally(() => clearTimeout(slowTimer))
    return () => {
      stale = true
      clearTimeout(slowTimer)
    }
  }, [lib, debounced])

  const bookSheet = (b: Book) => {
    if (pick) return pick.onBook(b)
    openSheet({
      title: b.title,
      description: `${b.author}${b.duration ? " · " + hms(b.duration) : ""}`,
      body: <BookActions book={b} devices={devices} refresh={refresh} newCard={newCard} close={closeSheet} />,
    })
  }

  const grid = useMemo(() => {
    if (!books) return Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="aspect-square w-full rounded-lg" />)
    return books.map((b) => (
      <button key={b.id} className="text-left" onClick={() => bookSheet(b)}>
        <BookCover id={b.id} className="aspect-square w-full rounded-lg" />
        <span className="mt-1.5 line-clamp-2 text-sm">{b.title}</span>
      </button>
    ))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [books, pick, devices])

  if (error) return <Empty>{error}</Empty>
  if (libs && !libs.length) return <Empty>No book libraries visible to this ABS user</Empty>
  return (
    <div className="flex flex-col gap-3">
      {pick && (
        <Alert>
          <AlertDescription className="flex flex-row items-center justify-between gap-2">
            <span>{pick.label}</span>
            <Button size="sm" variant="outline" onClick={pick.cancel}>
              Cancel
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="flex gap-2">
        {libs && libs.length > 1 && (
          <Select value={lib} onValueChange={setLib}>
            <SelectTrigger className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {libs.map((l) => (
                <SelectItem key={l.id} value={l.id}>
                  {l.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input type="search" placeholder="Search books" className="pl-8" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      </div>
      {!books && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          {slow ? "Audiobookshelf is slow to answer, still waiting…" : debounced ? "Searching…" : "Loading books…"}
        </p>
      )}
      {searchError ? <Empty>{searchError}</Empty> : books && !books.length ? <Empty>No books found</Empty> : <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-3">{grid}</div>}
    </div>
  )
}

function BookActions(props: { book: Book; devices: Device[]; refresh: () => void; newCard: (c: Choice) => void; close: () => void }) {
  const { book, devices, refresh, newCard, close } = props
  const [groups, setGroups] = useState<Group[]>([])
  useEffect(() => {
    api<BookInfo>(`/api/book/${encodeURIComponent(book.id)}`).then((i) => setGroups(groupsOf(i)), () => {})
  }, [book.id])
  return (
    <div className="flex flex-col gap-2 pb-2">
      {devices.map((d) => (
        <Button
          key={d.id}
          onClick={() => {
            close()
            playOn(d.id, book.id, refresh)
          }}
        >
          <Play /> Play on {d.name}
        </Button>
      ))}
      <Button variant="secondary" onClick={() => newCard({ item_id: book.id })}>
        <Plus /> Make a new card for this book
      </Button>
      {groups.map((g) => (
        <Button key={`${g.kind}:${g.id}`} variant="secondary" className="h-auto whitespace-normal py-2.5" onClick={() => newCard(groupChoice(g))}>
          <Layers /> Make a card for the whole {g.kind}: {g.name} ({g.books} books)
        </Button>
      ))}
      <Button variant="ghost" onClick={close}>
        Close
      </Button>
    </div>
  )
}
