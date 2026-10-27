# Yoto ↔ Audiobookshelf bridge

A small service that runs next to Audiobookshelf (ABS) in Docker and makes your Yoto players play ABS books, with ABS as the only record of progress.

- **Cards.** A MYO card points at an ABS book. Tap it and the Yoto plays from wherever that book was left, whether on the Yoto or on the ABS phone app. Pull it out and the position is saved in ABS.
- **Phone.** A web page (save it to the home screen) lists "Continue listening" and the whole library. "▶ <player>" plays any book on that Yoto, no card needed, from the ABS position.
- **Audio comes from your server.** The players fetch the audio from the bridge, on your LAN or through a public HTTPS address that serves only the audio ([players on other networks](#publishing-s-for-players-on-other-networks)). Nothing is uploaded to Yoto. Yoto's cloud is still used to look up which playlist a card belongs to and to carry play/pause commands and position reports (MQTT).

Status: early. Tested against fake ABS and fake Yoto servers (real ffmpeg audio) and connected to real Yoto players; the behaviours that only a real player can confirm are isolated behind config switches, see [Unverified points](#unverified-points-and-what-each-test-result-changes).


**Unofficial.** Not affiliated with or endorsed by Yoto. Works with Yoto via the official [developer API](https://yoto.dev). The repo ships no Yoto client id: each user registers their own app at [dashboard.yoto.dev](https://dashboard.yoto.dev) and accepts Yoto's [API Terms](https://yoto.dev/reference/terms-and-conditions/). Because the bridge uses the `family:devices:control` scope (to start and pause playback), Yoto will not verify the app, so its sign-in page shows an "unverified app" warning that you accept once. Yoto says such apps can still be used personally and shared.

**Your data.** Everything the bridge keeps is in `data/` on your own host: `config.yaml` (your ABS key and Yoto client id), `yoto_tokens.json` (your Yoto login, file mode 600) `state.json` (which card plays which book), `card_progress.json` (the place of cards that keep their own place instead of syncing with ABS) and `quiet.json` (the pauses found near track cuts, per book). It talks only to your Audiobookshelf server and to Yoto's API, login and MQTT services, plus the ntfy server you choose if you turn on alerts (the alert text names the problem and, for a failed card, the book); there is no telemetry. It uses the parent's Yoto account and ABS user and collects nothing about children. Only stream audio you have the right to use.

---

## How it works

```
 MYO card ──tap──▶ Yoto ──(cloud: which playlist?)──▶ playlist track = http://<bridge>/s/<slot>.mp3
                    │
                    └──── HTTP GET (LAN) ──▶ bridge ──asks ABS "where is this book?"──▶ ABS
                                               │   opens an ABS playback session
                                               │   ffmpeg: ABS files from that second → MP3
                    ◀──── audio ───────────────┘
 Yoto ──MQTT events (position, paused)──▶ Yoto cloud ──▶ bridge ──every 15 s──▶ ABS session sync
```

Three design choices carry most of the weight:

1. **Server-side resume.** The Yoto playlists the bridge makes hold stream tracks whose URLs only change when the book does (one track per chapter, see [below](#chapters-as-tracks-and-skipping)). When a player requests one on a tap, the bridge asks ABS for the current position and starts the audio there. Resume therefore works on a plain card tap and does not depend on the Yoto seeking inside a stream, which it cannot do. Every tap re-reads ABS, so a position you reached on the phone wins.
2. **The slot decides the book, not the Yoto playlist.** Which book a card plays is stored in the bridge. Re-pointing a card at another book is a tap on the phone page; no Yoto app, no re-linking. The same trick gives each player a "remote" playlist used by "▶ <player>" on the phone page: the bridge sets the book, then tells the player over MQTT to start that playlist.
3. **Two independent position sources.** The Yoto's MQTT `position` is used when it arrives. It is always capped by what the bridge actually delivered (constant-bitrate MP3, so bytes map exactly to seconds) and by the wall-clock time since the stream started. If MQTT positions never arrive, or arrive in an unexpected form, the bridge falls back to "delivered audio minus the player's buffer", which errs toward replaying a few seconds rather than skipping any.

Progress is written through real ABS playback sessions (`/api/items/{id}/play`, `/api/session/{id}/sync`, `/close`), so the Yoto shows up in ABS listening sessions and stats as "Yoto bridge", and "Continue listening" ordering updates. If the phone has moved the book after the Yoto stopped, the bridge's final write is skipped so it never overwrites the newer position.

---

## Setup

You need: an always-on machine with Docker (the ABS host, or any home server), an ABS API key or user API token, and a Yoto developer client id.

1. **ABS key.** Use a key belonging to the user whose progress the Yoto should share: the account logged in on the phone you want to hand off with. On ABS 2.26 or newer, an admin creates one on the **API Keys** tab of Settings → Users (a tab on that page, not inside a user). On older ABS, open that user under Settings → Users and copy its **API Token**; it works the same way, but newer ABS treats these tokens as legacy, so switch to an API key after upgrading. Paste it straight into the config file; it is effectively that user's password. (For a child's own ABS user, or one per player, see [below](#a-childs-own-abs-user-and-one-per-child).)
2. **Yoto client id.** Sign in at [dashboard.yoto.dev](https://dashboard.yoto.dev) with the Yoto account your players are on, create an app as a **Public Client** with the scopes:
   - `user:content:manage`
   - `family:devices:view`
   - `family:devices:control`
   - `family:library:view` (for browsing family library cards and groups)
   - `family:library:manage` (for organizing and updating cards in family groups)
   - `user:icons:manage` (for chapter and track display icon uploads)
   - `offline_access`

   Copy the client id (there is no secret). 
   
   Under **Allowed Callback URLs** add `<public_url>/s/yoto-callback`, for example `https://yoto.example.com/s/yoto-callback` or `http://127.0.0.1:8090/callback` (see [Choosing `public_url`](#choosing-public_url)). The bridge signs in with the browser login (authorization code + PKCE); Yoto refuses the older device-code login for new apps.
3. **Config.** Copy this repo to the Docker host (without `.git`; `git archive HEAD | ssh <user>@<host> 'mkdir -p <dir> && tar -xf - -C <dir>'` works and never touches `data/`), then on the host:
   ```sh
   mkdir data && cp config.example.yaml data/config.yaml
   # edit data/config.yaml: abs_url, abs_api_key, public_url (see below), yoto_client_id
   docker compose up -d --build
   docker compose logs -f
   ```
   Don't copy the whole folder (`scp -r *`): `web/node_modules` alone is over 200 MB in thousands of files. On a host with a container manager GUI you can instead create a compose project from that folder. For code updates, prefer `docker compose up -d --build` over SSH: some GUIs reuse the previously built image and keep running the old code.
   The log should say `ABS reachable … book libraries visible`.
4. **Connect Yoto.** Open `http://<host-ip>:8090` on your phone (the host's LAN address), press **Connect Yoto**, then **Open the Yoto sign-in page**, sign in and allow access. Yoto sends you to a "Yoto connected" page and the bridge page updates by itself. (If `yoto_redirect_uri` is a `127.0.0.1` address instead, the page Yoto sends you to will not load: copy its address into the box on the bridge page and press **Finish**.) The log then lists your players (`Yoto player y2…: <name>`) and `MQTT connected for N player(s)`. The bridge also creates one playlist per player, named "Audiobookshelf on <player name>"; leave those alone, they are what "▶ <player>" on the phone page uses.
5. **Save the page to the home screen** (Chrome menu → Add to Home screen).

### Choosing `public_url`

Each card's Yoto playlist holds the bridge's track URLs, `<public_url>/s/…`, and every player uses the same URLs. Yoto players cannot use a VPN, so `public_url` must be reachable from wherever your players are.

- **Players on the same network as the bridge:** `public_url: http://<host-ip>:8090`, the Docker host's LAN IP (not localhost, not a `.local` name). The Yoto login callback then also points at a LAN address; if Yoto's dashboard refuses a plain `http://` callback, set `yoto_redirect_uri: http://127.0.0.1:8787/callback` and use the paste-the-address fallback described in step 4.
- **Players on other networks** (for example on a phone hotspot away from home): see below.

### Publishing `/s/` for players on other networks

Publish the bridge's `/s/` path, and only that path, at a public HTTPS address. Any way of exposing one path over HTTPS works: a reverse proxy with a certificate (Caddy, nginx, Traefik) on a port you forward, or a tunnel service that can forward a path to `http://<host>:8090/s`. Then:

- set `public_url` to that address, e.g. `https://yoto.example.com` (with the port if it isn't 443, no trailing path), and restart the bridge: it rewrites every Yoto playlist to the new address by itself;
- add `<public_url>/s/yoto-callback` as the Yoto app's callback URL.

Notes:

- `/s/` carries the audio, the chapter icons, the Yoto login callback and a health check. The phone page and `/api/…` can change cards and browse the library, so keep them on your own network (or reach them over your VPN).
- The public hostname is not a secret. What protects the streams is that slot ids are random 72-bit tokens, and that nothing but `/s/` is published.
- Behind a proxy or tunnel every request comes from the same address, so `devices: … ip:` overrides do not apply; players are recognised by their MQTT events instead. The default single-ABS-user setup is unaffected.
- The audio then travels over the internet to the player: about 30 MB per hour of listening at the default 64 kbps.

The phone page is at `http://<host-ip>:8090` on your network.

### Making cards

- **Reuse a MYO card you already have:** Cards → *Reuse a MYO card* → pick the playlist that card is linked to → pick the book → confirm. The playlist's existing audio is replaced by the bridge stream; the physical card keeps working with no re-linking.
- **New card:** Cards → *New card* (or a book → *Make a new card*) → then in the Yoto app: Make Your Own → the playlist with the book's name → ⋮ → Link to a card → put a blank MYO card in a player. Linking a physical card is only possible in the Yoto app.
- **Change which book a card plays:** Cards → *Change book*. Instant; the Yoto playlist's title and cover are updated too.

- **Move an old card with uploaded audio, keeping the listener's place:** Cards → *Move an old card* → pick its playlist. The bridge reads the card's tracks and matches them to ABS books by length (against each book's file and chapter boundaries, chained track to track), so it shows which tracks are which book. Put the card in the Yoto it is usually played on, let it play a few seconds and pause: the player reports the track and position, and the page shows the book, time and chapter (or choose the track and minute by hand). *Save the place and make the new card* writes that position to ABS (books earlier on the card are marked finished, untick any that weren't) and makes a new series-card playlist (or reuses the bridge card that already plays that series); link a spare MYO card to it in the Yoto app. The old playlist is never changed. Other series that were on the same card get a *New card for …* button.

### Daily use

- Tap the card: plays from the ABS position (5 s rewind). Pull it: position saved within about a minute.
- On the phone, ABS app: the book shows the Yoto's position. Listen, pause.
- Back on the Yoto: tap the card again and it continues from the phone's position.
- No card for this book? Bridge page → Library → book → **Play on <player>**.
- "Move what I'm listening to onto the Yoto": pause in the ABS app, open the bridge page, the book is first under Continue, tap **▶ <player>**.

### Chapter icons on the Yoto's display

The Yoto's pixel display shows a 16x16 icon per track. The bridge draws one from the book's cover (shrunk to 16x16) with the current chapter number in tiny 3x4 digits in the top-right corner, tinted into the cover (light on dark covers, dark on light ones); books without chapters get just the mini cover. `chapter_icons: false` turns this off.

That is the default style. **Cards → Pixel icons on cards** sets another default, and each card's **Icon** button gives that card its own style (the card's style wins, like speed), with a preview of each:

| Style | What the display shows |
|---|---|
| Cover, number top right / top left / bottom left / bottom right | the cover with the tiny chapter number in that corner |
| Big number on a dimmed cover | the cover at 40% with the number in doubled 3x5 digits |
| Cover only | just the cover |
| Number only | the number, big, on a plain colour taken from the cover |
| Cover with a progress bar | the cover with a bar along the bottom row: how far into the book the chapter starts |
| Chapter theme icons | an icon from Yoto's public icon library whose title or tags match words of the chapter title; chapters like "Chapter 7" fall back to the default style. Kid-safe only unless **Theme icons → Allow every icon**: icons naming weapons, gore and the like are never picked |
| Random pixel creatures | a mirrored 8x8 creature in the cover's colours, different per chapter, always the same for that chapter |
| Custom picture | a picture you upload for that card, shrunk to 16x16; **Pictures per chapter** gives single chapters of the current book a picture of their own (chapters without one show the card's picture, or the default style) |
| No icon | the Yoto's own default icon |

A style change rewrites the card's Yoto playlist with new icon URLs (so the player fetches new icons); a card that is playing gets them when it stops, and an offline copy on its next copy. Theme icons on offline copies use Yoto's own icon (nothing uploaded).

### AI chapter pictures

**Pictures per chapter → Make AI pictures** listens to the start of each chapter of the card's book (the first `sample_seconds`, 3 minutes by default) and makes it a picture, saved as that chapter's own picture (so it can still be changed by hand). Chapters that already have a picture are kept unless you press **Redo all**. It runs in the background, one chapter at a time; when a service cannot be reached (a computer asleep) the chapter waits up to `retry_minutes` (60) and is tried again.

The bridge does none of the AI itself. It calls three services in the OpenAI API format, set under `ai_pictures` in config.yaml (see `config.example.yaml`); each has its own `url` (ending in `/v1`), `model` and optional `key`, so they can run on a computer at home, at any hosted provider, or a mix:

| Step | Request | Runs on, for example |
|---|---|---|
| `transcribe` | `POST <url>/audio/transcriptions` (16 kHz WAV) | whisper.cpp's `whisper-server --inference-path /v1/audio/transcriptions`, OpenAI, Groq |
| `text` | `POST <url>/chat/completions` | Ollama, LM Studio, OpenAI, OpenRouter |
| `image` (optional) | `POST <url>/images/generations`, or with `api: ollama` Ollama's `POST <url>/api/generate` | LocalAI, OpenAI; Ollama's image models (macOS) |

- **Without `image`**: the text model names a few things the chapter is about, and picks the best of the matching icons from Yoto's own icon library (drawn for 16x16; the theme icons' kid-safe filter applies). Small: a 3B text model and a small Whisper model fit next to everything else on a 16 GB computer.
- **With `image`**: the text model describes one simple object, which is drawn as a flat pixel-art icon and shrunk to 16x16. Image models need a lot of memory; on 16 GB set `unload: true` on every Ollama step so each model leaves memory after its step.
- Kid-safe: the text model is told to keep to kid-safe things, and a subject naming a weapon or anything gory is dropped (the chapter is left as it was), unless **Theme icons → Allow every icon**.
- What leaves the bridge: up to `sample_seconds` of each chapter's audio goes to the transcribe service, and its text to the text service. Keep both at home if that matters to you.

#### Claude draws the pictures (your own Anthropic API key)

Optional: instead of the services above, Claude can draw each picture itself. Claude makes no images; it chooses every one of the 256 pixels and answers with a small palette and 16 rows of palette numbers, which the bridge turns into the icon. That suits a 16x16 screen better than shrinking a big picture.

- **Set it up**: create an API key at console.anthropic.com (the pictures are billed to that account's API credits; a claude.ai subscription does not cover the API), then on the Cards tab paste it under **Claude pictures** and press **Save**. The page shows only its last four characters; the key is kept in `data/claude.json` (readable by the bridge only) and is never sent back to the page. **Remove** forgets it. Or set it in config.yaml under `ai_pictures.claude` (then the page cannot change it).
- **Model**: Claude Opus 5.5 (default, best drawings), Sonnet 5.5 (half the price), Haiku 5.5 (far cheaper, simpler drawings). A rough guide, not a measured bill: Opus 5.5 costs a few US cents per picture, about a dollar for a 30-chapter book; Haiku 5.5 a small fraction of a cent.
- **Use it**: **Icon → Pictures per chapter → Make AI pictures** draws every chapter of the card's current book; **Card picture** draws one picture for the whole card (saved as its custom picture).
- **What Claude gets**: the book's title, author, series and description from Audiobookshelf, and the chapter's number and title. Claude draws from what it knows about the book; chapters only called "Chapter 7" of a little-known book get a picture of the book as a whole. With a `transcribe` service also set, the opening of each chapter's text is added, which helps those chapters.
- Kid-safe as above: Claude is told to avoid weapons and scary things, a drawing it names as one is dropped, and a request Claude declines is skipped.
- When Claude is overloaded or rate limited, the chapter waits and is tried again, like a sleeping computer above. A wrong key stops the job with a message on the card.


- **Streaming cards** point their track at `<public_url>/s/icon/<slot>.png` (Yoto "dynamic icons", `display.iconUrl16x16`), which the bridge draws on request for the chapter being listened to, or the chapter ABS has the book at when the card is idle. Existing cards get the icon on the next bridge start. Whether the player re-fetches the icon while a track plays (so the number moves on mid-stream) is not documented; see [Unverified points](#unverified-points-and-what-each-test-result-changes).
- **Offline copies** upload one icon per chapter (`display.icon16x16`), so each chapter track shows its number with no internet. This needs the **`user:icons:manage`** scope on your Yoto app (dashboard.yoto.dev → your app → Scopes), then **Connect Yoto** again on the bridge page so the new permission is granted. Without it, offline copies are made as before, just without icons, and the log says why. Icons are uploaded once per book chapter and reused.

### Per-Item Chapter Artwork Cache & External Broker Integration

The bridge caches 16x16 chapter icons on a **per-item basis** in `data/icons/items/<item_id>/<chapter_num>.rgba` (and tracks them in `state.json` under `item_icons`).

- **Universal inheritance:** When artwork is curated for an Audiobookshelf book item, any card that plays that book—whether a dedicated MYO card, a reused card, an offline copy, or a card stepping through a series—automatically inherits the curated chapter icons without needing to re-upload or customize each slot separately.
- **Precedence hierarchy:**
  1. Slot-level custom chapter icon (`data/icons/<slot>/<item_id>-<n>.rgba`)
  2. Item-level custom chapter icon (`data/icons/items/<item_id>/<n>.rgba`)
  3. Card-level custom picture
  4. Selected artwork style (Cover, Progress bar, Theme icons, Creatures, or Default)
- **External Broker REST Endpoints:** External services (such as the Yoto ESP32 Middleware Broker) or scripts can manage the item-level cache directly using an API key (`X-Api-Key` or Bearer token):
  - `GET /api/items/{item_id}/icon/chapters/{n}`: Returns the 16x16 PNG for chapter `n` (1-indexed).
  - `PUT /api/items/{item_id}/icon/chapters/{n}`: Uploads a 16x16 PNG (accepts raw image bytes with `Content-Type: image/png` or JSON `{"image_b64": "<base64>"}`).
  - `DELETE /api/items/{item_id}/icon/chapters/{n}`: Removes the custom chapter icon.
  - `POST /api/items/{item_id}/icon/chapters/batch`: Uploads icons for multiple chapters in a single atomic call:
    ```json
    {
      "tracks": {
        "1": "<base64-png>",
        "2": "<base64-png>"
      }
    }
    ```


### Series and collection cards

A card can follow a whole ABS series or collection instead of one book. When you pick a book for a card (New card, Reuse a MYO card, Change book) and that book belongs to a series or a collection, the page asks **The whole series: …**, **The whole collection: …** or **Just this book**; a book's sheet in the Library also offers **Make a card for the whole series/collection**.

- Books play in series order (ABS sequence, numeric: 2 before 10), or in the collection's own order as arranged in ABS.
- Which book a tap plays is decided from ABS at that moment: the series book with the most recent listening activity, or the next one if that book is finished; with no activity yet, book 1. So listening to book 3 on the phone moves the card to book 3 at the phone's position, and finishing a book anywhere moves the card on.
- Playback runs straight on into the next book. When listening crosses into it, the bridge closes the finished book's ABS session, marks it finished, and opens one on the next book.
- The Yoto playlist is named after the series or collection; the Cards tab shows which book the card is on (📚 series, 🗂️ collection).
- Books added to or reordered in a series or collection take effect within a minute; the book picker's "which collections is this book in" list refreshes in the background every few minutes (ABS sends collections fully expanded, which can be megabytes).
- **Make offline** on a series or collection card continues into the following books until the hours run out, and syncs back to the right book.

### Chapters as tracks and skipping

The Yoto cannot seek inside a stream track, but its next/previous buttons move between tracks, and it **remembers per card which track it was on and asks for that track on the next tap**. So each streaming card's Yoto playlist has one track per chapter with stable numbers: **track N is chapter N** of the current book, from chapter 1, then on into the next books of a series or collection (at most 99 tracks). What a track plays is decided when the player asks for it:

- **A fresh tap** (no listening session, or the card was taken out since): the track holding your ABS position resumes there (less the 5 s rewind). If the player asks for a different track (you moved on with the phone), the bridge answers with a few seconds of silence and tells the player over MQTT to start the right track. Without MQTT it plays your spot under the track asked for.
- **Within a listen**: next/previous and the player moving on by itself play whole chapters from their start, so previous goes all the way back to chapter 1.
- The track list is only rewritten when the book changes (series), so the numbers the player remembers stay valid. A book with more than 99 tracks gets a 99-track window that moves in steps of 90 (chapters 1-99, then 91-189, …): the list changes once per 90 chapters, and the first tap after that is moved to the right track automatically. Track URLs carry a layout id; a player starting a cached list of a book no longer on the card gets the current list.
- Each track has its own chapter icon. Books without chapters in ABS get 30-minute parts (`part_seconds`), and chapters longer than an hour are split into parts too (some books have two 3-hour "chapters"). Those cuts, which are not chapter boundaries, move to the nearest pause in the audio within 20 seconds (found with ffmpeg the first time the book is laid out, then kept per book in `data/quiet.json`, so a book always gets the same cuts), so a track change does not land mid-word; offline copies cut the same way. `snap_cuts: false` turns this off.
- On the phone page, a playing player has **⏮ / ↺30 / 30↻ / ⏭**: the bridge moves the ABS position and starts the card on the right track there (no rewind). "Play on Yoto" also starts on the track of the chapter you're in.
- `chapter_tracks: false` goes back to one whole-book stream per card.

### Playback speed

The Yoto has no speed control of its own, so the bridge plays the book faster or slower itself (ffmpeg `atempo`, which keeps the voice's pitch). Two settings on the phone page, both stored in the bridge's `data/state.json`:

- **Player speed**, the menu on each player at the top of the page: every card that player plays, and Play on Yoto.
- **Card speed**, the menu on each card in the Cards tab: that card on any player. "Player's speed" (the default) means the card has none of its own. **A card's own speed wins over the player's.**

**Keeps its own place** (the bookmark menu on each card, next to its speed): the card resumes from a place the bridge keeps for it (`data/card_progress.json`) and never writes to Audiobookshelf: no listening session, no position, no "finished". Use it for a bedtime card a child falls asleep to and leaves running, so the book in ABS (and on the phone) stays where it was. Turning it on starts the card from where ABS has the book at that moment; turning it off goes back to ABS's place. Offline copies of such a card keep their positions on the bridge too.

Changing a speed while something plays restarts it right where it is (a second or two of silence, like the seek buttons), so the new speed is heard at once. Positions written to ABS are always book positions, and the listening time ABS records is real time. Chapter tracks, the 30 s seek buttons and the 5 s rewind are in book time, so at 2× a 30-minute part lasts 15 minutes. Offline copies have their own speed, chosen when you press Make offline (it starts at the card's speed); it is built into the uploaded audio, and at 1.5× the 5 hours Yoto allows hold 7.5 hours of the book. Speed needs `resume_mode: server` (the default).

When players have different speeds and a card has none of its own, a tap waits briefly for the player's "card inserted" event to know which speed to encode at (as it does for per-player ABS users); if the event comes too late, the card starts at 1× and restarts at the player's speed when the event arrives.

### Finding missing chapter markers (Chapters tab)

Chapter skipping is only as good as the book's chapters. The **Chapters** tab finds every book with no chapters (or one), and every book whose chapters are just its audio files (ABS makes one chapter per file, named after the file), looks each up on Audible through ABS's own chapter lookup, and shows a proposal per book before anything changes:

- **Scan** lists the books and looks them up in the background. The ASIN comes from the book's ABS metadata; without one (or if Audible has no chapters for it) ABS's Audible search by title and author picks the edition nearest your audio's length.
- Each book shows your ABS cover next to the Audible edition's cover and title (tap it to open the Audible page), the chapter count, your audio length against Audible's, and a verdict: **Good match** (within 15 s), **Check** (within 2 min, chapters past the end, or Audible says the times are approximate), **Likely wrong edition** (further off). Audible's "This is Audible" intro/outro is removed when that makes the lengths fit better, as the ABS chapter editor's checkbox does.
- A book whose file chapters already match Audible's (same count, starts within 3 s) is marked **Already right** and left alone; otherwise its current chapters are shown under the proposed ones.
- **Check every book against Audible** also looks at books with chapters of their own (embedded in an m4b, say) and lists those that differ from Audible's. It does one Audible lookup per book, so it is slow on a big library; such books are applied one at a time, never by "Apply all".
- **Chapters** shows the full list; **Other edition** / **Find edition** picks another search match or takes an ASIN by hand (and an Audible store); **Try again** repeats a lookup that failed or found nothing; **Skip** hides a book from later scans until **Undo skip**. Leaving a book alone keeps it in To review: the review is saved on the bridge (`data/chapters.json`) and survives closing the page and restarting the bridge. **Apply** (or **Apply all good matches**) writes the chapters to ABS and, if ABS had no ASIN, saves the one found. A new scan keeps applied and skipped decisions.
- **Books Audible can't help with** (dramatized editions listed as a few hour-long parts, home recordings): run `tools/audio_chapters.py` on a fast computer (not a low-power server). It downloads the book from ABS, uses Whisper to hear the narrator say "Chapter N", starts each chapter at the end of the pause just before it, places a chapter that is never said at the longest pause between its neighbours, and falls back to the strongest pauses when nothing is said. The result appears in the Chapters tab marked "found in the audio", for review like any other. Needs ffmpeg and `pip install faster-whisper`; `--search "title"` lists book ids, `--expect N` gives the chapter count you expect.
- Writing chapters needs an ABS user allowed to edit books. If `abs_api_key` belongs to a viewer, set `chapters_api_key` to a key of a user with the Update permission (the tab says when it is missing). `audible_region` sets the default Audible store (`us`).
- Cards already pointing at a book get the new chapter tracks the next time their track list is rebuilt.

### Offline copies (car trips, flights)

A streaming card needs the internet on every tap. For a trip, open **Cards** on the bridge page and press **Make offline** on the card:

1. The bridge reads the book's ABS position and cuts the next 5 hours (`offline_hours`) into tracks at chapter boundaries, splitting chapters longer than 55 minutes (`offline_track_seconds`).
2. It encodes each track, uploads it to Yoto as ordinary MYO audio, and replaces the card's playlist with those tracks. The card stays linked; nothing to do in the Yoto app. The card row shows progress ("uploading track 3 of 6"), then "Offline copy: 1h 02m → 6h 02m".
3. **Play the card once at home on Wi-Fi** so the player downloads the tracks. After that it plays without the bridge or the internet, and the Yoto keeps its own place.
4. Whenever the player is online and playing the copy, the position it reports (chapter + position within it) is written to ABS. It never moves ABS backwards past a point the phone has reached since.
5. Back home, press **Back to streaming**: the stream track goes back on the card and the next tap resumes from ABS.

**What to copy.** The sheet offers three choices:

- **Next 5 hours** (as above).
- **Whole book**: the whole book from its start, one chapter per track, so track N is chapter N as on a streaming card. Chapters are not split, except a "chapter" over an hour (below); a book with more than 99 chapters has neighbouring chapters share a track.
- **Whole series** (series and collection cards): every book from book 1 to the end of the series, wherever the listener is (the player is still moved to where they left off). If that is too much for 99 tracks (or for `offline_max_mb`), it starts at the current book instead. Each track holds as many whole chapters as it takes to fit everything in 99 tracks, and no track spans two books.

While a copy is being made, the card gets the copy's track list at once: tracks not uploaded yet stream from the bridge, and finished ones switch to their uploaded audio every 5 tracks or 2 minutes (never while the card is playing, so the player is not cut off). The card row shows "12 of 56 tracks on the card, the rest stream". Tracks on the card play offline once the player has downloaded them; until the copy says ready, the card still needs the bridge for the rest. Streamed tracks play at the card's speed, not the copy's. If the bridge restarts mid-upload (a deploy), it carries on at start-up, reusing the tracks already uploaded; pressing Make offline again after a failed copy reuses them too. If the card was listened to while the copy was made, the copy resumes from where that listening got to.

A whole copy starts at the top, so on its first play while online the bridge moves the player to where the book was left, and nothing is written to ABS until the player has got there (a copy starting at track 1 never drags ABS back). Whole copies keep each track under about 1 h 55 min of audio (Yoto's API never finished processing longer ones: 96 to 119 minute tracks were done in a minute or two, a 122 minute one never), so chapters are not split, with one exception, the same as on a streaming card: a book without chapters, or a "chapter" longer than an hour (a book listed as one or two huge chapters), goes in 30-minute parts (`part_seconds`), so no single track is a whole long book. To keep to Yoto's stated 60 minutes per track (in case its API enforces it), set `offline_whole_track_seconds: 3300`; then long chapters are split and 99 tracks hold about 90 hours of book at 1×. A series that does not fit takes as many whole books as fit and the card row says how many were left off; a single book that does not fit fails with a message, and the card keeps streaming.

Limits: Yoto's guide says 100 tracks, 60 minutes or 100 MB per track and 5 hours or 500 MB per card. The per-card total and the 60 minutes per track are not hard limits in practice (whole multi-book series fit on one card, with longer tracks), so Next 5 hours keeps to its 5 hours and 55-minute tracks but whole copies have no total limit and keep tracks under 2 hours; set `offline_max_mb` and `offline_whole_track_seconds` to add them. If Yoto refuses an upload, the copy fails and the card keeps streaming; an upload Yoto has not finished processing after 15 minutes is sent again, up to 3 times. If ABS or the network goes away part-way (a restart, a NAS update), the copy waits and carries on, for about an hour in all, and a track ABS stopped sending part-way is made again rather than uploaded with a gap. If a copy does fail, press Make offline with the same choice again: the tracks already uploaded are reused. Copies are made one at a time: press Make offline on several cards and the others wait their turn, in order, each card streaming as usual until its copy starts (`offline_parallel: 2` makes two at once). The Cards tab shows a progress bar per card (tracks uploaded, and the stage of the one in flight) and one over all copies being made. The audio is uploaded to your Yoto account (only for books you make offline). Listening done while the player is offline only reaches ABS when the player next reports its position online.

### A child's own ABS user, and one per child

Progress is saved to the ABS user whose key the bridge uses. If a child listens on the Yotos and you listen to the same book on your phone, give the child their own ABS user so you don't move each other's place:

- **One child, any number of players:** create an ABS user for the child (a viewer is enough; give it access to the kids' library), make a key for it, and put that key in `abs_api_key`. Restart the bridge. The phone page then shows and plays as the child, which is what you want for handing off from the child's phone or tablet.
- **Different children on different players:** keep `abs_api_key` for the child who uses most cards, and give each other child's player its own key under `devices:` (player ids are in the bridge log at startup):

  ```yaml
  devices:
    y24xxxxxxxxx:
      name: Player
      abs_api_key: "key-of-the-second-child"
  ```

  The audio request does not say which player sent it (behind a proxy or tunnel every request comes from the same address), so the bridge recognises the player another way: the player a phone-page ▶ was sent to, the player's LAN IP if you set `ip:` (players on the bridge's own network only), or the player's own "card inserted" event, which it waits up to `identify_wait_seconds` (2 s) for. If the event arrives later than that, the card starts from the default user's position, and as soon as the event arrives the bridge restarts it on that player from its own user's position, writing nothing to the wrong user. The log says `… is on Player, which has its own ABS user: restarting it`.

  "Make offline" copies from the position of the card's assigned player, or the default user.

### How long a card takes to start

Between a tap and sound, three things happen: the Yoto looks the card up in Yoto's cloud and asks for the audio (through your proxy or tunnel, if you use one); the bridge opens an ABS session and starts sending audio; the player fills its buffer and starts playing. Only the middle part is the bridge's.

To make it short, the bridge keeps the first 30 seconds of every card (`warm_start_seconds`), from where ABS has the book, already encoded. A tap then gets audio at once, and the player's buffer fills at network speed while ffmpeg starts on the rest. The bridge re-checks ABS positions every 5 minutes (`warm_refresh_seconds`) and after every listening session; if the phone has moved the book since, the tap starts normally from the new position.

The log shows where a slow start spends its time. Note the time you put the card in, then read:

- `GET <card> from …`: the player asked for the audio. Time before this is the Yoto and Yoto's cloud (and any proxy or tunnel).
- `<book>: first audio sent 0.12s after the request (warm start)`: the bridge's share.
- `<book> playing on <player>, 2.3s after the player asked for the audio`: the rest is the player buffering.

### Security & External API Authentication

The bridge includes dual-layer security tailored for both local web users and external automation clients (such as Home Assistant or the ESP32 broker):

- **Master API Key (`api_key`)**:
  Protects all `/api/*` management endpoints against unauthorized external requests.
  - Set `api_key: "your-32-char-hex-key"` in `config.yaml` or pass via environment variable `API_KEY` / `BRIDGE_API_KEY`.
  - External callers can authenticate using either:
    - HTTP Header: `X-Api-Key: <key>` or `Authorization: Bearer <key>`
    - Query Parameter (GET requests): `?apikey=<key>`
  - The built-in phone web UI (`web/`) running on the same origin/host is seamlessly permitted, leaving card configuration actions guarded by `ui_pin` if enabled.
  - `/api/status` is public for status checks and service discovery.
  - An API key can be dynamically regenerated via `POST /api/security/regenerate-key` (which saves the new key to `config.yaml`).

- **Phone Web UI PIN (`ui_pin`)**:
  Protects sensitive card-changing actions directly on the phone web interface (e.g. changing which book a card plays, creating cards, making offline copies). When set, the web UI prompts once for the PIN and remembers it locally.

### Extended Yoto Management Endpoints

The bridge exposes management endpoints used by external brokers (such as the ESP32 broker), automations, and third-party dashboards. Protected endpoints accept `X-Api-Key`, `Authorization: Bearer <key>`, or `?apikey=<key>`:

| Endpoint | Method | Auth | Request / Payload | Description |
|---|---|---|---|---|
| `/api/status` | `GET` | Public | None | Bridge health, active Yoto devices, current playback state, speeds, alerts, and login status |
| `/api/yoto/library` | `GET` | API Key | None | Returns user and family library playlists and cards (requires `family:library:view`) |
| `/api/yoto/groups` | `GET` | API Key | None | Returns family card groups, playlist categories, and custom tags |
| `/api/yoto/card/{card_id}` | `GET` | API Key | None | Detailed card metadata, chapter tracks, and 16×16 display icon URLs |
| `/api/yoto/card/{card_id}` | `PUT` | API Key / PIN | `{"title": "...", "cover_url": "...", "chapters": [...]}` | Update MYO playlist track content, chapter titles, audio streams, and display icons |
| `/api/yoto/icons/upload` | `POST` | API Key / PIN | `{"image_b64": "data:image/png;base64,...", "filename": "icon.png"}` or binary PNG bytes | Upload custom 16×16 pixel icons directly to Yoto's CDN, returning `icon_ref` (e.g. `yoto:#...`) |
| `/api/yoto/volume` | `POST` | API Key / PIN | `{"device_id": "...", "volume": 12}` | Adjust physical player volume directly via active MQTT control connection |
| `/api/security/regenerate-key` | `POST` | API Key | None | Generates a new 32-character hex API key, hot-reloads it in memory, and persists it to `config.yaml` |
| `/api/alerts/test` | `POST` | API Key / PIN | None | Dispatches immediate test alert notifications via Discord Webhook and/or ntfy |



### Alerts when something breaks

A broken Yoto login or an unreachable `public_url` otherwise shows up as "the card doesn't play" at bedtime. The bridge can push notifications through **Discord** webhooks and/or [ntfy](https://ntfy.sh):

- **Discord webhook**:
  1. In Discord: Server Settings → Integrations → Webhooks → New Webhook, and copy the Webhook URL.
  2. In `data/config.yaml`:
     ```yaml
     discord_webhook_url: https://discord.com/api/webhooks/<id>/<token>
     discord_username: Yoto Bridge
     # discord_avatar_url: ""
     ```
  3. Messages older than 48 hours are automatically pruned from the Discord channel to prevent notification clutter.
- **ntfy**:
  1. Install the ntfy app on your phone and subscribe to a topic with a long random name, e.g. `yoto-bridge-7f3k9q2m`. Anyone who knows the name can read it, so don't use a guessable one. (Your own ntfy server works too; put an access token in `ntfy_token`.)
  2. In `data/config.yaml`: `ntfy_url: https://ntfy.sh/yoto-bridge-7f3k9q2m`, then restart the bridge.

3. **Send a test notification**:
   ```sh
   curl -X POST http://<bridge-host>:8090/api/alerts/test -H "X-Api-Key: <api_key>"
   ```
   (Or with `-H "X-Pin: <pin>"` if `ui_pin` is set, or unauthenticated from the local phone web UI).

Every 5 minutes (`alert_check_minutes`) the bridge checks that ABS answers and accepts its key, that the Yoto login still works, that the player connection (MQTT) is up, and that `public_url` reaches this bridge from outside (it fetches `<public_url>/s/health`). A problem is sent after two failed checks in a row (`alert_after_failures`), so an ABS restart doesn't wake you, and once more when it is fixed. A card that cannot play is sent at once. Current problems also show as a banner on the phone page. If your network can't reach its own public address from inside (some routers can't), set `alert_check_public: false`.

---

## Playing to a Yoto from the ABS Android app: why it is a separate page

The ABS Android app has no plugin or "cast to custom device" hook. Its Cast button speaks Google Cast, which only certified receivers can implement, so the bridge cannot appear there. The options:

| Route | Friction in daily use | Cost |
|---|---|---|
| **Bridge web page on the home screen** (built) | Two taps: open the bridge icon, tap ▶ <player>. Same library, same progress, because both read ABS. | None beyond the bridge. Survives every ABS app update. |
| Fork the ABS app ([advplyr/audiobookshelf-app](https://github.com/advplyr/audiobookshelf-app), Vue + Capacitor) and add a "Play on Yoto" menu item that calls the bridge's `POST /api/play` | One tap from inside the app | A small code change, but you build and sideload your own APK, lose Play Store updates, and re-merge on every upstream release. |

The fork is a reasonable later step if the extra tap annoys you; the bridge API it would call already exists (`POST /api/play {"device_id","item_id"}`), so it is only UI work on the app side.

One thing to check when you try the hand-off: if the ABS app still has the book open in its player from before, pressing play in the app *may* continue from the app's own last position instead of re-reading the server (it likely re-syncs on reopen, but this is not verified). Closing the book in the app before handing over to the Yoto avoids the question.

---

## Unverified points and what each test result changes

Things only a real player can answer that have not been confirmed yet. Each maps to one config line or one log line to read.

| Unknown | What to look at | If it goes the other way |
|---|---|---|
| The player caches stream tracks offline | Play a card, unplug the bridge's network, tap again. | Server-side resume assumes the player re-requests the URL each time. If it plays from a cache, resume falls back to the Yoto's own position for that session and the next online play re-syncs. For car trips, use [offline copies](#offline-copies-car-trips-flights). |
| MQTT `position` for a stream: counted from where this stream started, or from the start of the book | With `LOG_LEVEL=DEBUG`, compare the event positions to the bridge's `start … at Ns` line. | Set `event_position: absolute`. Either way the cap on delivered audio keeps a wrong guess from jumping ahead. |
| Pause and resume on a streamed chapter track | Play a minute, pause for 10 s, resume; then pause for a minute and resume. The log shows `resuming track N at Ss (2s back)`, then `(5s back)`. | A Yoto hangs up the stream on pause and asks for the track afresh on resume; if it ever sends a Range header instead, the resume is served from that byte and these lines do not appear. |
| Offline copies: MQTT `position` is within the current track, and `chapterKey` is the uploaded chapter's key | Make a card offline, play it a minute into track 2, pause. The log shows `offline copy of … : wrote Ns to ABS`; N should be track 2's start plus about a minute. | If positions turn out to be counted from the start of the playlist, the mapping in `offline.py` (`on_event`) changes to use them directly. |


## Known limits

- **Skipping is by chapter.** The Yoto cannot seek inside a stream, so its next/previous move between chapter tracks; finer jumps (30 s) are on the phone page. Up to 99 tracks per card; a card further from the end of a long series gets the rest when its track list is rewritten after each listen.
- **Yoto cloud is in the loop** for card lookup and MQTT. If Yoto's service is down, cards do not start, even though the audio comes from your server.
- **Per-player ABS users** rely on the player's MQTT event naming the card (which it does), unless the phone page started it or `ip:` is set. If that event arrives late, the card starts as the default user and restarts on the right one. With one ABS user for all players this does not matter.
- **One player per card at a time.** Two players playing the same card simultaneously share one session.
- **Security:** the stream URLs have no login (the Yoto cannot send credentials) but use unguessable 12-character slots. If you publish the bridge, publish only `/s/`, never the whole bridge. All management endpoints under `/api/*` are protected by a master API key (`X-Api-Key` or Bearer token) for external consumers and brokers, while the same-origin phone web interface is protected by `ui_pin` for card and playback changes. Do not expose unauthenticated management ports to the public internet.
- **Transcoding:** every stream is re-encoded to 64 kbps mono MP3 with ffmpeg. Measured on a desktop CPU: 10 minutes of AAC took 4 s of CPU, i.e. under 1 % of one core per playing Yoto; a low-power server CPU will be a few times that.

## Development

The phone page is a React + Vite + Tailwind 4 app using [shadcn/ui](https://ui.shadcn.com) components, in `web/`. It builds into `yotobridge/static/` (not committed; the Dockerfile's first stage builds it):

```sh
cd web && npm ci && npm run build      # or: npm run dev, with the bridge on 127.0.0.1:18091 (BRIDGE=… to change)
```

Add components with `npx shadcn@latest add <name>`; they land in `web/src/components/ui`.

```sh
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest            # needs ffmpeg on PATH
LOG_LEVEL=DEBUG BRIDGE_CONFIG=data/config.yaml python -m yotobridge
```

Layout: `yotobridge/app.py` (HTTP routes, wiring), `playback.py` (sessions, position, ABS writes), `streamer.py` (ffmpeg → constant-bitrate MP3), `yoto.py` (Yoto login, playlists, MQTT), `abs_client.py`, `store.py` (card ↔ book state), `web/src` (phone page: `App.tsx`, `views/`, `components/`). `tests/conftest.py` holds the fake ABS and Yoto servers.
