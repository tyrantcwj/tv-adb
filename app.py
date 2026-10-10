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
import time
from pathlib import Path

from aiohttp import WSMsgType, web

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = Path(os.environ.get("DATA_DIR", BASE / "data"))
PORT = int(os.environ.get("PORT", "8765"))
PASSWORD = os.environ.get("PASSWORD", "")
ADB = os.environ.get("ADB", "adb")
APP_VERSION = "1.1.0"
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


async def api_diag(request):
    serial = request.query.get("serial", "")
    if serial:
        return web.json_response(last_failure.get(serial, {}))
    return web.json_response(last_failure)


async def api_version(request):
    return web.json_response({"version": APP_VERSION, "scrcpy": SCRCPY_VERSION})


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


SCRCPY_CMD = f"CLASSPATH={SCRCPY_REMOTE} app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION}"
# per-device encoder that is known to work, for devices whose default (hardware) encoder fails
encoder_pref: dict[str, str] = {}
# full scrcpy output of the last failed start per device, served by /api/diag for remote debugging
last_failure: dict[str, dict] = {}


def saved_encoder(serial):
    if serial in encoder_pref:
        return encoder_pref[serial]
    return next((d.get("encoder", "") for d in load_saved() if d["serial"] == serial), "")


def set_encoder(serial, encoder):
    encoder_pref[serial] = encoder
    items = load_saved()
    for d in items:
        if d["serial"] == serial:
            if encoder:
                d["encoder"] = encoder
            else:
                d.pop("encoder", None)
            save_saved(items)


ENCODER_RE = re.compile(r"--video-codec=h264 --video-encoder=(\S+)(?:[ \t]+\((\w+)\))?")


async def fallback_encoder(serial, failed):
    """Pick another H.264 encoder, preferring the platform software one.
    Returns (encoder or "", raw scrcpy output for diagnostics)."""
    rc, out = await adb_text("-s", serial, "shell", f"{SCRCPY_CMD} list_encoders=true log_level=info", timeout=20)
    log.info("[%s] encoders:\n%s", serial, out)
    # before Android 10 scrcpy prints no (sw)/(hw) tag; Google's encoders are the software ones
    found = [(name, kind or ("sw" if re.search(r"google|c2\.android", name, re.I) else "hw"))
             for name, kind in ENCODER_RE.findall(out)]
    for want in ("sw", "hw"):
        for name, kind in found:
            if kind == want and name != failed:
                return name, out
    return "", out


def scrcpy_reason(output, exc):
    """The useful part of scrcpy's own log, rather than a bare socket error."""
    lines = [l.replace("[server] ", "") for l in output
             if any(k in l for k in ("ERROR", "WARN", "Exception", "Caused by", "Error:", "rror"))]
    frames = [l.strip() for l in output if l.strip().startswith("at ")][:6]
    if lines and frames:
        lines = lines[-4:] + frames
    if not lines:
        # nothing tagged: show whatever scrcpy printed, minus the routine device banner
        lines = [l.replace("[server] ", "") for l in output if "INFO: Device:" not in l]
    return "\n".join(lines[-10:]) or str(exc) or exc.__class__.__name__


SCREENRECORD = "screenrecord"  # pseudo encoder name: stream via the device's screenrecord binary
FLAG_CONFIG = 1 << 63
FLAG_KEY = 1 << 62


def packet(data, config=False, key=False, pts=0):
    """Same 12-byte framing scrcpy uses, so the page needs one code path."""
    flags = FLAG_CONFIG if config else (pts | (FLAG_KEY if key else 0))
    return flags.to_bytes(8, "big") + len(data).to_bytes(4, "big"), bytes(data)


class AnnexBFramer:
    """Cut a raw H.264 Annex B byte stream into access units (one packet per picture)."""

    def __init__(self):
        self.buf = bytearray()
        self.config = bytearray()
        self.prefix = bytearray()
        self.au = bytearray()
        self.au_key = False
        self.t0 = time.monotonic()
        self.emitted = False
        self.broken = False  # a flush cut a NAL short; the caller should restart for a clean key frame
        self.junk = bytearray()  # non-H.264 output (screenrecord error text)

    def _pts(self):
        return int((time.monotonic() - self.t0) * 1e6)

    def _end_au(self):
        if not self.au:
            return []
        out = [packet(self.au, key=self.au_key, pts=self._pts())]
        self.au = bytearray()
        self.au_key = False
        self.emitted = True
        return out

    def _nal(self, nal):
        t = nal[0] & 0x1F
        out = []
        if t in (7, 8):  # SPS / PPS
            out += self._end_au()
            self.config += b"\0\0\0\1" + nal
        elif t in (1, 5):  # picture slice
            if len(nal) > 1 and nal[1] & 0x80:  # first_mb_in_slice == 0: a new picture starts
                out += self._end_au()
            if self.config:
                out.append(packet(self.config, config=True))
                self.config = bytearray()
            if not self.au and self.prefix:
                self.au += self.prefix
                self.prefix = bytearray()
            self.au += b"\0\0\0\1" + nal
            self.au_key |= t == 5
        elif t in (6, 9):  # SEI / access unit delimiter belong to the next picture
            out += self._end_au()
            self.prefix += b"\0\0\0\1" + nal
        return out

    def _process(self, final):
        data = self.buf
        starts = []
        i = data.find(b"\0\0\1")
        while i != -1:
            starts.append(i)
            i = data.find(b"\0\0\1", i + 3)
        if not starts:
            if final:
                self._discard(len(data))
            return []
        if starts[0] > 0:
            self._discard(starts[0])
        ends = starts[1:] + ([len(data)] if final else [])
        out = []
        for a, b in zip(starts, ends):
            nal = bytes(data[a + 3:b]).rstrip(b"\0")
            if nal:
                out += self._nal(nal)
        del data[:starts[len(ends)] if len(ends) < len(starts) else len(data)]
        if final:
            out += self._end_au()
        return out

    def _discard(self, n):
        if self.emitted and any(self.buf[:n]):
            self.broken = True  # bytes without a start code after we already flushed: the flush was early
        elif len(self.junk) < 2000:
            self.junk += self.buf[:n]

    def feed(self, data):
        self.buf += data
        return self._process(final=False)

    def flush(self):
        return self._process(final=True)


class ScrcpyVideo:
    """Packets straight from scrcpy's video socket."""

    def __init__(self, reader, control_writer):
        self.reader, self.cw = reader, control_writer

    async def next(self):
        hdr = await self.reader.readexactly(12)
        return hdr, await self.reader.readexactly(int.from_bytes(hdr[8:12], "big"))

    def request_key(self):
        self.cw.write(b"\x11")  # RESET_VIDEO -> fresh config + key frame

    async def close(self):
        pass


class ScreenRecordVideo:
    """H.264 from the device's own screenrecord binary.

    For ROMs whose Java MediaCodec cannot be created from the shell user at all, e.g. Hisense VIDAA
    whose MediaCodec.<init> does a SystemProperties.set that shell is not allowed to do. screenrecord
    uses the native codec and never hits that hook. It stops after 3 minutes, so it is respawned.
    """

    TIME_LIMIT = 180
    IDLE_FLUSH = 0.05  # a quiet pipe means the current picture is complete

    def __init__(self, serial, width, height, bit_rate):
        self.serial, self.width, self.height, self.bit_rate = serial, width, height, bit_rate
        self.proc = None
        self.framer = None
        self.queue = collections.deque()
        self.restart = False
        self.quick_exits = 0

    async def _spawn(self):
        await self.close()
        self.framer = AnnexBFramer()
        self.started = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            ADB, "-s", self.serial, "exec-out", "screenrecord", "--output-format=h264",
            f"--size={self.width}x{self.height}", f"--bit-rate={self.bit_rate}",
            f"--time-limit={self.TIME_LIMIT}", "-",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def start(self):
        """Spawn and wait for the first picture, so failures surface before the page is told we're live."""
        await self._spawn()
        first = await asyncio.wait_for(self.next(), 15)
        self.queue.appendleft(first)

    async def next(self):
        while not self.queue:
            if self.proc is None or self.restart or (self.framer and self.framer.broken):
                self.restart = False
                await self._spawn()
            try:
                chunk = await asyncio.wait_for(self.proc.stdout.read(1 << 16), self.IDLE_FLUSH)
            except asyncio.TimeoutError:
                self.queue.extend(self.framer.flush())
                continue
            if chunk:
                self.queue.extend(self.framer.feed(chunk))
                continue
            # screenrecord ended: normally its time limit, otherwise it failed outright
            self.queue.extend(self.framer.flush())
            await self.proc.wait()
            lived = time.monotonic() - self.started
            self.quick_exits = 0 if lived > 10 or self.framer.emitted else self.quick_exits + 1
            if self.quick_exits >= 2:
                text = self.framer.junk.decode("utf-8", "replace").strip()
                raise RuntimeError(f"screenrecord 退出：{text or self.proc.returncode}")
            self.proc = None
        return self.queue.popleft()

    def request_key(self):
        self.restart = True  # a new screenrecord starts with SPS/PPS + IDR

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
        self.proc = None


async def display_size(serial):
    rc, out = await adb_text("-s", serial, "shell", "wm size")
    sizes = {k: (int(w), int(h)) for k, w, h in re.findall(r"(Physical|Override) size:\s*(\d+)x(\d+)", out)}
    return sizes.get("Override") or sizes.get("Physical") or (1920, 1080)


def fit_size(width, height, max_size):
    k = min(1.0, max_size / max(width, height)) if max_size else 1.0
    return max(16, int(width * k) // 16 * 16), max(16, int(height * k) // 16 * 16)


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

    live_state = {"proc": None, "port": None, "reader": None, "source": None}
    writers = []
    tasks = []
    output = collections.deque(maxlen=40)

    async def fail(msg):
        log.warning("%s: %s", serial, msg)
        if not ws.closed:
            await ws.send_json({"type": "error", "message": msg})
            await ws.close()
        return ws

    async def teardown():
        for t in tasks:
            t.cancel()
        tasks.clear()
        if live_state["source"]:
            await live_state["source"].close()
            live_state["source"] = None
        for w in writers:
            w.close()
        writers.clear()
        proc = live_state["proc"]
        if proc and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
        live_state["proc"] = None
        if live_state["port"]:
            await adb_text("-s", serial, "forward", "--remove", f"tcp:{live_state['port']}")
            live_state["port"] = None

    async def collect_exit_log():
        """The video socket drops the moment scrcpy dies, before its stderr has been read."""
        proc, reader = live_state["proc"], live_state["reader"]
        if proc:
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
        if reader:
            try:
                await asyncio.wait_for(asyncio.shield(reader), 2)
            except (asyncio.TimeoutError, Exception):
                pass

    async def start_scrcpy(video_opts, with_video):
        """Start scrcpy-server; returns (video reader or None, control reader, control writer, device name)."""
        output.clear()
        scid = "%08x" % random.getrandbits(31)
        rc, out = await adb_text("-s", serial, "forward", "tcp:0", f"localabstract:scrcpy_{scid}")
        if rc != 0 or not out.strip().isdigit():
            raise RuntimeError(f"adb forward 失败：{out}")
        port = live_state["port"] = int(out.strip())

        cmd = (f"{SCRCPY_CMD} scid={scid} log_level=info tunnel_forward=true audio=false control=true "
               f"cleanup=true clipboard_autosync=false {video_opts}")
        proc = live_state["proc"] = await asyncio.create_subprocess_exec(
            ADB, "-s", serial, "shell", cmd,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def read_output():
            while line := await proc.stdout.readline():
                text = line.decode("utf-8", "replace").rstrip()
                output.append(text)
                log.info("[%s] %s", serial, text)

        live_state["reader"] = asyncio.create_task(read_output())
        tasks.append(live_state["reader"])

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
            raise RuntimeError("scrcpy-server 启动超时")

        vr = None
        if with_video:
            vr, vw = await open_socket(True)
            writers.append(vw)
        # without video the control socket is the first one: it gets the dummy byte and the device name
        cr, cw = await open_socket(not with_video)
        writers.append(cw)
        name = (await asyncio.wait_for((vr or cr).readexactly(64), 10)).split(b"\0", 1)[0].decode("utf-8", "replace")
        return vr, cr, cw, name

    async def launch(encoder, size):
        vr, cr, cw, name = await start_scrcpy(
            f"video_codec=h264 max_size={size} video_bit_rate={bit_rate} max_fps={max_fps}"
            + (f" video_encoder={encoder}" if encoder else ""), True)
        meta = await asyncio.wait_for(vr.readexactly(12), 15)
        width, height = int.from_bytes(meta[4:8], "big"), int.from_bytes(meta[8:12], "big")
        return ScrcpyVideo(vr, cw), cr, cw, name, (width, height), None

    async def launch_screenrecord():
        _, cr, cw, name = await start_scrcpy("video=false", False)
        dw, dh = await display_size(serial)
        width, height = fit_size(dw, dh, max_size)
        source = live_state["source"] = ScreenRecordVideo(serial, width, height, bit_rate)
        await source.start()
        return source, cr, cw, name, (width, height), (dw, dh)

    try:
        try:
            await ensure_server(serial)
        except Exception as e:
            return await fail(f"推送 scrcpy-server 失败：{e}")

        encoder = saved_encoder(serial)
        started = None
        failure = None
        if encoder != SCREENRECORD:
            try:
                # a remembered fallback encoder is usually the software one, which can't keep up above 720p
                started = await launch(encoder, min(max_size or 1280, 1280) if encoder else max_size)
            except Exception as e:
                await collect_exit_log()
                reason = scrcpy_reason(output, e)
                failure = last_failure[serial] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "encoder": encoder,
                                                  "first": list(output) or [repr(e)], "reason": reason}
                await teardown()
                # TV SoCs often ship a hardware encoder scrcpy cannot drive; retry once with another one
                retry, listing = ("", "") if encoder else await fallback_encoder(serial, encoder)
                failure["encoders"] = listing.splitlines()
                if retry or encoder:
                    log.warning("%s: encoder %r failed (%s), retrying with %r", serial, encoder or "default", reason, retry or "default")
                    try:
                        started = await launch(retry, min(max_size or 1280, 1280) if retry else max_size)
                        set_encoder(serial, retry)
                    except Exception as e2:
                        await collect_exit_log()
                        failure["retry"] = retry
                        failure["second"] = list(output) or [repr(e2)]
                        await teardown()
        if started is None:
            # last resort: the device's own screenrecord (native codec) for video, scrcpy only for input
            try:
                started = await launch_screenrecord()
            except Exception as e3:
                await collect_exit_log()
                await teardown()
                if failure is None:
                    failure = last_failure[serial] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "encoder": encoder}
                failure["screenrecord"] = list(output) + [str(e3) or repr(e3)]
                if encoder == SCREENRECORD:
                    set_encoder(serial, "")  # retry the normal path next time
                head = f"scrcpy 启动失败：{failure['reason']}\n\n" if failure.get("reason") else ""
                return await fail(f"{head}改用系统录屏也失败：{str(e3) or repr(e3)}")
            if encoder != SCREENRECORD:
                log.warning("%s: using screenrecord fallback", serial)
                set_encoder(serial, SCREENRECORD)
        if failure is not None and started is not None:
            last_failure.pop(serial, None)
        source, cr, cw, name, (width, height), touch = started

        msg = {"type": "meta", "name": name, "codec": "h264", "width": width, "height": height}
        if touch:
            # scrcpy has no video to map against, so it takes raw display coordinates
            msg.update(touchWidth=touch[0], touchHeight=touch[1], mode=SCREENRECORD)
        await ws.send_json(msg)

        # the page acks every few frames ("a<count>"); OS socket buffers hide a slow viewer otherwise
        flow = {"acked": 0, "acking": False}
        inflight = collections.deque()  # (frame number, send time)

        async def pump_video():
            loop = asyncio.get_running_loop()
            sent = 0
            skipping = False
            last_reset = 0.0
            while True:
                hdr, payload = await source.next()
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
                                source.request_key()
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
                    if msg.data == b"\x11":  # RESET_VIDEO from the page
                        source.request_key()
                        continue
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
        await teardown()
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
    app.router.add_get("/api/version", api_version)
    app.router.add_get("/api/diag", api_diag)
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
