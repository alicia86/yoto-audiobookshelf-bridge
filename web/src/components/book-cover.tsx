import { BookOpen } from "lucide-react"
import { useState } from "react"

import { cover } from "@/lib/api"
import { cn } from "@/lib/utils"

export function BookCover({ id, className }: { id?: string; className?: string }) {
  const [failed, setFailed] = useState(false)
  return (
    <div className={cn("flex items-center justify-center overflow-hidden rounded-md bg-muted text-muted-foreground", className)}>
      {id && !failed ? (
        <img src={cover(id)} alt="" loading="lazy" className="size-full object-cover" onError={() => setFailed(true)} />
      ) : (
        <BookOpen className="size-1/3" />
      )}
    </div>
  )
}
