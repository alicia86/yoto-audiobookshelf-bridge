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
