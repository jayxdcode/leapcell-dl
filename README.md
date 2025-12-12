# leapcell-dl


A compact reference for installing, configuring, and running the MP3 downloader + cache service.
It documents the install.sh + run.sh workflow, required environment variables, how the FastAPI endpoint behaves, troubleshooting tips, and security notes.

---

This repository automates a **headless Playwright workflow** running inside a **Leapcell instance**, integrated with a **FastAPI backend**, **MEGA cloud storage (via rclone)**, and **Redis caching**.

It performs the following:

* Launch a headless browser using Playwright
* Navigate to a target site and wait for content loading
* After 2.5 seconds, search the DOM for a button with text `Download`
* Automatically click the button (headless)
* Capture the resulting downloaded file or its resolved URL
* Upload the file to MEGA using rclone
* Store a cached link in Redis
* Serve everything through a FastAPI endpoint: `GET /?id=<string>`

This README covers installation, configuration, workflow, and usage.

---

# 🚀 Features

### ✅ Headless Browser Automation (Playwright)

* Fully compatible with **Leapcell** and **Termux**
* No Chromium dependency required (uses Playwright’s bundled browsers)
* Waits for DOM load, then finds and clicks a button with text `Download`

### ✅ FastAPI Wrapper

* Exposes a simple API endpoint:

```
GET /api/v1/fetch?id=<string>
```

> Root endpoint shows usage info

* ID parameter determines which asset to fetch

### ✅ MEGA Cloud Upload (via rclone)

* Fully automated **non‑interactive** rclone configuration
* Used as file caching backend

### ✅ Redis Caching

* Stores generated MEGA download links
* Prevents re‑scraping and re‑processing
* Extremely fast lookups

---

# 📦 Installation

Clone the repository:

```bash
git clone https://github.com/your/repo.git
cd repo
```

Run the one‑click installer:

```bash
bash install.sh
```

What the script installs:

* Python dependencies (FastAPI, Playwright, Redis client)
* Playwright browsers
* rclone (fully non‑interactive)
* Redis server

---

# 📂 Repository Structure

```
repo/
├── install.sh          # Full non-interactive installer
├── server.py           # FastAPI backend
├── worker.js           # Playwright automation (Node.js)
├── mega.sh             # rclone MEGA uploader
├── README.md
└── ...
```

---

# 🔧 Configuration

### MEGA Rclone Configuration (Auto‑Generated)

The installer creates a default MEGA remote:

```
[mega]
type = mega
user = your-email-here
pass = encrypted-password-here
```

### Redis

* Installed and started automatically
* Accessible at: `localhost:6379`

---

# 🧠 How the System Works

1. **API call received** → `/api/v1/fetch?id=12345`
2. FastAPI checks **Redis cache**

   * If cached → return MEGA link
3. If not cached:

   * FastAPI spawns the **Node.js Playwright worker**
   * Worker loads the target webpage
   * Waits 2.5 sec
   * Locates the `Download` button by text
   * Clicks it
   * Intercepts the download (buffer or URL)
4. File is uploaded to **MEGA via rclone**
5. MEGA download link is saved to **Redis**
6. API returns:

```json
{ "url": "https://mega.nz/..." }
```

---

# ▶️ Usage

### Start API

```bash
python3 server.py
```

### Example Request

```bash
curl "http://localhost:8000/api/v1/fetch?id=12345"
```

### Example Response

```json
{
  "id": "12345",
  "cached": true,
  "url": "https://mega.nz/file/..."
}
```

---

# ⚙️ install.sh (Generated Overview)

```bash
#!/bin/bash
set -e

# Install Python & requirements
pip install fastapi uvicorn redis playwright

# Install playwright browsers
playwright install chromium

# Install node + deps
npm install

# Install redis
apt install redis-server -y
systemctl enable redis --now

# Install rclone (non-interactive)
curl -L https://rclone.org/install.sh | bash -s -- --no-interaction
```

---

# ☁️ Uploading to MEGA

The worker will call:

```bash
rclone copy /tmp/output mega:/cache/
```

---

# 📌 Redis Commands

### Check Cached IDs

```bash
redis-cli keys "*"
```

### Get Cached Link

```bash
redis-cli get 12345
```

---

# 📝 Notes

* Works in **Leapcell**, **Ubuntu**, **Termux**, and any minimal Linux environment.
* Designed to be fully automated; no manual MEGA login required.
* Can be expanded to support multiple storage providers.

