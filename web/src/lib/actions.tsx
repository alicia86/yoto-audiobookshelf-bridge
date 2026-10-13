// Actions shared by several views: play a book on a player, and work out what a card should play
// when a book belongs to a series or collection.
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { api, errorText } from "@/lib/api"
import type { Book, BookInfo, Choice, Group } from "@/types"

export type Picked = { choice: Choice; label: string }

export function groupsOf(info: BookInfo): Group[] {
  return [
    ...(info.series ?? []).map((s) => ({ ...s, kind: "series" as const })),
    ...(info.collections ?? []).map((c) => ({ ...c, kind: "collection" as const })),
  ].filter((g) => g.books > 1)
}

export function groupChoice(g: Group): Choice {
  return g.kind === "collection"
    ? { collection_id: g.id, library_id: g.library_id, collection_name: g.name }
    : { series_id: g.id, library_id: g.library_id, series_name: g.name }
}

export async function playOn(deviceId: string, itemId: string, refresh: () => void) {
  try {
    const r = await api<{ title: string }>("/api/play", { method: "POST", body: { device_id: deviceId, item_id: itemId } })
    toast.success(`Playing ${r.title}`)
    setTimeout(refresh, 3000)
  } catch (e) {
    toast.error(errorText(e))
  }
}

type SheetFns = { openSheet: (c: { title: string; description?: string; body: React.ReactNode }) => void; closeSheet: () => void }

/** One book, or (if it belongs to any) a whole series or collection. Resolves null on cancel. */
export async function chooseWhat(book: Book, sheet: SheetFns): Promise<Picked | null> {
  let groups: Group[] = []
  const busy = toast.loading("Checking for a series or collection…")
  try {
    groups = groupsOf(await api<BookInfo>(`/api/book/${encodeURIComponent(book.id)}`))
  } catch {
    /* no series info: just the book */
  } finally {
    toast.dismiss(busy)
  }
  if (!groups.length) return { choice: { item_id: book.id }, label: book.title }
  return new Promise((resolve) => {
    const done = (p: Picked | null) => {
      sheet.closeSheet()
      resolve(p)
    }
    const what = groups.length > 1 ? "series or collection" : groups[0].kind
    sheet.openSheet({
      title: book.title,
      description: `This book is part of a ${what}. What should the card play?`,
      body: (
        <div className="flex flex-col gap-2 pb-2">
          {groups.map((g) => (
            <Button key={`${g.kind}:${g.id}`} className="h-auto whitespace-normal py-3" onClick={() => done({ choice: groupChoice(g), label: g.name })}>
              The whole {g.kind}: {g.name} ({g.books} books, in order)
            </Button>
          ))}
          <Button variant="secondary" onClick={() => done({ choice: { item_id: book.id }, label: book.title })}>
            Just this book
          </Button>
          <Button variant="ghost" onClick={() => done(null)}>
            Cancel
          </Button>
        </div>
      ),
    })
  })
}

export function choiceKind(c: Choice): "" | "series" | "collection" {
  return "collection_id" in c ? "collection" : "series_id" in c ? "series" : ""
}
