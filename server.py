"""
Runs the PB tracker: serves index.html (and its JSON data file), relays
live category data from HD Toolbox, and reads Spelunky.exe's own live
game state directly to build per-area split data - all from one process
on one port.

Why a relay is needed for category data: HD Toolbox's own websocket server
(ws://localhost:4225/ws/category) only accepts connections whose Origin
header is exactly "http://localhost:4225" - a built-in anti-DNS-rebinding
check. A page served from anywhere else (including this one, on a
different port) gets rejected by browser-enforced rules before it ever
reaches our code. A plain script has no such restriction (only browsers
auto-attach and lock down the Origin header), so this script connects to
HD Toolbox itself and re-broadcasts what it receives to the page over a
connection with no origin restriction of its own.

The per-area split feed is NOT relayed from HD Toolbox's own Run Tracker
(ws://localhost:4225/ws/session) - see game_reader.py's own docstring for
why: that tracker only records an area as complete when a poll happens to
catch one specific, narrow screen state, and silently (and permanently)
drops it otherwise. game_reader.py reads the game's own live state
directly instead, with that specific bug fixed.

Usage: double-click run.bat, or `python server.py`. Then open
http://localhost:4226 in a browser (or add it as an OBS browser source).
Leave this window open while you play.
"""

import asyncio
import json
import mimetypes
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import pystray
import websockets
from PIL import Image
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

import mossranking

try:
    import game_reader
except Exception as e:  # pragma: no cover - defensive only
    # A broken/missing pymem install (wrong VC++ redistributable, etc.)
    # shouldn't take down category tracking and everything else - just
    # run without live per-area splits, the same as HD Toolbox's own Run
    # Tracker simply not being open used to behave.
    game_reader = None
    print(f"Live per-area splits unavailable (game_reader failed to load: {e})")

mimetypes.add_type("image/webp", ".webp")  # not registered on all Python builds

HD_TOOLBOX_WS = "ws://localhost:4225/ws/category"
HD_TOOLBOX_ORIGIN = "http://localhost:4225"
HOST = "localhost"
PORT = 4226
WS_PATH = "/ws"

# Bumped on every tagged GitHub release - the single source of truth both
# for what's shown next to the dashboard's own title and for what
# "CheckForUpdate" compares against the latest release tag.
VERSION = "1.4.0"
GITHUB_REPO = "groomp/spelunky-pb-tracker"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_USER_AGENT = "PBTracker-UpdateCheck"

# When bundled by PyInstaller (--onefile), bundled read-only assets
# (index.html, reference_splits.json, ...) are extracted to a temp dir at
# sys._MEIPASS, not next to the exe - but files we write at runtime
# (exported_pbs.json) need to live next to the actual exe so they survive
# after the temp dir is cleaned up on exit. Plain `python server.py` has
# neither distinction: both are just this script's own directory.
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).parent
else:
    BUNDLE_DIR = Path(__file__).parent
    APP_DIR = BUNDLE_DIR

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/dev.html": "dev.html",
    "/reference_splits.json": "reference_splits.json",
    "/preview_bg.webp": "preview_bg.webp",
    "/frog.ico": "frog.ico",
}

clients = set()
# Tracked separately per upstream feed - a single shared "latest message"
# would let whichever feed happened to update last clobber the other, so a
# newly-connecting client could miss its initial category OR session state.
latest_category_message = None
latest_session_message = None
EXPORT_PATH = APP_DIR / "exported_pbs.json"
# Same idea as EXPORT_PATH, for hand-tweaked WR Splits overrides instead of
# PBs - see the "SaveWrSplits" handler below.
EXPORT_PATH_WR = APP_DIR / "exported_wr_splits.json"
# Overwritten by a "Refresh World Records" scrape (see handle_client_message)
# - served in place of the bundled world_records.json whenever it exists,
# so a refresh survives this run without needing to touch the bundled
# (and, when frozen, not reliably writable) copy.
WORLD_RECORDS_CACHE_PATH = APP_DIR / "world_records_cache.json"


def serve_static(path):
    filename = STATIC_FILES.get(path)
    if filename is None:
        return Response(404, "Not Found", Headers(), b"Not found")
    data = (BUNDLE_DIR / filename).read_bytes()
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(data))
    return Response(200, "OK", headers, data)


def serve_exported_pbs():
    # Unlike STATIC_FILES (bundled, read-only, served from BUNDLE_DIR),
    # this is written at runtime into APP_DIR - every connecting client
    # fetches it on load (see loadServerPBs() in index.html) so a browser
    # that never had PB data saved into ITS OWN localStorage - most
    # notably OBS's browser source, a separate browser engine from
    # whatever you use for the dashboard - still starts from whatever was
    # last saved anywhere, instead of empty.
    data = EXPORT_PATH.read_bytes() if EXPORT_PATH.exists() else b"{}"
    headers = Headers()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(data))
    return Response(200, "OK", headers, data)


def serve_exported_wr_splits():
    # Same idea as serve_exported_pbs, for hand-tweaked WR Splits overrides
    # instead of PBs - with one difference: WR splits are a shared,
    # community fact (not personal data like a PB), so a fresh install
    # with no local override yet falls back to default_wr_splits.json - a
    # bundled, read-only snapshot of real WR splits shipped with the exe -
    # instead of empty. The moment a player edits any WR split locally,
    # EXPORT_PATH_WR gets created with the full set (defaults plus their
    # own edit - see saveWrSplits' own push, which always sends the whole
    # object) and takes over from then on, same as any other local
    # override.
    if EXPORT_PATH_WR.exists():
        data = EXPORT_PATH_WR.read_bytes()
    else:
        default_path = BUNDLE_DIR / "default_wr_splits.json"
        data = default_path.read_bytes() if default_path.exists() else b"{}"
    headers = Headers()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(data))
    return Response(200, "OK", headers, data)


def serve_world_records():
    # Prefers whatever "Refresh World Records" last scraped (see
    # handle_client_message) over the bundled baseline - same reasoning as
    # serve_exported_pbs, just read-through instead of always-runtime since
    # most runs never trigger a refresh at all.
    if WORLD_RECORDS_CACHE_PATH.exists():
        data = WORLD_RECORDS_CACHE_PATH.read_bytes()
    else:
        data = (BUNDLE_DIR / "world_records.json").read_bytes()
    headers = Headers()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(data))
    return Response(200, "OK", headers, data)


def read_world_records_groups():
    """Whatever /world_records.json would currently serve, parsed - the
    baseline a refresh's diff is computed against."""
    try:
        path = WORLD_RECORDS_CACHE_PATH if WORLD_RECORDS_CACHE_PATH.exists() else (BUNDLE_DIR / "world_records.json")
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_wr_splits_override():
    """The local WR splits override (exported_wr_splits.json) if one
    exists, parsed - {} otherwise. Used by RefreshWorldRecords to
    backfill it from the bundled defaults without reading through
    serve_exported_wr_splits's own bundled-fallback behavior, which
    would mask whether a (possibly near-empty) local override already
    exists."""
    if not EXPORT_PATH_WR.exists():
        return {}
    try:
        return json.loads(EXPORT_PATH_WR.read_text(encoding="utf-8"))
    except Exception:
        return {}


def diff_world_records(old_groups, new_groups):
    """Flattens both (category names are unique across groups) and reports
    every category that's new or whose WR time/player changed since the
    last scrape - what the "Refresh World Records" button's status line
    summarizes."""
    def flatten(groups):
        flat = {}
        for entries in groups.values():
            flat.update(entries)
        return flat

    old_flat = flatten(old_groups)
    new_flat = flatten(new_groups)
    changes = []
    for category, entry in new_flat.items():
        old_entry = old_flat.get(category)
        if old_entry is None:
            changes.append({
                "category": category, "kind": "new",
                "time": entry.get("time"), "player": entry.get("player"),
            })
        elif old_entry.get("time") != entry.get("time") or old_entry.get("player") != entry.get("player"):
            changes.append({
                "category": category, "kind": "updated",
                "oldTime": old_entry.get("time"), "oldPlayer": old_entry.get("player"),
                "time": entry.get("time"), "player": entry.get("player"),
            })
    return changes


def _parse_version(version_str):
    """"v1.2.3" or "1.2.3" -> (1, 2, 3), for a simple tuple comparison -
    good enough since every tag this project has ever used follows that
    exact shape."""
    cleaned = version_str.strip().lstrip("vV")
    parts = []
    for p in cleaned.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def _fetch_latest_release():
    """Hits GitHub's own "latest release" API - blocking, so only ever
    called via asyncio.to_thread. Returns (tag_name, html_url, asset_url)
    for the PBTracker.exe asset, raising on any failure (no release, no
    matching asset, network error, ...)."""
    req = urllib.request.Request(
        GITHUB_LATEST_RELEASE_URL,
        headers={"User-Agent": GITHUB_USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag_name = data["tag_name"]
    html_url = data.get("html_url", f"https://github.com/{GITHUB_REPO}/releases/latest")
    asset_url = None
    for asset in data.get("assets", []):
        if asset.get("name") == "PBTracker.exe":
            asset_url = asset.get("browser_download_url")
            break
    if asset_url is None:
        raise ValueError("Latest release has no PBTracker.exe asset")
    return tag_name, html_url, asset_url


def _download_and_apply_update(asset_url):
    """Downloads the new exe next to the running one, then spawns a
    detached helper .bat that waits for THIS process to exit (Windows
    won't let anything overwrite an exe file while it's still running as
    a process), swaps it into place, relaunches it, and deletes itself.
    Only ever called when frozen (getattr(sys, "frozen", False)) -
    "the running exe" is meaningless otherwise."""
    current_exe = Path(sys.executable)
    new_exe = current_exe.with_name(current_exe.stem + "_new.exe")
    req = urllib.request.Request(asset_url, headers={"User-Agent": GITHUB_USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        new_exe.write_bytes(r.read())

    helper_bat = current_exe.with_name("_pbtracker_update.bat")
    pid = os.getpid()
    helper_bat.write_text(
        "@echo off\r\n"
        ":waitloop\r\n"
        f"tasklist /FI \"PID eq {pid}\" 2>NUL | find \"{pid}\" >NUL\r\n"
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >NUL\r\n"
        "  goto waitloop\r\n"
        ")\r\n"
        f"move /Y \"{new_exe}\" \"{current_exe}\"\r\n"
        f"start \"\" \"{current_exe}\"\r\n"
        "del \"%~f0\"\r\n",
        encoding="utf-8",
    )
    # CREATE_NO_WINDOW (a hidden console), not DETACHED_PROCESS (no console
    # at all) - confirmed by testing that tasklist | find inside the bat
    # above just hangs forever with no console to run in whatsoever.
    subprocess.Popen(
        ["cmd.exe", "/c", str(helper_bat)],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


async def refresh_world_records():
    """The actual work behind the "Refresh World Records" button - also
    run as part of applying a self-update (see the "ApplyUpdate" handler)
    so a fresh install always starts from current WR times/splits instead
    of whatever was last cached, without the player needing to remember to
    click Refresh afterward too. Returns a dict matching the
    "WorldRecordsRefreshed" websocket message shape."""
    old_groups = read_world_records_groups()
    # Blocking network I/O (6 category-page fetches) - off the event loop
    # for the same reason as SyncMossRanking's own scrape.
    new_groups = await asyncio.to_thread(mossranking.scrape_world_records)
    changes = diff_world_records(old_groups, new_groups)
    WORLD_RECORDS_CACHE_PATH.write_text(json.dumps(new_groups, indent=2))

    # Backfills anything missing from the local WR splits override
    # (exported_wr_splits.json) using the bundled defaults, same
    # category-by-category, key-by-key gap-filling as mergeMossRankingData
    # does for PBs - never touches a category/key the override already has
    # (an older local file with an empty {} in it - created the moment
    # this exact refresh last ran on an older version and found ANY WR
    # change worth clearing an override for - would otherwise permanently
    # shadow a newer, better bundled default forever, since the override
    # always wins once the file exists at all, no matter how old).
    wr_splits_added = 0
    wr_splits = read_wr_splits_override()
    default_path = BUNDLE_DIR / "default_wr_splits.json"
    if default_path.exists():
        try:
            defaults = json.loads(default_path.read_text(encoding="utf-8"))
        except Exception:
            defaults = {}
        for category, default_splits in defaults.items():
            current = wr_splits.setdefault(category, {})
            for key, value in default_splits.items():
                if key not in current:
                    current[key] = value
                    wr_splits_added += 1
            if not current:
                del wr_splits[category]
    if wr_splits_added:
        EXPORT_PATH_WR.write_text(json.dumps(wr_splits, indent=2))

    result = {"ok": True, "groups": new_groups, "changes": changes}
    if wr_splits_added:
        result["wrSplits"] = wr_splits
    return result


async def process_request(connection, request):
    # request.path includes the query string (e.g. "/?transparent"); strip it
    # before matching routes/files, which only care about the path itself.
    path = request.path.split("?", 1)[0]
    if path == WS_PATH:
        return None  # let the websocket handshake proceed
    if path == "/exported_pbs.json":
        return serve_exported_pbs()
    if path == "/exported_wr_splits.json":
        return serve_exported_wr_splits()
    if path == "/world_records.json":
        return serve_world_records()
    return serve_static(path)


async def handle_client_message(websocket, message):
    try:
        msg = json.loads(message)
    except ValueError:
        return
    msg_type = msg.get("type")

    if msg_type == "SavePBs":
        try:
            data = msg.get("data", {})
            EXPORT_PATH.write_text(json.dumps(data, indent=2))
            print(f"Saved PB data to {EXPORT_PATH.name}")
            await websocket.send(json.dumps({"type": "SaveAck", "ok": True}))
            # Every OTHER connected client - e.g. an OBS browser source,
            # which has its own separate localStorage from wherever this
            # save came from - gets the change live instead of only the
            # next time it happens to reload and fetch exported_pbs.json.
            others = clients - {websocket}
            if others:
                update_msg = json.dumps({"type": "PBsUpdated", "data": data})
                await asyncio.gather(
                    *(client.send(update_msg) for client in others), return_exceptions=True
                )
        except Exception as e:
            print(f"Failed to save PB data: {e}")
            await websocket.send(json.dumps({"type": "SaveAck", "ok": False, "error": str(e)}))

    elif msg_type == "SaveWrSplits":
        # Same idea as SavePBs just above, for hand-tweaked WR Splits
        # overrides instead of PBs.
        try:
            data = msg.get("data", {})
            EXPORT_PATH_WR.write_text(json.dumps(data, indent=2))
            print(f"Saved WR split overrides to {EXPORT_PATH_WR.name}")
            await websocket.send(json.dumps({"type": "SaveAck", "ok": True}))
            others = clients - {websocket}
            if others:
                update_msg = json.dumps({"type": "WrSplitsUpdated", "data": data})
                await asyncio.gather(
                    *(client.send(update_msg) for client in others), return_exceptions=True
                )
        except Exception as e:
            print(f"Failed to save WR split overrides: {e}")
            await websocket.send(json.dumps({"type": "SaveAck", "ok": False, "error": str(e)}))

    elif msg_type == "LoadDefaultWrSplits":
        # Destructive, unlike RefreshWorldRecords' own backfill (which
        # only ever fills gaps and never overwrites a value that's
        # already there, local edits included) - this throws out whatever
        # WR splits override exists on this machine entirely, hand-typed/
        # estimated ones included, and replaces it outright with
        # whatever's bundled as THIS version's own default_wr_splits.json.
        # For players stuck on a stale/wrong split set an old version
        # already wrote to their own override file, which Refresh alone
        # can never touch since every value in it already "exists".
        print("Loading official WR splits (full overwrite)...")
        try:
            default_path = BUNDLE_DIR / "default_wr_splits.json"
            defaults = json.loads(default_path.read_text(encoding="utf-8")) if default_path.exists() else {}
            EXPORT_PATH_WR.write_text(json.dumps(defaults, indent=2))
            print(f"  -> wrote {len(defaults)} categories to {EXPORT_PATH_WR.name}")
            await websocket.send(json.dumps({"type": "WrSplitsLoaded", "ok": True, "data": defaults}))
            others = clients - {websocket}
            if others:
                update_msg = json.dumps({"type": "WrSplitsUpdated", "data": defaults})
                await asyncio.gather(
                    *(client.send(update_msg) for client in others), return_exceptions=True
                )
        except Exception as e:
            print(f"  -> load failed: {e}")
            await websocket.send(json.dumps({"type": "WrSplitsLoaded", "ok": False, "error": str(e)}))

    elif msg_type == "SyncMossRanking":
        profile_input = msg.get("profile", "")
        print(f"Syncing Moss Ranking profile: {profile_input!r}")
        try:
            # resolve_user_id and scrape_profile both do blocking network
            # I/O (resolving a username takes a request of its own) - run
            # them off the event loop so the HD Toolbox relay and other
            # clients aren't stalled while they fetch.
            user_id = await asyncio.to_thread(mossranking.resolve_user_id, profile_input)
            pbs, username = await asyncio.to_thread(mossranking.scrape_profile, user_id)
            print(f"  -> {len(pbs)} categories found for user ID {user_id} ({username!r})")
            await websocket.send(json.dumps({
                "type": "MossRankingSynced", "ok": True, "userId": user_id,
                "username": username, "pbs": pbs,
            }))
        except Exception as e:
            print(f"  -> sync failed: {e}")
            await websocket.send(json.dumps({
                "type": "MossRankingSynced", "ok": False, "error": str(e),
            }))

    elif msg_type == "RefreshWorldRecords":
        print("Refreshing World Records from Moss Ranking...")
        try:
            result = await refresh_world_records()
            changes = result["changes"]
            wr_splits_added = "wrSplits" in result
            print(f"  -> refreshed, {len(changes)} change(s), {'some' if wr_splits_added else 'no'} split(s) backfilled")
            await websocket.send(json.dumps({"type": "WorldRecordsRefreshed", **result}))
        except Exception as e:
            print(f"  -> refresh failed: {e}")
            await websocket.send(json.dumps({
                "type": "WorldRecordsRefreshed", "ok": False, "error": str(e),
            }))

    elif msg_type == "CheckForUpdate":
        try:
            tag_name, html_url, asset_url = await asyncio.to_thread(_fetch_latest_release)
            latest_version = tag_name.lstrip("vV")
            available = _parse_version(latest_version) > _parse_version(VERSION)
            print(f"Update check: current={VERSION} latest={latest_version} available={available}")
            await websocket.send(json.dumps({
                "type": "UpdateCheckResult", "ok": True,
                "currentVersion": VERSION, "latestVersion": latest_version,
                "available": available, "releaseUrl": html_url, "assetUrl": asset_url,
            }))
        except Exception as e:
            print(f"Update check failed: {e}")
            await websocket.send(json.dumps({
                "type": "UpdateCheckResult", "ok": False, "error": str(e), "currentVersion": VERSION,
            }))

    elif msg_type == "ApplyUpdate":
        asset_url = msg.get("assetUrl")
        if not getattr(sys, "frozen", False):
            await websocket.send(json.dumps({
                "type": "UpdateApplyResult", "ok": False,
                "error": "Self-update only works in the packaged .exe, not when running from source.",
            }))
            return
        if not asset_url:
            await websocket.send(json.dumps({
                "type": "UpdateApplyResult", "ok": False, "error": "No download URL given.",
            }))
            return
        try:
            # Same refresh the "Refresh World Records" button triggers -
            # run here too so the override/cache files next to the exe
            # (which survive the swap below untouched) are already current
            # by the time the updated exe starts, instead of leaving
            # whatever was last cached to silently shadow fresh data again.
            # Best-effort: a refresh failure (offline, MossRanking down)
            # shouldn't block installing the update itself.
            try:
                await refresh_world_records()
                print("  -> World Records refreshed as part of the update")
            except Exception as e:
                print(f"  -> World Records refresh skipped (update continues): {e}")

            print(f"Downloading update from {asset_url} ...")
            await asyncio.to_thread(_download_and_apply_update, asset_url)
            # The helper script is now waiting for this exact process to
            # exit before it can swap the exe file - tell the page the
            # update is on its way, then exit so that can happen.
            await websocket.send(json.dumps({"type": "UpdateApplyResult", "ok": True}))
            await asyncio.sleep(0.5)  # give the send above a moment to actually flush
            os._exit(0)
        except Exception as e:
            print(f"Update apply failed: {e}")
            await websocket.send(json.dumps({
                "type": "UpdateApplyResult", "ok": False, "error": str(e),
            }))


async def ws_handler(websocket):
    if websocket.request.path != WS_PATH:
        return
    clients.add(websocket)
    print(f"PB tracker page connected ({len(clients)} connected)")
    try:
        if latest_category_message is not None:
            await websocket.send(latest_category_message)
        if latest_session_message is not None:
            await websocket.send(latest_session_message)
        async for message in websocket:
            await handle_client_message(websocket, message)
    finally:
        clients.discard(websocket)
        print(f"PB tracker page disconnected ({len(clients)} connected)")


async def broadcast(message, which):
    global latest_category_message, latest_session_message
    if which == "category":
        latest_category_message = message
    else:
        latest_session_message = message
    if clients:
        await asyncio.gather(
            *(client.send(message) for client in clients), return_exceptions=True
        )


async def upstream_loop(url, which, label):
    """Relays an HD Toolbox feed (just category now - see module docstring
    for why session/per-area splits reads the game's own live state
    directly instead) to every connected pb_tracker client - see
    broadcast() and ws_handler(). Retries every 2s while HD Toolbox isn't
    open."""
    announced_waiting = False
    while True:
        try:
            async with websockets.connect(url, origin=HD_TOOLBOX_ORIGIN) as ws:
                print(f"Connected to HD Toolbox {label}")
                announced_waiting = False
                async for message in ws:
                    await broadcast(message, which)
        except Exception:
            # Print this once, not on every retry - HD Toolbox may just not
            # be open yet, which is normal at launch, not an error to spam.
            if not announced_waiting:
                print(f"Waiting for {label} (HD Toolbox) to open...")
                announced_waiting = True
        await asyncio.sleep(2)


async def session_poll_loop():
    """Local replacement for HD Toolbox's own Run Tracker feed (see
    game_reader.py and the module docstring above) - pymem's underlying
    Win32 calls are blocking, so the actual polling runs on its own OS
    thread rather than sharing this coroutine's event loop, and hands
    each update back to it thread-safely via run_coroutine_threadsafe.
    A no-op (just parks forever) if game_reader failed to import."""
    if game_reader is None:
        return

    loop = asyncio.get_running_loop()
    stop_event = threading.Event()

    def on_update(run_state):
        message = json.dumps({"type": "Session", "data": {"data": {"run_state": run_state}}})
        asyncio.run_coroutine_threadsafe(broadcast(message, "session"), loop)

    announced = {"status": None}

    def on_status(status):
        # Mirrors upstream_loop's own "don't spam the log" shape above -
        # only prints on an actual transition, not every poll.
        if status != announced["status"]:
            print(f"Run tracker (reading Spelunky.exe's live game state): {status}")
            announced["status"] = status

    threading.Thread(
        target=game_reader.poll_loop, args=(on_update, on_status, stop_event), daemon=True
    ).start()

    # Never returns in normal operation - the thread above does the actual
    # work and reports back via on_update/broadcast; this coroutine just
    # needs to stay alive alongside it for asyncio.gather in main().
    while True:
        await asyncio.sleep(3600)


async def main(started_event):
    async with serve(ws_handler, HOST, PORT, process_request=process_request):
        # Binding succeeded - safe to let the main thread's startup wait
        # continue now, before the (never-returning) relay loop below.
        started_event.set()
        url = f"http://{HOST}:{PORT}"
        print(f"PB tracker running at {url}")
        print("Opening it in your browser now. Leave this window open while you play.")
        print("(Add it as an OBS Browser Source using the URL above plus ?transparent)")
        try:
            webbrowser.open(url)
        except Exception:
            pass  # not fatal - they can still open the URL themselves
        await asyncio.gather(
            upstream_loop(HD_TOOLBOX_WS, "category", "category tracker"),
            session_poll_loop(),
        )


def run_server_thread(started_event, error_holder):
    """Runs the asyncio server on a background thread so the main thread is
    free to run the tray icon's own event/message loop instead - pystray
    needs the main thread for that on Windows."""
    try:
        asyncio.run(main(started_event))
    except OSError as e:
        error_holder["kind"] = "os"
        error_holder["error"] = e
        started_event.set()
    except Exception as e:
        error_holder["kind"] = "other"
        error_holder["error"] = e
        started_event.set()


def load_tray_image():
    ico_path = BUNDLE_DIR / "frog.ico"
    if ico_path.exists():
        return Image.open(ico_path)
    # Fallback so a missing icon file can't take down the tray icon too -
    # a plain colored square still tells you at a glance that it's running.
    return Image.new("RGB", (64, 64), color=(204, 98, 29))


def run_tray_icon():
    url = f"http://{HOST}:{PORT}"

    def open_dashboard(icon, item):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def quit_app(icon, item):
        icon.stop()
        # The server thread is a daemon with no clean-shutdown hook wired
        # up (just an infinite relay loop) - a hard exit is the simplest
        # reliable way to actually close the window on Quit.
        import os
        os._exit(0)

    icon = pystray.Icon(
        "PBTracker",
        load_tray_image(),
        "PB Tracker - running",
        menu=pystray.Menu(
            pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    icon.run()


if __name__ == "__main__":
    started_event = threading.Event()
    error_holder = {}
    server_thread = threading.Thread(
        target=run_server_thread, args=(started_event, error_holder), daemon=True
    )
    server_thread.start()
    started_event.wait()

    if error_holder:
        e = error_holder["error"]
        # The window must never just vanish on error - always show why and
        # wait for a keypress, so a double-clicked .exe doesn't flash shut
        # before anyone can read what went wrong.
        if error_holder["kind"] == "os":
            print(f"\nCouldn't start: {e}")
            if e.errno in (10048, 98) or "already in use" in str(e).lower() or "usage" in str(e).lower():
                print(
                    f"Something is already using port {PORT} - is the PB tracker "
                    "already running (check for another window, or a tray icon)?"
                )
        else:
            print(f"\nUnexpected error: {e}")
        input("\nPress Enter to close...")
    else:
        try:
            run_tray_icon()
        except KeyboardInterrupt:
            pass
