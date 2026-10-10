"""Build the Synology DSM 7 package (x86_64) without Docker.

    python synology/build.py

Bundles a standalone CPython, the aiohttp wheels, Google's Linux adb and the app
into dist/tv-adb-<version>-x86_64.spk. Everything is assembled straight into tar
streams so Linux file modes and symlinks survive building on Windows.
"""
import io
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = HERE / ".cache"
DIST = ROOT / "dist"

VERSION = "1.1.0-0006"
PY_VER = "3.12"
PY_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261003/"
          "cpython-3.12.15%2B20261003-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz")
ADB_URL = "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"

# trimmed from the bundled interpreter to keep the package small
PY_SKIP = (
    "python/include/",
    "python/share/",
    "python/lib/tcl", "python/lib/tk", "python/lib/itcl", "python/lib/thread",
    "python/lib/libtcl", "python/lib/libtk",
    f"python/lib/python{PY_VER}/test/",
    f"python/lib/python{PY_VER}/idlelib/",
    f"python/lib/python{PY_VER}/tkinter/",
    f"python/lib/python{PY_VER}/turtledemo/",
    f"python/lib/python{PY_VER}/ensurepip/",
    f"python/lib/python{PY_VER}/lib2to3/",
    f"python/lib/python{PY_VER}/lib-dynload/_tkinter",
)

INFO = {
    "package": "tv-adb",
    "version": VERSION,
    "os_min_ver": "7.0-40000",
    "arch": "x86_64",
    "displayname": "TV ADB",
    "description": "Web remote for Android devices over ADB: live screen, remote keys, device list.",
    "description_chs": "网页版安卓设备遥控台：实时画面、遥控按键、设备管理。",
    "maintainer": "tyrantcwj",
    "maintainer_url": "https://github.com/tyrantcwj/tv-adb",
    "support_url": "https://github.com/tyrantcwj/tv-adb",
    "dsmuidir": "ui",
    "dsmappname": "SYNO.SDS._ThirdParty.App.tvadb",
    "adminprotocol": "http",
    "adminport": "8765",
    "adminurl": "/",
    "thirdparty": "yes",
    "ctl_stop": "yes",
    "silent_install": "no",
    "silent_upgrade": "yes",
}


def fetch(url, name):
    CACHE.mkdir(exist_ok=True)
    path = CACHE / name
    if not path.exists():
        print("download", url)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
    return path


def wheels():
    out = CACHE / "wheels"
    if not any(out.glob("aiohttp-*.whl")):
        subprocess.check_call([
            sys.executable, "-m", "pip", "download", "-q", "-d", str(out),
            "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64",
            "--python-version", PY_VER, "--implementation", "cp", "--abi", f"cp{PY_VER.replace('.', '')}",
            "--only-binary=:all:", "-r", str(ROOT / "requirements.txt"),
            # pip evaluates markers against the host interpreter, so 3.12-only deps get skipped
            "typing_extensions",
        ])
    return sorted(out.glob("*.whl"))


def add_bytes(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def add_dir(tar, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = int(time.time())
    tar.addfile(info)


def add_tree(tar, src: Path, dest: str):
    for p in sorted(src.rglob("*")):
        rel = f"{dest}/{p.relative_to(src).as_posix()}"
        if p.is_dir():
            add_dir(tar, rel)
        else:
            add_bytes(tar, rel, p.read_bytes())


def build_package_tgz():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        # interpreter, keeping the original modes and symlinks
        with tarfile.open(fetch(PY_URL, "python.tar.gz")) as py:
            for m in py.getmembers():
                if m.name.startswith(PY_SKIP):
                    continue
                m.uid = m.gid = 0
                m.uname = m.gname = "root"
                tar.addfile(m, py.extractfile(m) if m.isfile() else None)

        site = f"python/lib/python{PY_VER}/site-packages"
        for whl in wheels():
            with zipfile.ZipFile(whl) as z:
                for n in z.namelist():
                    if not n.endswith("/"):
                        add_bytes(tar, f"{site}/{n}", z.read(n), 0o755 if n.endswith(".so") else 0o644)

        with zipfile.ZipFile(fetch(ADB_URL, "platform-tools-linux.zip")) as z:
            add_dir(tar, "bin")
            add_bytes(tar, "bin/adb", z.read("platform-tools/adb"), 0o755)

        add_dir(tar, "app")
        add_bytes(tar, "app/app.py", (ROOT / "app.py").read_bytes())
        add_tree(tar, ROOT / "static", "app/static")
        add_tree(tar, ROOT / "server", "app/server")

        add_tree(tar, HERE / "ui", "ui")
        add_bytes(tar, "tv-adb.sc", (HERE / "tv-adb.sc").read_bytes())
    return buf.getvalue()


def lf(path: Path):
    return path.read_bytes().replace(b"\r\n", b"\n")


def main():
    if not (HERE / "PACKAGE_ICON.PNG").exists():
        subprocess.check_call([sys.executable, str(HERE / "make_icons.py")])
    package = build_package_tgz()
    info = "".join(f'{k}="{v}"\n' for k, v in INFO.items()).encode()

    DIST.mkdir(exist_ok=True)
    out = DIST / f"tv-adb-{VERSION}-x86_64.spk"
    with tarfile.open(out, "w", format=tarfile.USTAR_FORMAT) as spk:
        add_bytes(spk, "INFO", info)
        add_bytes(spk, "package.tgz", package)
        add_dir(spk, "scripts")
        for s in sorted((HERE / "scripts").iterdir()):
            add_bytes(spk, f"scripts/{s.name}", lf(s), 0o755)
        add_dir(spk, "conf")
        for c in sorted((HERE / "conf").iterdir()):
            add_bytes(spk, f"conf/{c.name}", lf(c))
        add_dir(spk, "WIZARD_UIFILES")
        for w in sorted((HERE / "WIZARD_UIFILES").iterdir()):
            add_bytes(spk, f"WIZARD_UIFILES/{w.name}", lf(w))
        add_bytes(spk, "PACKAGE_ICON.PNG", (HERE / "PACKAGE_ICON.PNG").read_bytes())
        add_bytes(spk, "PACKAGE_ICON_256.PNG", (HERE / "PACKAGE_ICON_256.PNG").read_bytes())
    print(f"{out}  {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
