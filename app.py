import asyncio
import base64
import collections
import json
import logging
import os
import random
import re
import shlex
import tempfile
from pathlib import Path

from aiohttp import WSMsgType, web

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = Path(os.environ.get("DATA_DIR", BASE / "data"))
PORT = int(os.environ.get("PORT", "8765"))
PASSWORD = os.environ.get("PASSWORD", "")
ADB = os.environ.get("ADB", "adb")
SCRCPY_VERSION = os.environ.get("SCRCPY_VERSION", "3.3.4")
SCRCPY_LOCAL = Path(os.environ.get("SCRCPY_SERVER", BASE / "server" / f"scrcpy-server-v{SCRCPY_VERSION}"))
SCRCPY_REMOTE = f"/data/local/tmp/scrcpy-server-v{SCRCPY_VERSION}.jar"

log = logging.getLogger("adb-remote")
DEVICES_FILE = DATA / "devices.json"
push_locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
# one live stream per device: a second viewer takes over instead of starting another encoder
sessions: dict[str, "Session"] = {}
SEND_BUFFER = 512 * 1024
MAX_LAG = 1.0  # seconds a frame may stay unacknowledged by the viewer before we start dropping
ACK_EVERY = 5  # the page acks every 5th frame


async def adb(*args, timeout=15, data=None):
    proc = await asyncio.create_subprocess_exec(
        ADB, *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b"timeout"
    return proc.returncode, out


async def adb_text(*args, timeout=15):
    rc, out = await adb(*args, timeout=timeout)
    return rc, out.decode("utf-8", "replace").strip()


def load_saved():
    try:
        return json.loads(DEVICES_FILE.read_text("utf-8"))
    except Exception:
        return []


def save_saved(items):
    DATA.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")


def remember(serial, model=None, name=None, auto=None):
    items = load_saved()
    item = next((d for d in items if d["serial"] == serial), None)
    if item is None:
        item = {"serial": serial}
        items.append(item)
    if model:
        item["model"] = model
    if auto is not None:
        item["auto"] = auto
    if name is not None:
        if name:
            item["name"] = name
        else:
            item.pop("name", None)
    save_saved(items)


def normalize_addr(addr):
    addr = addr.strip()
    if not addr:
        return ""
    if ":" not in addr and "." in addr:
        return addr + ":5555"
    return addr


async def list_devices():
    rc, out = await adb_text("devices", "-l")
    found = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        found[parts[0]] = {"serial": parts[0], "state": parts[1], "model": info.get("model", "").replace("_", " ")}
    saved = load_saved()
    result = []
    for d in saved:
        cur = found.pop(d["serial"], None)
        state = cur["state"] if cur else ("disconnected" if d.get("auto") is False else "offline")
        result.append({"serial": d["serial"], "state": state,
                       "model": (cur and cur["model"]) or d.get("model", ""), "name": d.get("name", ""),
                       "saved": True})
    for d in found.values():
        result.append({**d, "saved": False})
    return result


async def device_state(serial):
    rc, out = await adb_text("-s", serial, "get-state", timeout=5)
    return out if rc == 0 else ("unauthorized" if "unauthorized" in out else "offline")


# ---------------------------------------------------------------- HTTP API

def body_serial(data):
    serial = str(data.get("serial", "")).strip()
    if not serial:
        raise web.HTTPBadRequest(text="serial required")
    return serial


async def api_devices(request):
    return web.json_response(await list_devices())


async def api_connect(request):
    data = await request.json()
    addr = normalize_addr(str(data.get("addr", "")))
    if not addr:
        raise web.HTTPBadRequest(text="addr required")
    rc, out = await adb_text("connect", addr, timeout=12)
    # "failed to authenticate" means the device is now showing the RSA prompt
    ok = "connected to" in out or "failed to authenticate" in out
    state = "offline"
    if ok:
        for _ in range(10):
            state = await device_state(addr)
            if state == "device":
                break
            await asyncio.sleep(0.3)
    model = ""
    if state == "device":
        _, model = await adb_text("-s", addr, "shell", "getprop", "ro.product.model", timeout=5)
    if ok:
        remember(addr, model, auto=True)
    return web.json_response({"ok": ok, "serial": addr, "state": state, "model": model, "message": out})


async def api_save(request):
    data = await request.json()
    serial = normalize_addr(str(data.get("addr", "")))
    if not serial:
        raise web.HTTPBadRequest(text="addr required")
    old = str(data.get("old", "")).strip()
    if old and old != serial:
        items = [d for d in load_saved() if d["serial"] != serial]
        for d in items:
            if d["serial"] == old:
                d["serial"] = serial
                d.pop("model", None)
        save_saved(items)
        if ":" in old:
            await adb_text("disconnect", old)
    remember(serial, name=str(data.get("name", "")).strip()[:40])
    return web.json_response({"ok": True, "serial": serial})


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.task = asyncio.current_task()
        self.released = asyncio.Event()  # set once scrcpy is gone from the device


async def close_session(serial, message):
    """Stop a device's live stream and wait until its encoder has left the device
    (some SoCs, e.g. RK3399, cannot run two encoders at once)."""
    sess = sessions.pop(serial, None)
    if sess is None:
        return
    if not sess.ws.closed:
        try:
            await sess.ws.send_json({"type": "error", "message": message})
        except Exception:
            pass
    sess.task.cancel()
    try:
        await asyncio.wait_for(sess.released.wait(), 8)
    except asyncio.TimeoutError:
        log.warning("%s: previous stream did not stop in time", serial)


async def api_disconnect(request):
    serial = body_serial(await request.json())
    await close_session(serial, "已断开")
    if any(d["serial"] == serial for d in load_saved()):
        remember(serial, auto=False)
    rc, out = await adb_text("disconnect", serial)
    return web.json_response({"ok": rc == 0, "message": out})


async def api_forget(request):
    serial = body_serial(await request.json())
    save_saved([d for d in load_saved() if d["serial"] != serial])
    if ":" in serial:
        await adb_text("disconnect", serial)
    return web.json_response({"ok": True})


async def api_info(request):
    serial = request.query.get("serial", "")
    script = ("getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; "
              "getprop ro.build.version.sdk; wm size | tail -n1; dumpsys battery | grep -m1 level")
    rc, out = await adb_text("-s", serial, "shell", script, timeout=8)
    if rc != 0:
        return web.json_response({"ok": False, "message": out})
    lines = (out.splitlines() + [""] * 6)[:6]
    size = lines[4].split(":")[-1].strip()
    battery = lines[5].split(":")[-1].strip()
    return web.json_response({"ok": True, "brand": lines[0], "model": lines[1], "android": lines[2],
                              "sdk": lines[3], "size": size, "battery": battery})


KEY_RE = re.compile(r"^\d{1,3}$")


async def api_key(request):
    data = await request.json()
    serial = body_serial(data)
    code = str(data.get("code", ""))
    if not KEY_RE.match(code):
        raise web.HTTPBadRequest(text="bad keycode")
    args = ["input", "keyevent"] + (["--longpress"] if data.get("long") else []) + [code]
    rc, out = await adb_text("-s", serial, "shell", *args)
    return web.json_response({"ok": rc == 0, "message": out})


async def api_text(request):
    data = await request.json()
    serial = body_serial(data)
    text = str(data.get("text", ""))
    if not text:
        return web.json_response({"ok": True})
    # `input text` only handles ASCII; spaces must be %s
    safe = shlex.quote(text.replace(" ", "%s"))
    rc, out = await adb_text("-s", serial, "shell", f"input text {safe}")
    return web.json_response({"ok": rc == 0, "message": out})


ACTIONS = {
    "reboot": ["reboot"],
    "shutdown": ["shell", "reboot -p"],
    "recovery": ["reboot", "recovery"],
    "bootloader": ["reboot", "bootloader"],
}


async def api_action(request):
    data = await request.json()
    serial = body_serial(data)
    args = ACTIONS.get(data.get("action"))
    if not args:
        raise web.HTTPBadRequest(text="unknown action")
    rc, out = await adb_text("-s", serial, *args, timeout=10)
    if ":" in serial and data.get("action") != "shutdown":
        # tcp devices drop off during reboot; keep adb from holding a dead transport
        await adb_text("disconnect", serial)
    return web.json_response({"ok": rc in (0, -1), "message": out})


async def api_screenshot(request):
    serial = request.query.get("serial", "")
    rc, out = await adb("-s", serial, "exec-out", "screencap", "-p", timeout=20)
    if rc != 0 or not out.startswith(b"\x89PNG"):
        raise web.HTTPBadGateway(text=out[:300].decode("utf-8", "replace"))
    return web.Response(body=out, content_type="image/png")


async def api_install(request):
    serial = request.query.get("serial", "")
    reader = await request.multipart()
    field = await reader.next()
    if field is None:
        raise web.HTTPBadRequest(text="file required")
    fd, path = tempfile.mkstemp(suffix=".apk")
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await field.read_chunk(1 << 20):
                f.write(chunk)
        rc, out = await adb_text("-s", serial, "install", "-r", "-d", path, timeout=300)
    finally:
        os.unlink(path)
    return web.json_response({"ok": rc == 0 and "Success" in out, "message": out[-400:]})


# ---------------------------------------------------------------- scrcpy stream

async def ensure_server(serial):
    size = SCRCPY_LOCAL.stat().st_size
    async with push_locks[serial]:
        rc, out = await adb_text("-s", serial, "shell", f"stat -c %s {SCRCPY_REMOTE} 2>/dev/null")
        if out.strip() == str(size):
            return
        rc, out = await adb_text("-s", serial, "push", str(SCRCPY_LOCAL), SCRCPY_REMOTE, timeout=60)
        if rc != 0:
            raise RuntimeError(out)


def clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


async def ws_stream(request):
    serial = request.query.get("serial", "")
    # heartbeat reaps half-open sockets (sleeping phones) so the device stops encoding
    ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=15)
    await ws.prepare(request)
    await close_session(serial, "画面已在其他窗口打开")
    sess = sessions[serial] = Session(ws)
    transport = request.transport
    sock = transport.get_extra_info("socket") if transport else None
    if sock is not None:
        try:
            import socket
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
        except OSError:
            pass

    max_size = clamp_int(request.query.get("max_size"), 0, 4096, 1280)
    bit_rate = clamp_int(request.query.get("bit_rate"), 200_000, 50_000_000, 4_000_000)
    max_fps = clamp_int(request.query.get("max_fps"), 1, 120, 30)

    scid = "%08x" % random.getrandbits(31)
    port = None
    proc = None
    writers = []
    tasks = []
    output = collections.deque(maxlen=40)

    async def fail(msg):
        log.warning("%s: %s", serial, msg)
        if not ws.closed:
            await ws.send_json({"type": "error", "message": msg})
            await ws.close()
        return ws

    try:
        try:
            await ensure_server(serial)
        except Exception as e:
            return await fail(f"推送 scrcpy-server 失败：{e}")

        rc, out = await adb_text("-s", serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}")
        if rc != 0 or not out.strip().isdigit():
            return await fail(f"adb forward 失败：{out}")
        port = int(out.strip())

        cmd = (f"CLASSPATH={SCRCPY_REMOTE} app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
               f"scid={scid} log_level=info tunnel_forward=true audio=false control=true cleanup=true "
               f"video_codec=h264 max_size={max_size} video_bit_rate={bit_rate} max_fps={max_fps} "
               f"clipboard_autosync=false")
        proc = await asyncio.create_subprocess_exec(
            ADB, "-s", serial, "shell", cmd,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def read_output():
            while line := await proc.stdout.readline():
                text = line.decode("utf-8", "replace").rstrip()
                output.append(text)
                log.info("[%s] %s", serial, text)

        tasks.append(asyncio.create_task(read_output()))

        async def open_socket(first):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 15
            while loop.time() < deadline:
                if proc.returncode is not None:
                    break
                try:
                    r, w = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    await asyncio.sleep(0.1)
                    continue
                if not first:
                    return r, w
                try:
                    # adb accepts before the device listens; the dummy byte proves the server is up
                    await asyncio.wait_for(r.readexactly(1), 2)
                    return r, w
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
                    w.close()
                    await asyncio.sleep(0.15)
            raise RuntimeError("\n".join(list(output)[-6:]) or "scrcpy-server 启动超时")

        try:
            vr, vw = await open_socket(True)
            writers.append(vw)
            cr, cw = await open_socket(False)
            writers.append(cw)
            name = (await asyncio.wait_for(vr.readexactly(64), 10)).split(b"\0", 1)[0].decode("utf-8", "replace")
            meta = await asyncio.wait_for(vr.readexactly(12), 15)
        except Exception as e:
            return await fail(f"scrcpy 启动失败：{e}")

        codec = meta[:4].decode("ascii", "replace")
        width = int.from_bytes(meta[4:8], "big")
        height = int.from_bytes(meta[8:12], "big")
        await ws.send_json({"type": "meta", "name": name, "codec": codec, "width": width, "height": height})

        # the page acks every few frames ("a<count>"); OS socket buffers hide a slow viewer otherwise
        flow = {"acked": 0, "acking": False}
        inflight = collections.deque()  # (frame number, send time)

        async def pump_video():
            loop = asyncio.get_running_loop()
            sent = 0
            skipping = False
            last_reset = 0.0
            while True:
                hdr = await vr.readexactly(12)
                size = int.from_bytes(hdr[8:12], "big")
                payload = await vr.readexactly(size)
                config, key = hdr[0] & 0x80, hdr[0] & 0x40
                if not config:
                    now = loop.time()
                    while inflight and inflight[0][0] <= flow["acked"]:
                        inflight.popleft()
                    # the last ACK_EVERY frames may legitimately be unacked (acks are batched)
                    lagging = (flow["acking"] and len(inflight) > ACK_EVERY
                               and now - inflight[0][1] > MAX_LAG)
                    backlog = transport.get_write_buffer_size() if transport else 0
                    # rather than queue seconds of latency, drop everything while the viewer is behind,
                    # then resume from a fresh key frame
                    if lagging or backlog > SEND_BUFFER // 2:
                        skipping = True
                        continue
                    if skipping:
                        if not key:
                            if now - last_reset > 1:
                                cw.write(b"\x11")  # RESET_VIDEO -> fresh config + key frame
                                last_reset = now
                            continue
                        skipping = False
                await ws.send_bytes(hdr + payload)
                sent += 1
                inflight.append((sent, loop.time()))

        async def drain_device_msgs():
            while await cr.read(4096):
                pass

        async def pump_control():
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    cw.write(msg.data)
                    await cw.drain()
                elif msg.type == WSMsgType.TEXT and msg.data[:1] == "a" and msg.data[1:].isdigit():
                    flow["acked"] = int(msg.data[1:])
                    flow["acking"] = True
                elif msg.type == WSMsgType.ERROR:
                    break

        live = [asyncio.create_task(pump_video()), asyncio.create_task(drain_device_msgs()),
                asyncio.create_task(pump_control())]
        tasks.extend(live)
        done, _ = await asyncio.wait(live, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (asyncio.IncompleteReadError, ConnectionError)):
                log.warning("%s stream ended: %r", serial, exc)
        if not ws.closed and not any(t is live[2] for t in done):
            await ws.send_json({"type": "error", "message": "画面连接已断开"})
    except asyncio.CancelledError:
        pass  # taken over by another viewer, or the device was disconnected
    except ConnectionError:
        pass
    finally:
        if sessions.get(serial) is sess:
            del sessions[serial]
        for t in tasks:
            t.cancel()
        for w in writers:
            w.close()
        if proc and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
        if port:
            await adb_text("-s", serial, "forward", "--remove", f"tcp:{port}")
        sess.released.set()
        if not ws.closed:
            await ws.close()
    return ws


# ---------------------------------------------------------------- app

@web.middleware
async def auth_middleware(request, handler):
    if PASSWORD:
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                ok = base64.b64decode(header[6:]).decode().split(":", 1)[-1] == PASSWORD
            except Exception:
                pass
        if not ok:
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="ADB Remote"'})
    return await handler(request)


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def on_startup(app):
    await adb_text("start-server", timeout=20)

    async def reconnect():
        for d in load_saved():
            if ":" in d["serial"] and d.get("auto") is not False:
                await adb_text("connect", d["serial"], timeout=8)

    app["reconnect"] = asyncio.create_task(reconnect())


def make_app():
    app = web.Application(middlewares=[auth_middleware], client_max_size=1 << 31)
    app.router.add_get("/", index)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/connect", api_connect)
    app.router.add_post("/api/save", api_save)
    app.router.add_post("/api/disconnect", api_disconnect)
    app.router.add_post("/api/forget", api_forget)
    app.router.add_get("/api/info", api_info)
    app.router.add_post("/api/key", api_key)
    app.router.add_post("/api/text", api_text)
    app.router.add_post("/api/action", api_action)
    app.router.add_get("/api/screenshot", api_screenshot)
    app.router.add_post("/api/install", api_install)
    app.router.add_get("/ws", ws_stream)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(make_app(), port=PORT, access_log=None)
