# Phase 1 Test Execution & Review Log

> **Target Repositories:**  
> - [yoto-audiobookshelf-bridge](https://github.com/alicia86/yoto-audiobookshelf-bridge) (Host: `192.168.1.11:8090` / `https://yoto.ohiodeans.us`)  
> - [yoto-esp32-broker](https://github.com/alicia86/projects) (Host: `127.0.0.1:5055` / `192.168.1.72:5055`)  
> **Tester / Reviewer:** Alicia  
> **Date Started:** 2026-10-27  

Use this document to track hands-on verification, recording status (`[ ]` Pending, `[x]` Passed, `[-]` Failed / Issue) and notes in the comment blocks below each test case.

---

## 1. Bridge Web UI & Discord Alerting

### 1.1 Web UI "🔔 Test Discord" Button
- **Action:** Open Bridge web interface (`http://192.168.1.11:8090` or `https://yoto.ohiodeans.us`), go to **Cards** tab, scroll to bottom **Alert Notifications** section, and click **🔔 Test Discord**.
- **Expected Result:** Toast appears indicating test alert was sent; an informational embed (`🔵 Yoto Bridge Alert: System Test`) arrives in the configured Discord alerts channel with timestamp and footer.
- **Status:** [ PASS ]
> **Comments / Notes:**  
> Tested and working as expected:

---

### 1.2 Discord 48-Hour Message Auto-Pruning
- **Action:** Check `/data/discord_message_history.json` on the Bridge host.
- **Expected Result:** The message ID from the test embed is recorded with its Unix timestamp so it can be purged after 48 hours.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

## 2. Broker ↔ Bridge Live Connectivity & Security

### 2.1 Authenticated Bridge Connection Probe (Valid Key)
- **Action:** In Broker Admin Dashboard (`http://127.0.0.1:5055/admin`), go to **Settings** → **API Credentials**. Ensure Bridge URL is set and Master API Key matches `config.yaml` (`8e57054ac7c53115821110fe4f145488`). Click **Test Connection**.
- **Expected Result:** Success banner: `Bridge Connected (XXms)! Status: Logged in, X active players detected • API Key Verified.`
- **Status:** [ PASS ]
> **Comments / Notes:**  
> Tested with and without bridge API key. 

---

### 2.2 Rejection of Missing or Invalid API Key
- **Action:** Modify or clear the Master API Key in the Broker Settings form and click **Test Connection**.
- **Expected Result:** Connection test fails with HTTP 401: `Bridge requires authentication, but no Master API Key is configured in the Broker` or `Bridge rejected the provided Master API Key`.
- **Status:** [ BLOCKED ]
> **Comments / Notes:**  
> unable to clear the Broker Master API Key. There is currently nothing to test that is using the Master API key, so this test is currently blocked. This test should be revisited once the Master API key is being used for testing.

---

## 3. Chapter Artwork Inheritance & Sync

### 3.1 Bridge UI Inspection of Curated Book (Boxcar Children Vol 4)
- **Action:** In Bridge web UI, locate the card playing *The Boxcar Children Volume 4* (`68b4d0eb-1c0d-42fe-84f8-a546c9c223a7`) or inspect its chapter icon list.
- **Expected Result:** The 39 custom icons previously curated in the Broker display in the chapter list without requiring per-slot customization.
- **Status:** [ PASS ]
> **Comments / Notes:**  
> The icons appeared when viewing the "Pictures by Chapter" but have not yet sent it to the Yoto for testing of the display. Currently unclear what Pixel Icon setting should be used when using the custom icons.

---

### 3.2 Physical Yoto Player Playback & 16×16 Pixel Art Display
- **Action:** Insert the physical card for Boxcar Children Vol 4 into a real Yoto player. Skip through tracks (Chapter 1, 2, 3, etc.).
- **Expected Result:** The physical 16×16 LED display renders the corresponding custom chapter icon on each track change.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

### 3.3 Broker 1-Click "🔄 Sync to Bridge" Button (Second Book Test)
- **Action:** In Broker **Library** tab, click **Track Studio** on another multi-chapter ABS book (e.g., *Boxcar Children Vol 1, 2, or 3*). Apply custom icons or number overlays to a few chapters, then click the **🔄 Sync to Bridge** button in the header.
- **Expected Result:** Toast displays `✅ Successfully synced X custom icon(s) to Yoto Bridge item cache.` Icons appear immediately in Bridge storage (`/data/icons/items/<media_id>/`).
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

## 4. Offline Copies & Artwork Upload

### 4.1 Offline Copy with Curated Chapter Artwork
- **Action:** On the Bridge **Cards** tab, choose **Make offline** ("Next 5 hours" or "Whole book") for an Audiobookshelf title that has custom chapter artwork.
- **Expected Result:** The copy process runs; `offline.py` detects item-level cached icons, uploads each 16×16 PNG to Yoto CDN, and associates them with the offline playlist tracks.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

## 5. Extended Yoto Cloud Operations

### 5.1 Full Family Library Exploration (`/api/yoto/cards/all`)
- **Action:** Issue a GET request with `X-Api-Key` to `http://192.168.1.11:8090/api/yoto/cards/all`.
- **Expected Result:** Returns full JSON array of all retail and MYO cards in the Yoto family account, beyond what is currently assigned to local Bridge slots.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

### 5.2 Card Group Assignment (`/api/yoto/cards/{id}/group`)
- **Action:** Fetch available groups via `GET /api/yoto/groups`, then assign a card to a group using `POST /api/yoto/cards/{card_id}/group`.
- **Expected Result:** HTTP 200 OK; the card's group assignment reflects in the official Yoto smartphone app.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

### 5.3 Remote Hardware Volume Control (`/api/yoto/players/{device_id}/volume`)
- **Action:** Send a POST to `http://192.168.1.11:8090/api/yoto/players/<device_id>/volume` with payload `{"volume": 5}` while the player is powered on and online.
- **Expected Result:** The physical Yoto player adjusts its volume level accordingly.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

## 6. Upstream Documentation Review

### 6.1 Review `FORK_ENHANCEMENTS.md`
- **Action:** Review [FORK_ENHANCEMENTS.md](file:///d:/OneDrive/Documents/GitHub/yoto-audiobookshelf-bridge/FORK_ENHANCEMENTS.md) for completeness, accuracy, and tone before sharing with `mylacc`.
- **Expected Result:** Technical details, feature comparison table, and recommended PR breakdown are clear and aligned with your expectations.
- **Status:** [ ]
> **Comments / Notes:**  
> 

---

## 7. Issues Discovered During Testing

| Issue # | Component | Description / Observation | Resolution / Action Item |
| :--- | :--- | :--- | :--- |
| 1 | | | |
| 2 | | | |
| 3 | | | |
