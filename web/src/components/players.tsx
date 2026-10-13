import { Gauge, Pause, Play, RotateCcw, RotateCw, SkipBack, SkipForward, Square } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, errorText, hms, SPEEDS, speedLabel } from "@/lib/api"
import type { Device } from "@/types"

export function Players({ devices, refresh, speeds = SPEEDS }: { devices: Device[]; refresh: () => void; speeds?: number[] }) {
  if (!devices.length) return <p className="text-sm text-muted-foreground">No Yoto players yet</p>
  const ctl = async (action: string, deviceId: string) => {
    try {
      await api("/api/control", { method: "POST", body: { action, device_id: deviceId } })
      setTimeout(refresh, 800)
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  const seek = async (action: string, deviceId: string) => {
    try {
      await api("/api/seek", { method: "POST", body: { action, device_id: deviceId } })
      setTimeout(refresh, 2500)
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  const setSpeed = async (deviceId: string, speed: number) => {
    try {
      await api("/api/speed", { method: "POST", body: { device_id: deviceId, speed } })
      toast.success(`Speed ${speedLabel(speed)}`)
      setTimeout(refresh, 2500)
    } catch (e) {
      toast.error(errorText(e))
    }
  }
  return (
    <div className="-mx-4 flex gap-3 overflow-x-auto px-4 pb-1 [scrollbar-width:none]">
      {devices.map((d) => (
        <Card key={d.id} className="min-w-56 shrink-0 gap-2 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-medium">{d.name}</span>
            {d.online === false ? <Badge variant="secondary">offline</Badge> : d.status === "playing" ? <Badge>playing</Badge> : null}
          </div>
          <p className="max-w-60 truncate text-xs text-muted-foreground">
            {d.now_playing ? `${d.now_playing} · ${hms(d.position)} / ${hms(d.duration)}` : d.status || "idle"}
          </p>
          <div className="flex gap-1.5">
            <Button size="sm" variant="outline" onClick={() => ctl("pause", d.id)} aria-label={`Pause ${d.name}`}>
              <Pause /> Pause
            </Button>
            <Button size="sm" variant="outline" onClick={() => ctl("resume", d.id)} aria-label={`Resume ${d.name}`}>
              <Play /> Resume
            </Button>
            <Button size="icon" variant="outline" className="size-8" onClick={() => ctl("stop", d.id)} aria-label={`Stop ${d.name}`}>
              <Square />
            </Button>
            <Select value={String(d.speed ?? 1)} onValueChange={(v) => setSpeed(d.id, Number(v))}>
              <SelectTrigger size="sm" className="h-8 gap-1 px-2" aria-label={`Speed of ${d.name}`}>
                <Gauge />
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(speeds.includes(d.speed) ? speeds : [...speeds, d.speed].sort((a, b) => a - b)).map((s) => (
                  <SelectItem key={s} value={String(s)}>
                    {speedLabel(s)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {d.playing_speed != null && d.playing_speed !== d.speed && (
            <p className="text-xs text-muted-foreground">This card plays at its own speed, {speedLabel(d.playing_speed)}</p>
          )}
          {d.now_playing && (
            <div className="flex gap-1.5">
              <Button size="icon" variant="ghost" className="size-8" onClick={() => seek("prev", d.id)} aria-label="Previous chapter">
                <SkipBack />
              </Button>
              <Button size="sm" variant="ghost" className="h-8 px-2" onClick={() => seek("back30", d.id)} aria-label="Back 30 seconds">
                <RotateCcw /> 30
              </Button>
              <Button size="sm" variant="ghost" className="h-8 px-2" onClick={() => seek("fwd30", d.id)} aria-label="Forward 30 seconds">
                30 <RotateCw />
              </Button>
              <Button size="icon" variant="ghost" className="size-8" onClick={() => seek("next", d.id)} aria-label="Next chapter">
                <SkipForward />
              </Button>
            </div>
          )}
        </Card>
      ))}
    </div>
  )
}
