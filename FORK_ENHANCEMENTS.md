# Fork Comparison & Enhancements: Yoto-Audiobookshelf Bridge

> **Fork Repository:** [alicia86/yoto-audiobookshelf-bridge](https://github.com/alicia86/yoto-audiobookshelf-bridge)  
> **Upstream Project:** [mylacc/yoto-audiobookshelf-bridge](https://github.com/mylacc/yoto-audiobookshelf-bridge)  
> **Upstream Baseline Commit:** `b85d192` (*Claude can draw the AI chapter pictures with the owner's own API key*)  
> **Branch:** `main`

---

## 1. Overview & Architectural Goals

This document provides a comprehensive, technical comparison of all features, security controls, and architectural enhancements introduced in this fork.

While upstream focused on core streaming, series sequence chaining, Audible chapter alignment, and on-the-fly pixel art synthesis, this fork expands the bridge into an **extensible, production-grade integration hub** capable of:
1. **Multi-System Interoperability:** Providing machine-to-machine REST APIs for external homelab services, middleware brokers (e.g. ESP32 NFC/RFID players, Home Assistant), and curation studios without bypassing bridge state.
2. **Robust Security & Granular Access Control:** Introducing Master API key middleware for remote automation while maintaining a smooth, PIN-authenticated or unauthenticated same-origin web interface for mobile household users.
3. **Decoupled, Per-Item Artwork Caching:** Transitioning chapter icons from ephemeral, slot-bound coordinates to a persistent, item-centric cache where curated pixel art is stored once per Audiobookshelf book item and automatically inherited across any physical card, series progression, or offline copy.
4. **Rich Operational Alerting:** Adding native Discord Webhook notifications with embed formatting, system health checks, and automatic 48-hour rolling log pruning.
5. **Direct Yoto Cloud Ecosystem Utilities:** Exposing family library exploration, card group assignments, remote player volume control, direct 16×16 display icon uploads, and programmatic MYO playlist updates.

---

## 2. Feature Breakdown: Upstream Baseline vs. Fork Enhancements

| Component / Subsystem | Upstream Baseline (`b85d192`) | Fork Enhancements (`alicia86`) |
| :--- | :--- | :--- |
| **API Authentication** | UI PIN only (`ui_pin` in config / `X-Pin` header). External requests had no dedicated token standard. | **API Key Middleware (`api_key`):**<br>• `X-Api-Key` and `Authorization: Bearer <key>` support.<br>• Auto-generates high-entropy token into `config.yaml` on initial boot if omitted.<br>• Exposes key status and regeneration (`POST /api/security/api-key/regenerate`).<br>• Fully exempts same-origin web UI browsers and public stream paths (`/s/*`). |
| **Chapter Artwork Caching** | Stored strictly per card slot (`data/icons/<slot>/<item>-<n>.rgba`). Reusing or pointing a new card lost custom artwork. | **Per-Item Caching (`data/icons/items/<item_id>/<n>.rgba`):**<br>• Indexed in `state.json` under `item_icons`.<br>• Universal inheritance: any card playing the book inherits custom chapter icons automatically.<br>• Precedence hierarchy: Slot custom icon → Item custom icon → Card picture → Configured style. |
| **External Artwork APIs** | Artwork only uploadable via web UI per active slot. | **Item-Level REST Endpoints:**<br>• `GET /api/items/{item_id}/icon/chapters/{n}`<br>• `PUT /api/items/{item_id}/icon/chapters/{n}`<br>• `DELETE /api/items/{item_id}/icon/chapters/{n}`<br>• `POST /api/items/{item_id}/icon/chapters/batch` (bulk JSON base64 upload). |
| **Yoto Cloud Operations** | Read-only player list & card playlist synchronization during card setup. | **Direct Cloud Utilities:**<br>• `GET /api/yoto/cards/all` (fetch entire family library).<br>• `GET /api/yoto/groups` & `POST /api/yoto/cards/{card_id}/group` (card organization).<br>• `POST /api/yoto/players/{device_id}/volume` (remote volume limits & adjustments).<br>• `POST /api/yoto/icons/upload` (direct 16×16 PNG to `yoto:#<mediaId>`).<br>• `POST /api/yoto/cards/{card_id}/content` (programmatic MYO playlist replacement). |
| **OAuth Scopes** | Requested basic scopes (`offline_access`, player control, etc.). | **Expanded Family Scopes:**<br>• Added `family:library:view` and `family:library:manage` to OAuth request for full card management. |
| **Operational Alerting** | `ntfy` webhook notifications only. | **Discord Webhook Alerts (`discord_webhook_url`):**<br>• Formatted embeds (color codes, timestamps, error details).<br>• 48-hour rolling message pruning (deletes obsolete Discord alerts automatically via `?wait=true`).<br>• Configurable username and avatar (`discord_username`, `discord_avatar_url`).<br>• Manual test endpoint (`POST /api/alerts/test`). |
| **Offline Copies** | Uploaded custom icons to Yoto CDN if present on the slot. | **Item-Aware Offline Copies:**<br>• `uploaded_icon` seamlessly pulls from the item-level cache so offline copies inherit curated artwork without manual slot customization. |

---

## 3. Deep-Dive: Architectural & Code Modifications

### 3.1 Security & Authentication Architecture
*Files modified: `yotobridge/config.py`, `yotobridge/app.py`, `web/src/lib/api.ts`*

#### The Challenge
Upstream secured the bridge web UI via `ui_pin`. While effective for household members accessing the web interface from a phone, it proved awkward for headless scripts, ESP32 brokers, and external automation systems which require persistent, revokable tokens without interfering with local browser sessions.

#### The Solution
- **Dual-Layer Middleware:**
  - Requests bearing an `X-Api-Key` or `Authorization: Bearer <key>` matching `cfg.api_key` are granted immediate administrative access.
  - Same-origin browser requests (detected via `Sec-Fetch-Site: same-origin` or browser headers) and local unauthenticated discovery (`/api/status`, `/s/*` streams) bypass the API key check and fall back to standard `ui_pin` verification.
- **Automatic Key Provisioning:**
  - If `api_key` is empty or omitted on startup, `Config.load()` generates a cryptographically secure 32-character hexadecimal token (`secrets.token_hex(16)`), writes it directly to `config.yaml`, and logs a notice.
- **Key Management API:**
  - `GET /api/security/api-key`: Returns the key status, prefix/suffix, and configuration method.
  - `POST /api/security/api-key/regenerate`: Generates a fresh key, saves it to `config.yaml`, and returns the new secret.

---

### 3.2 Per-Item Chapter Artwork Cache
*Files modified: `yotobridge/store.py`, `yotobridge/app.py`*

#### The Challenge
In the original design, custom chapter pictures were tied to a physical card slot:
```
data/icons/<slot>/<item_id>-<chapter_number>.rgba
```
If a user switched the book on a card, reused a MYO card, played via remote play ("Play on Yoto"), or set up a multi-book series progression, the carefully curated or AI-generated chapter artwork was dissociated.

#### The Solution
- **State Schema Extension:**
  Added `item_icons: dict[str, dict[str, str]]` to `State` in `yotobridge/store.py`:
  ```python
  @dataclass
  class State:
      cards: dict[str, CardSlot] = field(default_factory=dict)
      ...
      item_icons: dict[str, dict[str, str]] = field(default_factory=dict)
  ```
- **Unified On-Disk Hierarchy:**
  Icons are saved under:
  ```
  data/icons/items/<item_id>/<chapter_number>.rgba
  ```
- **Resolution Precedence in `custom_picture()`:**
  ```python
  def custom_picture(self, slot: CardSlot, item_id: str, number: int) -> bytes | None:
      # 1. Slot-specific custom chapter icon
      ch_key = f"{item_id}:{number}"
      if slot.icon_chapters.get(ch_key):
          p = self.card_icon_path(slot.slot, item_id, number)
          if p.exists():
              return p.read_bytes()

      # 2. Item-level cached chapter icon (NEW)
      if item_id and self.store.state.item_icons.get(item_id, {}).get(str(number)):
          p = self.item_icon_path(item_id, number)
          if p.exists():
              return p.read_bytes()

      # 3. Card-level custom picture
      if slot.icon_custom and slot.icon_custom_hash:
          p = self.card_custom_path(slot.slot)
          if p.exists():
              return p.read_bytes()

      return None
  ```
- **Batch Ingestion Endpoint:**
  External curation scripts can push an entire book's chapter track icons in one POST:
  ```http
  POST /api/items/{item_id}/icon/chapters/batch
  X-Api-Key: <api_key>
  Content-Type: application/json

  {
    "tracks": {
      "1": "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9h...",
      "2": "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9h..."
    }
  }
  ```

---

### 3.3 Yoto Cloud Extended Features
*Files modified: `yotobridge/yoto.py`, `yotobridge/app.py`*

#### Expanded OAuth Scopes
Updated `OAUTH_SCOPES` in `yotobridge/yoto.py`:
```python
OAUTH_SCOPES = [
    "offline_access",
    "user:profile:view",
    "user:cards:view",
    "user:cards:manage",
    "user:icons:view",
    "user:icons:manage",
    "user:player:view",
    "user:player:manage",
    "family:library:view",    # Added
    "family:library:manage",  # Added
]
```

#### New Remote Endpoints
1. **Family Library (`GET /api/yoto/cards/all`):**
   Retrieves all cards in the family account directly from Yoto Cloud (`/media/content/user/me/cards`), allowing external dashboards to view full libraries beyond the bridge's local slots.
2. **Card Groups (`GET /api/yoto/groups`, `POST /api/yoto/cards/{card_id}/group`):**
   Queries defined card groups (`/media/content/user/me/card-groups`) and reassigns cards to categories directly.
3. **Player Volume (`POST /api/yoto/players/{device_id}/volume`):**
   Dispatches volume level adjustments directly to physical Yoto players via Yoto API commands.
4. **Direct Icon Upload (`POST /api/yoto/icons/upload`):**
   Uploads a 16×16 PNG to Yoto Cloud storage (`/media/displayIcons/user/me/upload`) and returns the canonical `yoto:#<mediaId>` URI.
5. **MYO Content Sync (`POST /api/yoto/cards/{card_id}/content`):**
   Updates the tracks and chapters of an official Make Your Own playlist directly on Yoto servers.

---

### 3.4 Discord Webhook Alerting & Auto-Pruning
*Files modified: `yotobridge/alerts.py`, `yotobridge/config.py`*

- **Rich Embed Payloads:**
  In addition to plain text or `ntfy`, alerts can be sent to Discord via webhooks formatted as rich embeds with status colors (green for recovery, orange for warnings, red for offline/unreachable services).
- **Automatic 48-Hour Message Pruning:**
  When sending alerts to Discord, the bridge appends `?wait=true` to capture the resulting Discord `message_id`. Timestamps and message IDs are logged to `data/discord_history.json`. On subsequent runs, messages older than 48 hours (172,800 seconds) are deleted via HTTP `DELETE` requests, keeping notification channels clean and uncluttered.

---

## 4. Upstream Compatibility & Non-Breaking Design

A principal objective of this fork was ensuring **100% backward compatibility** with upstream installations:

1. **Config Safety:**
   - All newly added configuration keys (`api_key`, `discord_webhook_url`, `discord_username`, `discord_avatar_url`) are optional.
   - If omitted, existing installations continue running with their existing `ui_pin` behavior.
2. **State Backward Compatibility:**
   - Existing `data/state.json` files without `item_icons` load without error; the dataclass defaults to an empty dictionary (`field(default_factory=dict)`).
3. **Audio Streaming Untouched:**
   - Core FFmpeg streaming, 64 kbps mono transcoding, server-side resume, quiet pause detection (`data/quiet.json`), and Audible chapter matching logic remain completely faithful to upstream.
4. **Test Suite:**
   - Upstream unit tests pass cleanly alongside new test coverage added for API token verification, Discord embeds, and item-level cache resolution.

---

## 5. Summary of Commits on Fork

```
fd53ae3 feat: implement per-item chapter artwork caching and batch sync endpoints
8e8fb5b fix: readme formatting
c5f16a6 fix: readme information on localhost IP
4c3c850 refactor(docs): remove Arr-style references and standardize on API key
ce5567b docs(config): add api_key setting to config.example.yaml
21d7e0a docs(readme): expand extended API endpoint documentation and restore warm start section
37a0f96 docs: update README with API authentication, Discord alerts, and extended management endpoints
57155f3 fix(security): exempt web UI same-origin requests and status from api_key enforcement
3e0f542 feat(yoto): add endpoints to update MYO playlist content and upload display icons
271c2b3 feat(security): implement API key auth middleware and key regeneration
a1fe330 feat(oauth): add family:library:view and family:library:manage scopes for library and groups access
42559be feat(api): add endpoints for family library, card groups, and player volume control
6656073 feat: includes optional notifications via Discord Webhook
```

---

## 6. Suggestions for Upstream Integration

If upstream wishes to incorporate these improvements, they can be broken down into clean, modular Pull Requests:
- **PR 1: Item-Level Artwork Cache:** Decouples chapter icons from slots and stores them by `item_id`, benefiting users who reuse cards or listen to series.
- **PR 2: API Key Security Middleware:** Adds token protection for external integrations without impacting mobile browser users.
- **PR 3: Discord Webhook Notifications:** Adds Discord alerts with auto-pruning alongside the existing `ntfy` transport.
- **PR 4: Extended Yoto Management Endpoints:** Adds family library querying, card grouping, and remote volume control.
