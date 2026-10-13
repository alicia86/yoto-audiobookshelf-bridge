import { BookHeadphones, CreditCard, Library as LibraryIcon, ListOrdered } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { Banners } from "@/components/banners"
import { useOverlays } from "@/components/overlays"
import { Players } from "@/components/players"
import { Button } from "@/components/ui/button"
import { chooseWhat, choiceKind, type Picked } from "@/lib/actions"
import { api, errorText } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { Book, Choice, Status } from "@/types"
import { CardsView } from "@/views/cards"
import { ChaptersView } from "@/views/chapters"
import { ContinueView } from "@/views/continue"
import { LibraryView, type Pick } from "@/views/library"

type Tab = "continue" | "library" | "cards" | "chapters"
const TABS: { id: Tab; label: string; icon: typeof LibraryIcon }[] = [
  { id: "continue", label: "Continue", icon: BookHeadphones },
  { id: "library", label: "Library", icon: LibraryIcon },
  { id: "cards", label: "Cards", icon: CreditCard },
  { id: "chapters", label: "Chapters", icon: ListOrdered },
]

export default function App() {
  const overlays = useOverlays()
  const [status, setStatus] = useState<Status | null>(null)
  const [tab, setTab] = useState<Tab>("continue")
  const [pick, setPick] = useState<Pick | null>(null)
  const [cardsKey, setCardsKey] = useState(0) // remount Cards to reload after changes made elsewhere
  const pickResolve = useRef<((p: Picked | null) => void) | null>(null)

  const refresh = useCallback(() => {
    api<Status>("/api/status").then(setStatus, () => {})
  }, [])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const endPick = useCallback((p: Picked | null) => {
    setPick(null)
    pickResolve.current?.(p)
    pickResolve.current = null
  }, [])

  // Send the user to Library to choose a book; resolves with the book, series or collection chosen.
  const pickBook = useCallback(
    (label: string) =>
      new Promise<Picked | null>((resolve) => {
        pickResolve.current?.(null)
        pickResolve.current = resolve
        setPick({
          label,
          onBook: async (b: Book) => {
            setPick(null)
            const p = await chooseWhat(b, overlays)
            setTab("cards")
            const r = pickResolve.current
            pickResolve.current = null
            r?.(p)
          },
          cancel: () => {
            endPick(null)
            setTab("cards")
          },
        })
        setTab("library")
      }),
    [overlays, endPick],
  )

  const goTo = (t: Tab) => {
    if (pick && t !== "library") endPick(null)
    setTab(t)
  }

  const newCard = useCallback(
    async (choice: Choice) => {
      const group = choiceKind(choice)
      overlays.closeSheet()
      try {
        await api("/api/cards", { method: "POST", body: choice })
      } catch (e) {
        toast.error(errorText(e))
        return
      }
      setCardsKey((k) => k + 1)
      overlays.openSheet({
        title: "Card playlist created",
        description: "Last step, in the Yoto app",
        body: (
          <div className="flex flex-col gap-3 pb-2 text-sm">
            <p>
              Make Your Own → find the playlist with this {group || "book"}'s name → ⋮ → Link to a card → put a blank MYO card in a player.
            </p>
            <p className="text-muted-foreground">
              After that the card plays {group ? `the ${group} in order, from whichever book you are on,` : "this book"} from wherever you left off.
            </p>
            <Button
              onClick={() => {
                overlays.closeSheet()
                setTab("cards")
              }}
            >
              OK
            </Button>
          </div>
        ),
      })
    },
    [overlays],
  )

  const devices = status?.devices ?? []
  return (
    <div className="min-h-svh bg-background pb-[calc(4.5rem+env(safe-area-inset-bottom))] text-foreground">
      <header className="sticky top-0 z-10 border-b bg-background/95 px-4 pt-3 pb-2 backdrop-blur supports-[backdrop-filter]:bg-background/80">
        <h1 className="mb-2 text-lg font-semibold tracking-tight">Yoto Bridge</h1>
        <Players devices={devices} refresh={refresh} speeds={status?.speeds} />
      </header>
      {status && <Banners status={status} refresh={refresh} />}
      <main className="mx-auto max-w-3xl px-4 py-3">
        {tab === "continue" && <ContinueView devices={devices} refresh={refresh} />}
        {tab === "library" && <LibraryView devices={devices} refresh={refresh} pick={pick} newCard={newCard} />}
        {tab === "cards" && <CardsView key={cardsKey} pickBook={pickBook} newCard={newCard} reload={() => setCardsKey((k) => k + 1)} />}
        {tab === "chapters" && <ChaptersView />}
      </main>
      <nav className="fixed inset-x-0 bottom-0 z-10 flex border-t bg-background pb-[env(safe-area-inset-bottom)]">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => goTo(id)}
            className={cn(
              "flex flex-1 flex-col items-center gap-1 py-2.5 text-xs text-muted-foreground transition-colors",
              tab === id && "font-medium text-foreground",
            )}
            aria-current={tab === id ? "page" : undefined}
          >
            <Icon className="size-5" />
            {label}
          </button>
        ))}
      </nav>
    </div>
  )
}
