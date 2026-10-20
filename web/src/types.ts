export type Device = {
  id: string
  name: string
  online: boolean | null
  status: string | null
  now_playing: string | null
  item_id: string | null
  position: number | null
  duration: number | null
  speed: number // the player's speed (cards without their own speed play at it)
  playing_speed: number | null // speed of what it plays now
}

export type Status = {
  yoto_logged_in: boolean
  yoto_client_id_set: boolean
  login_pending: { url: string; redirect_uri: string; expires_at: number } | null
  mqtt: boolean
  devices: Device[]
  pin_required: boolean
  speeds?: number[]
  resume_mode: string
  error: string
  problems?: string[]
  icon_upload_error?: string
  icons?: IconSettings
}

// Pixel icons on the Yoto's display: the default style, and the styles a card can have
export type IconSettings = {
  styles: { id: string; label: string }[]
  style: string // the default style
  allow_all: boolean // theme icons may be anything, not only kid-safe ones
  enabled: boolean // chapter_icons in config.yaml
  ai?: { enabled: boolean; mode: "library" | "images" } // AI chapter pictures (ai_pictures in config.yaml)
}

// A background job making AI pictures for a card's chapters
export type AIJob = {
  status: "running" | "done" | "failed" | "cancelled"
  done: number
  total: number
  skipped: number
  step: string
  error: string
  mode: "library" | "images"
}

export type Book = {
  id: string
  title: string
  author: string
  duration: number | null
  position?: number
  finished?: boolean
}

export type Group = { id: string; name: string; library_id: string; books: number; kind: "series" | "collection" }

export type BookInfo = Book & {
  series: Omit<Group, "kind">[]
  collections: Omit<Group, "kind">[]
}

export type Offline = {
  status: "building" | "ready" | "failed"
  step?: string
  error?: string
  mode?: "next" | "book" | "series"
  books_left_off?: number
  on_card?: number // while building: tracks already on the card (the rest stream)
  // while building:
  progress?: number // 0 to 1: tracks done plus the part of the one in flight
  done?: number // tracks finished (uploaded and processed by Yoto)
  current?: number // the track in flight, from 1
  phase?: "" | "encoding" | "uploading" | "processing"
  queued?: boolean // waiting for another copy to finish; the card is untouched until then
  queue_position?: number // 1 = next to start
  hours?: number
  speed?: number
  start?: number
  end?: number
  tracks: number
  from_title?: string
  to_title?: string
  last_pos?: number
  last_event_at?: number | null
}

export type CardSlot = {
  slot: string
  item_id: string
  title: string
  yoto_card_id: string
  yoto_title: string
  stream_url: string
  offline: Offline | null
  series_id: string
  series_name: string
  group_kind: "" | "series" | "collection"
  speed: number // 0 = the player's speed
  own_place: boolean // keeps its own place: listening is never written to Audiobookshelf
  icon_style: string // "" = the default style
  icon_custom: boolean // a picture of the owner's own is uploaded
  icon_chapters: number // chapters with a picture of their own
  ai_job?: AIJob | null
}

export type Library = { id: string; name: string }

// What a card plays: one book, a whole series or a whole collection (the /api/cards body)
export type Choice =
  | { item_id: string }
  | { series_id: string; library_id: string; series_name: string }
  | { collection_id: string; library_id: string; collection_name: string }

// Chapters tab (/api/chapters): books without chapter markers and the Audible chapters proposed for them
export type ChapterProposal = {
  chapters: { title: string; start: number; end: number }[]
  runtime: number
  duration: number
  difference: number
  branding_removed: boolean
  dropped: number
  warnings: string[]
  quality: "good" | "check" | "bad"
  note?: string
}

export type ChapterCandidate = {
  asin: string
  title: string
  author: string
  narrator: string
  duration: number
  confidence: number | null
  cover?: string
}

export type ChapterBook = {
  item_id: string
  title: string
  author: string
  duration: number
  num_chapters: number
  reason: "none" | "files" | "own"
  current: { title: string; start: number; end: number }[]
  asin: string
  asin_source: "" | "abs" | "search" | "manual"
  region: string
  candidates: ChapterCandidate[]
  source?: "audible" | "audio"
  audible?: (ChapterCandidate & { url: string }) | null
  proposal: ChapterProposal | null
  status: "queued" | "looking" | "ready" | "matches" | "no_match" | "error" | "skipped" | "applied"
  error: string
}

export type ChapterOverview = {
  scan: { running: boolean; done: number; total: number; started_at: number | null; finished_at: number | null; error: string }
  books: ChapterBook[]
  can_edit: { ok: boolean; user: string; error?: string }
  region: string
  regions: string[]
}
