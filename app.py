# app.py
import os
import asyncio
import time
import pathlib
import urllib.parse
import shutil
import re
import base64
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import aiofiles

app = FastAPI(title="LeapCell downloader + rclone->mega cache")

# Config via env vars
SERVICE_URL_TEMPLATE = os.getenv("SERVICE_URL_TEMPLATE", "https://leapcell.example/item/{id}")
# keep DOWNLOADS_DIR for backwards-compat but not used for persistent writes
DOWNLOADS_DIR = pathlib.Path(os.getenv("DOWNLOADS_DIR", "downloads"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL = int(os.getenv("REDIS_TTL", str(60 * 60 * 24)))  # store cached link 24h by default
BROWSER_EXECUTABLE_PATH = os.getenv("BROWSER_EXECUTABLE_PATH")  # optional path to chromium executable
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "mega")  # rclone remote name
RCLONE_REMOTE_FOLDER = os.getenv("RCLONE_REMOTE_FOLDER", "leapcell_cache")  # folder inside cloud remote
WAIT_MS = float(os.getenv("WAIT_MS", "2500"))  # 2.5s default wait after page load
PLAYWRIGHT_DOWNLOAD_TIMEOUT = int(os.getenv("PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS", "15000"))

# Note: do NOT attempt to mkdir on read-only FS; we will not write persistent files.
# DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)  <-- removed

# Redis client (async)
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Helper: build service url
def make_target_url(id: str) -> str:
    return SERVICE_URL_TEMPLATE.format(id=id)

# Helper: stream bytes to rclone using `rclone rcat` and then get a public link
async def rclone_rstream_upload_bytes(data_bytes: bytes, remote_folder: str, filename: str) -> str:
    """
    Streams data_bytes to 'rclone rcat <remote>:<folder>/<filename>' and then returns rclone link.
    """
    remote_target = f"{RCLONE_REMOTE}:{remote_folder.rstrip('/')}/{filename}"

    # rclone rcat <remote>:path  <-- reads file data from stdin
    proc = await asyncio.create_subprocess_exec(
        "rclone", "rcat", remote_target,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    out, err = await proc.communicate(input=data_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone rcat failed: {err.decode().strip() or 'no stderr'}")

    # now obtain a shareable link
    proc2 = await asyncio.create_subprocess_exec(
        "rclone", "link", remote_target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out2, err2 = await proc2.communicate()
    if proc2.returncode != 0:
        raise RuntimeError(f"rclone link failed: {err2.decode().strip() or 'no stderr'}")

    link = out2.decode().strip()
    return link

# Helper: try to download by clicking the Download button and capture bytes
async def playwright_download_stream(target_url: str, id: str) -> Tuple[str, bytes]:
    """
    Launch Playwright, go to the target_url, wait WAIT_MS, find button with text "Download",
    click it and wait for the response that looks like a downloadable resource.
    Returns (suggested_filename, bytes).
    """
    timestamp = int(time.time())
    async with async_playwright() as p:
        browser_kwargs = {"headless": True}
        if BROWSER_EXECUTABLE_PATH:
            browser_kwargs["executable_path"] = BROWSER_EXECUTABLE_PATH
        browser = await p.chromium.launch(**browser_kwargs, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # navigate
            await page.goto(target_url, wait_until="load", timeout=30000)
        except Exception as e:
            await browser.close()
            raise RuntimeError(f"page.goto failed: {e}")

        # allow page to run JS and render
        await asyncio.sleep(WAIT_MS / 1000)

        # locate the Download control
        locator_candidates = [
            page.locator("button", has_text="Download"),
            page.locator("a", has_text="Download"),
            page.locator('xpath=//button[normalize-space(.)="Download"]'),
            page.locator('xpath=//a[normalize-space(.)="Download"]'),
            page.locator('text="Download"')
        ]

        found_locator = None
        for loc in locator_candidates:
            try:
                count = await loc.count()
            except Exception:
                count = 0
            if count and count > 0:
                found_locator = loc.first
                break

        if not found_locator:
            await browser.close()
            raise HTTPException(status_code=404, detail="Download control with text 'Download' not found on page")

        # Wait for a response that looks like a file (attachment or non-HTML content-type)
        def is_download_response(resp):
            try:
                headers = {k.lower(): v for k, v in resp.headers.items()}
            except Exception:
                headers = {}
            cdisp = headers.get("content-disposition", "")
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            # treat anything that has 'attachment' in content-disposition as a download
            if "attachment" in cdisp.lower():
                return True
            # if content-type is not HTML or JS/CSS, treat as a potential asset (image/zip/mp3/octet-stream)
            if ctype and ctype not in ("text/html", "application/xhtml+xml", "application/javascript", "text/css"):
                return True
            return False

        # start waiting for a matching response while clicking
        try:
            wait_promise = page.wait_for_response(is_download_response, timeout=PLAYWRIGHT_DOWNLOAD_TIMEOUT)
            # trigger the click
            await found_locator.click()
            response = await wait_promise
        except PlaywrightTimeoutError:
            # fallback: maybe the button is a link; try to fetch via href in page context
            href = await found_locator.get_attribute("href")
            if href:
                resolved = urllib.parse.urljoin(page.url, href)
                try:
                    # fetch inside page to preserve cookies and auth
                    fetched = await page.evaluate("""async (u) => {
                        const r = await fetch(u, { method: 'GET', credentials: 'same-origin' });
                        const ok = r.ok;
                        const status = r.status;
                        const disposition = r.headers.get('content-disposition') || '';
                        const filenameMatch = (disposition.match(/filename="?([^\";]+)"?/) || [])[1] || null;
                        const blob = await r.blob();
                        const arr = new Uint8Array(await blob.arrayBuffer());
                        // convert to base64
                        let binary = '';
                        for (let i = 0; i < arr.length; i++) binary += String.fromCharCode(arr[i]);
                        const b64 = btoa(binary);
                        return { ok, status, filename: filenameMatch, base64: b64, contentType: r.headers.get('content-type') || '' };
                    }""", resolved)
                except Exception as e:
                    await browser.close()
                    raise RuntimeError(f"fallback page fetch failed: {e}")

                if not fetched.get("ok"):
                    await browser.close()
                    raise RuntimeError(f"fallback fetch failed status {fetched.get('status')}")
                filename = fetched.get("filename") or f"{id}-{timestamp}"
                b64 = fetched.get("base64")
                data_bytes = base64.b64decode(b64)
                await browser.close()
                return filename, data_bytes
            else:
                await browser.close()
                raise RuntimeError("Click did not trigger a downloadable response and no href found to fetch")

        # If we have a response, get body bytes
        try:
            body = await response.body()
        except Exception as e:
            await browser.close()
            raise RuntimeError(f"failed to read response body: {e}")

        # attempt to get filename from content-disposition header
        headers = {k.lower(): v for k, v in response.headers.items()}
        cdisp = headers.get("content-disposition", "")
        filename = None
        if cdisp:
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', cdisp)
            if m:
                filename = urllib.parse.unquote(m.group(1))

        # fallback to derive filename
        if not filename:
            # try to get from url path
            filename = pathlib.Path(urllib.parse.urlparse(response.url).path).name
            if not filename:
                filename = f"{id}-{timestamp}"

        await browser.close()
        return filename, body

@app.get("/")
async def root(id: str = Query(..., description="ID to pass to service template")):
    if not id:
        raise HTTPException(status_code=400, detail="id parameter required")

    cache_key = f"leapcell:{id}"
    # check cache
    try:
        cached_link = await redis.get(cache_key)
    except Exception as e:
        cached_link = None

    if cached_link:
        return JSONResponse({"id": id, "downloadUrl": cached_link, "cached": True})

    # construct target url
    target_url = make_target_url(id)
    filename = None
    data_bytes = None
    try:
        # run playwright to get filename and bytes (no file writes)
        filename, data_bytes = await playwright_download_stream(target_url, id)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"download failed: {e}")

    # upload to mega via rclone rcat (stream)
    try:
        remote_folder = RCLONE_REMOTE_FOLDER.rstrip("/")
        link = await rclone_rstream_upload_bytes(data_bytes, remote_folder, filename)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rclone/upload failed: {e}")

    # store in redis
    try:
        await redis.set(cache_key, link, ex=REDIS_TTL)
    except Exception as e:
        # warn but continue
        print("warning: redis set failed", e)

    return JSONResponse({"id": id, "downloadUrl": link, "cached": False})
