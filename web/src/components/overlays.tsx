// App-wide overlays used imperatively from event handlers: a bottom drawer ("sheet"), a confirm
// dialog and the PIN prompt. Each returns a promise or takes content, so views stay simple.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react"

import { Input } from "@/components/ui/input"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle } from "@/components/ui/drawer"
import { onPinRequest } from "@/lib/api"

type SheetContent = { title: ReactNode; description?: ReactNode; body: ReactNode }
type ConfirmOpts = { title: string; description: string; action: string; destructive?: boolean }

type Overlays = {
  openSheet: (c: SheetContent) => void
  closeSheet: () => void
  confirm: (o: ConfirmOpts) => Promise<boolean>
}

const Ctx = createContext<Overlays | null>(null)

export function useOverlays() {
  const v = useContext(Ctx)
  if (!v) throw new Error("useOverlays outside OverlayProvider")
  return v
}

export function OverlayProvider({ children }: { children: ReactNode }) {
  const [sheet, setSheet] = useState<SheetContent | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [confirmOpts, setConfirmOpts] = useState<ConfirmOpts | null>(null)
  const confirmResolve = useRef<(v: boolean) => void>(() => {})
  const [pinOpen, setPinOpen] = useState(false)
  const [pin, setPinValue] = useState("")
  const pinResolve = useRef<(v: string | null) => void>(() => {})

  const openSheet = useCallback((c: SheetContent) => {
    setSheet(c)
    setSheetOpen(true)
  }, [])
  const closeSheet = useCallback(() => setSheetOpen(false), [])
  const confirm = useCallback(
    (o: ConfirmOpts) =>
      new Promise<boolean>((resolve) => {
        confirmResolve.current = resolve
        setConfirmOpts(o)
      }),
    [],
  )

  useEffect(() => {
    onPinRequest(
      () =>
        new Promise<string | null>((resolve) => {
          pinResolve.current = resolve
          setPinValue("")
          setPinOpen(true)
        }),
    )
  }, [])

  const endConfirm = (v: boolean) => {
    confirmResolve.current(v)
    setConfirmOpts(null)
  }
  const endPin = (v: string | null) => {
    pinResolve.current(v)
    setPinOpen(false)
  }

  return (
    <Ctx.Provider value={{ openSheet, closeSheet, confirm }}>
      {children}

      <Drawer open={sheetOpen} onOpenChange={setSheetOpen}>
        <DrawerContent className="max-h-[85vh]">
          {sheet && (
            <div className="mx-auto w-full max-w-lg overflow-y-auto px-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
              <DrawerHeader className="px-0 text-left">
                <DrawerTitle>{sheet.title}</DrawerTitle>
                {sheet.description && <DrawerDescription>{sheet.description}</DrawerDescription>}
              </DrawerHeader>
              {sheet.body}
            </div>
          )}
        </DrawerContent>
      </Drawer>

      <AlertDialog open={!!confirmOpts} onOpenChange={(o) => !o && endConfirm(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirmOpts?.title}</AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-line">{confirmOpts?.description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => endConfirm(false)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className={confirmOpts?.destructive ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
              onClick={() => endConfirm(true)}
            >
              {confirmOpts?.action}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={pinOpen} onOpenChange={(o) => !o && endPin(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Bridge PIN</AlertDialogTitle>
            <AlertDialogDescription>This bridge asks for a PIN before changing cards.</AlertDialogDescription>
          </AlertDialogHeader>
          <Input
            autoFocus
            inputMode="numeric"
            type="password"
            value={pin}
            onChange={(e) => setPinValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && endPin(pin || null)}
          />
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => endPin(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => endPin(pin || null)}>Continue</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Ctx.Provider>
  )
}
