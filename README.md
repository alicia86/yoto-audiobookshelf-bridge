# Yoto ↔ Audiobookshelf bridge

A small service that runs next to Audiobookshelf (ABS) in Docker and makes your Yoto players play ABS books, with ABS as the only record of progress.

- **Cards.** A MYO card points at an ABS book. Tap it and the Yoto plays from wherever that book was left, whether on the Yoto or on the ABS phone app. Pull it out and the position is saved in ABS.
- **Phone.** A web page (save it to the home screen) lists "Continue listening" and the whole library. "▶ <player>" plays any book on that Yoto, no card needed, from the ABS position.
- **Audio comes from your server.** The players fetch the audio from the bridge, on your LAN or through a public HTTPS address that serves only the audio ([players on other networks](#publishing-s-for-players-on-other-networks)). Nothing is uploaded to Yoto. Yoto's cloud is still used to look up which playlist a card belongs to and to carry play/pause commands and position reports (MQTT).

Status: early. Tested against fake ABS and fake Yoto servers (real ffmpeg audio) and connected to real Yoto players; the behaviours that only a real player can confirm are isolated behind config switches, see [Unverified points](#unverified-points-and-what-each-test-result-changes).


**Unofficial.** Not affiliated with or endorsed by Yoto. Works with Yoto via the official [developer API](https://yoto.dev). The repo ships no Yoto client id: each user registers their own app at [dashboard.yoto.dev](https://dashboard.yoto.dev) and accepts Yoto's [API Terms](https://yoto.dev/reference/terms-and-conditions/). Because the bridge uses the `family:devices:control` scope (to start and pause playback), Yoto will not verify the app, so its sign-in page shows an "unverified app" warning that you accept once. Yoto says such apps can still be used personally and shared.

**Your data.** Everything the bridge keeps is in `data/` on your own host: `config.yaml` (your ABS key and Yoto client id), `yoto_tokens.json` (your Yoto login, file mode 600) and `state.json` (which card plays which book). It talks only to your Audiobookshelf server and to Yoto's API, login and MQTT services, plus the ntfy server you choose if you turn on alerts (the alert text names the problem and, for a failed card, the book); there is no telemetry. It uses the parent's Yoto account and ABS user and collects nothing about children. Only stream audio you have the right to use.

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
2. **Yoto client id.** Sign in at [dashboard.yoto.dev](https://dashboard.yoto.dev) with the Yoto account your players are on, create an app as a **Public Client** with the scopes `user:content:manage`, `family:devices:view`, `family:devices:control`, `offline_access` and (for chapter icons on offline copies) `user:icons:manage`, and copy the client id (there is no secret). Under **Allowed Callback URLs** add `<public_url>/s/yoto-callback`, for example `https://yoto.example.com/s/yoto-callback` (see [Choosing `public_url`](#choosing-public_url)). The bridge signs in with the browser login (authorization code + PKCE); Yoto refuses the older device-code login for new apps.
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

- **Streaming cards** point their track at `<public_url>/s/icon/<slot>.png` (Yoto "dynamic icons", `display.iconUrl16x16`), which the bridge draws on request for the chapter being listened to, or the chapter ABS has the book at when the card is idle. Existing cards get the icon on the next bridge start. Whether the player re-fetches the icon while a track plays (so the number moves on mid-stream) is not documented; see [Unverified points](#unverified-points-and-what-each-test-result-changes).
- **Offline copies** upload one icon per chapter (`display.icon16x16`), so each chapter track shows its number with no internet. This needs the **`user:icons:manage`** scope on your Yoto app (dashboard.yoto.dev → your app → Scopes), then **Connect Yoto** again on the bridge page so the new permission is granted. Without it, offline copies are made as before, just without icons, and the log says why. Icons are uploaded once per book chapter and reused.

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
- Each track has its own chapter icon. Books without chapters in ABS get 30-minute parts (`part_seconds`), and chapters longer than an hour are split into parts too (some books have two 3-hour "chapters").
- On the phone page, a playing player has **⏮ / ↺30 / 30↻ / ⏭**: the bridge moves the ABS position and starts the card on the right track there (no rewind). "Play on Yoto" also starts on the track of the chapter you're in.
- `chapter_tracks: false` goes back to one whole-book stream per card.

### Playback speed

The Yoto has no speed control of its own, so the bridge plays the book faster or slower itself (ffmpeg `atempo`, which keeps the voice's pitch). Two settings on the phone page, both stored in the bridge's `data/state.json`:

- **Player speed**, the menu on each player at the top of the page: every card that player plays, and Play on Yoto.
- **Card speed**, the menu on each card in the Cards tab: that card on any player. "Player's speed" (the default) means the card has none of its own. **A card's own speed wins over the player's.**

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

Limits: Yoto allows 100 tracks, 60 minutes per track and 5 hours per card, so a copy covers up to 5 hours from where you are, not the whole of a long book. The audio is uploaded to your Yoto account (only for books you make offline). Listening done while the player is offline only reaches ABS when the player next reports its position online.

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



### Alerts when something breaks

A broken Yoto login or an unreachable `public_url` otherwise shows up as "the card doesn't play" at bedtime. The bridge can push a notification through [ntfy](https://ntfy.sh) (free, no account):

1. Install the ntfy app on your phone and subscribe to a topic with a long random name, e.g. `yoto-bridge-7f3k9q2m`. Anyone who knows the name can read it, so don't use a guessable one. (Your own ntfy server works too; put an access token in `ntfy_token`.)
2. In `data/config.yaml`: `ntfy_url: https://ntfy.sh/yoto-bridge-7f3k9q2m`, then restart the bridge.
3. Send a test: `curl -X POST http://<bridge-host>:8090/api/alerts/test` (add `-H "X-Pin: <pin>"` if you set `ui_pin`).

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
| Offline copies: MQTT `position` is within the current track, and `chapterKey` is the uploaded chapter's key | Make a card offline, play it a minute into track 2, pause. The log shows `offline copy of … : wrote Ns to ABS`; N should be track 2's start plus about a minute. | If positions turn out to be counted from the start of the playlist, the mapping in `offline.py` (`on_event`) changes to use them directly. |


## Known limits

- **Skipping is by chapter.** The Yoto cannot seek inside a stream, so its next/previous move between chapter tracks; finer jumps (30 s) are on the phone page. Up to 99 tracks per card; a card further from the end of a long series gets the rest when its track list is rewritten after each listen.
- **Yoto cloud is in the loop** for card lookup and MQTT. If Yoto's service is down, cards do not start, even though the audio comes from your server.
- **Per-player ABS users** rely on the player's MQTT event naming the card (which it does), unless the phone page started it or `ip:` is set. If that event arrives late, the card starts as the default user and restarts on the right one. With one ABS user for all players this does not matter.
- **One player per card at a time.** Two players playing the same card simultaneously share one session.
- **Security:** the stream URLs have no login (the Yoto cannot send credentials) but use unguessable 12-character slots. If you publish the bridge, publish only `/s/`, never the whole bridge: the phone page and `/api/` can change cards and browse the library. The phone page can be PIN-protected for card changes with `ui_pin`. Do not port-forward the bridge.
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
