import { Play } from "lucide-react"
import { useEffect, useState } from "react"

import { BookCover } from "@/components/book-cover"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { playOn } from "@/lib/actions"
import { api, errorText, hms } from "@/lib/api"
import type { Book, Device } from "@/types"

export function ContinueView({ devices, refresh }: { devices: Device[]; refresh: () => void }) {
  const [items, setItems] = useState<Book[] | null>(null)
  const [error, setError] = useState("")
  useEffect(() => {
    api<Book[]>("/api/continue").then(setItems, (e) => setError(errorText(e)))
  }, [])
  if (error) return <Empty>{error}</Empty>
  if (!items) return <ListSkeleton />
  if (!items.length) return <Empty>Nothing in progress. Pick a book in Library.</Empty>
  return (
    <div className="flex flex-col gap-3">
      {items.map((b) => (
        <Card key={b.id} className="flex-row items-center gap-3 p-3">
          <BookCover id={b.id} className="size-16 shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium">{b.title}</p>
            <p className="truncate text-sm text-muted-foreground">
              {b.author} · {hms(b.position)} of {hms(b.duration)}
            </p>
            <Progress className="mt-2 h-1.5" value={b.duration ? Math.min(100, (100 * (b.position ?? 0)) / b.duration) : 0} />
            {devices.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {devices.map((d) => (
                  <Button key={d.id} size="sm" onClick={() => playOn(d.id, b.id, refresh)}>
                    <Play /> {d.name}
                  </Button>
                ))}
              </div>
            )}
          </div>
        </Card>
      ))}
    </div>
  )
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-10 text-center text-sm whitespace-pre-line text-muted-foreground">{children}</p>
}

export function ListSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      {[0, 1, 2].map((i) => (
        <Skeleton key={i} className="h-24 w-full rounded-xl" />
      ))}
    </div>
  )
}
