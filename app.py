# app.py
import os
import asyncio
import time
import pathlib
import urllib.parse
import shutil
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import aiofiles

app = FastAPI(title="LeapCell downloader + rclone->mega cache")

# Config via env vars
SERVICE_URL_TEMPLATE = os.getenv("SERVICE_URL_TEMPLATE", "https://leapcell.example/item/{id}")
DOWNLOADS_DIR = pathlib.Path(os.getenv("DOWNLOADS_DIR", "downloads"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL = int(os.getenv("REDIS_TTL", str(60 * 60 * 24)))  # store cached link 24h by default
BROWSER_EXECUTABLE_PATH = os.getenv("BROWSER_EXECUTABLE_PATH")  # optional path to chromium executable
RCLONE_REMOTE = os.getenv("RCLONE_REMOTE", "mega")  # rclone remote name
RCLONE_REMOTE_FOLDER = os.getenv("RCLONE_REMOTE_FOLDER", "leapcell_cache")  # folder inside cloud remote
WAIT_MS = float(os.getenv("WAIT_MS", "2500"))  # 2.5s default wait after page load
PLAYWRIGHT_DOWNLOAD_TIMEOUT = int(os.getenv("PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS", "15000"))

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Redis client (async)
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Helper: build leapcell url
def make_target_url(id: str) -> str:
    return SERVICE_URL_TEMPLATE.format(id=id)

# Helper: shell out to rclone: copy local file to remote and then produce a public link
async def rclone_upload_and_link(local_path: str, remote_folder: str, filename: str) -> str:
    """
    Uses rclone to copy the file and to produce a public link.
    Expects rclone remote configured (RCLONE_REMOTE).
    """
    remote_target = f"{RCLONE_REMOTE}:{remote_folder}/{filename}"

    # copy the file (copyto to preserve filename)
    copy_cmd = ["rclone", "copyto", local_path, remote_target]
    proc = await asyncio.create_subprocess_exec(*copy_cmd,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copyto failed: {err.decode().strip()}")

    # create/get shareable link
    link_cmd = ["rclone", "link", remote_target]
    proc2 = await asyncio.create_subprocess_exec(*link_cmd,
                                                 stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
    out2, err2 = await proc2.communicate()
    if proc2.returncode != 0:
        raise RuntimeError(f"rclone link failed: {err2.decode().strip()}")

    link = out2.decode().strip()
    return link

# Helper: try to download by clicking the Download button
async def playwright_download(target_url: str, id: str) -> pathlib.Path:
    """
    Launch Playwright, go to the target_url, wait 2.5s, find button with text "Download",
    click it and wait for the download event. Returns local saved path.
    """
    timestamp = int(time.time())
    async with async_playwright() as p:
        browser_kwargs = {"headless": True}
        if BROWSER_EXECUTABLE_PATH:
            browser_kwargs["executable_path"] = BROWSER_EXECUTABLE_PATH
        # common args for headless environments
        browser = await p.chromium.launch(**browser_kwargs, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            # go to page
            await page.goto(target_url, wait_until="load", timeout=30000)
        except Exception as e:
            # continue, maybe page still usable
            await browser.close()
            raise RuntimeError(f"page.goto failed: {e}")

        # wait for content to settle
        await asyncio.sleep(WAIT_MS / 1000)

        # Try common strategies for locating the download control
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

        # Prefer the Playwright download API: expect_download then click.
        local_file_path = None
        try:
            # this waits for download to start and returns a Download object
            async with page.expect_download(timeout=PLAYWRIGHT_DOWNLOAD_TIMEOUT) as download_info:
                await found_locator.click()
            download = await download_info.value
            suggested = download.suggested_filename or f"download-{id}-{timestamp}"
            local_target = DOWNLOADS_DIR / f"{id}-{timestamp}-{suggested}"
            # ensure directory exists
            local_target_parent = local_target.parent
            local_target_parent.mkdir(parents=True, exist_ok=True)
            await download.save_as(str(local_target))
            local_file_path = local_target
        except PlaywrightTimeoutError:
            # fallback: maybe the button is actually a link (href). Try to get href and download via page context fetch.
            href = await found_locator.get_attribute("href")
            if href:
                resolved = urllib.parse.urljoin(page.url, href)
                # perform a direct request in the page context to preserve cookies
                try:
                    resp_text = await page.evaluate("""async (u) => {
                        const r = await fetch(u, {method: 'GET', credentials: 'same-origin'});
                        const blob = await r.blob();
                        const arr = new Uint8Array(await blob.arrayBuffer());
                        // return as base64 string
                        let binary = '';
                        for (let i = 0; i < arr.length; i++) binary += String.fromCharCode(arr[i]);
                        return { ok: r.ok, status: r.status, filename: (r.headers.get('content-disposition') || '').match(/filename="?(.*)"?/)?.[1] || null, base64: btoa(binary) };
                    }""", resolved)
                except Exception as e:
                    await browser.close()
                    raise RuntimeError(f"fallback page fetch failed: {e}")

                if not resp_text.get("ok"):
                    await browser.close()
                    raise RuntimeError(f"fallback fetch failed status {resp_text.get('status')}")

                filename = resp_text.get("filename") or f"{id}-{timestamp}"
                b64 = resp_text.get("base64")
                local_target = DOWNLOADS_DIR / filename
                async with aiofiles.open(local_target, "wb") as f:
                    await f.write(b64.encode("ascii"))  # base64 bytes
                local_file_path = local_target
            else:
                await browser.close()
                raise RuntimeError("Click did not trigger a downloadable response and no href found to fetch")

        await browser.close()
        return pathlib.Path(local_file_path)

@app.get("/")
async def root(id: str = Query(..., description="ID to pass to leapcell template")):
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
    local_path = None
    try:
        # run playwright download
        local_path = await playwright_download(target_url, id)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"download failed: {e}")

    # upload to mega via rclone
    filename = local_path.name
    try:
        remote_folder = RCLONE_REMOTE_FOLDER.rstrip("/")
        link = await rclone_upload_and_link(str(local_path), remote_folder, filename)
    except Exception as e:
        # cleanup local file on failure
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"rclone/upload failed: {e}")

    # store in redis
    try:
        await redis.set(cache_key, link, ex=REDIS_TTL)
    except Exception as e:
        # warn but continue
        print("warning: redis set failed", e)

    # optional: cleanup local file after upload
    try:
        local_path.unlink(missing_ok=True)
    except Exception:
        pass

    return JSONResponse({"id": id, "downloadUrl": link, "cached": False})
