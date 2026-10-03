#!/usr/bin/env python3
"""Manage the user's local HTML library login service (macOS, stdlib only)."""

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


APP_DIR = Path(__file__).resolve().parent
SERVER = APP_DIR / "server.py"
DATA_DIR = APP_DIR / ".data"
LABEL = "local.html-library"
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
DOMAIN = "gui/{}".format(os.getuid())
SERVICE = DOMAIN + "/" + LABEL
URL = "http://127.0.0.1:18765"
PORTS = (18765, 18766)
LOCAL_HTTP = build_opener(ProxyHandler({}))
PID_FILE = DATA_DIR / "server-process.json"


class ManagerError(Exception):
    pass


def require_macos():
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise ManagerError("登录自启管理需要 macOS 和 launchctl。")


def launchctl(*args):
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=20
    )


def loaded():
    return launchctl("print", SERVICE).returncode == 0


def owned_plist():
    """Never replace or unload a service owned by a different checkout/app."""
    if not PLIST.exists() and not PLIST.is_symlink():
        return False
    if PLIST.is_symlink():
        raise ManagerError("同名启动配置是符号链接，已保留，未做更改：{}".format(PLIST))
    try:
        with PLIST.open("rb") as handle:
            config = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise ManagerError("无法验证已有启动配置，已保留：{}".format(PLIST)) from exc
    if not isinstance(config, dict):
        raise ManagerError("已有启动配置格式不符，已保留：{}".format(PLIST))
    args = config.get("ProgramArguments", [])
    if (
        config.get("Label") != LABEL
        or not isinstance(args, list)
        or len(args) != 3
        or args[1] != "-u"
        or not isinstance(args[2], str)
        or Path(args[2]).resolve() != SERVER
        or config.get("WorkingDirectory") != str(APP_DIR)
    ):
        raise ManagerError(
            "发现不属于这个作品库的同名启动配置，拒绝覆盖或停止：{}".format(PLIST)
        )
    return True


def health():
    try:
        request = Request(URL + "/api/state", headers={"Accept": "application/json"})
        with LOCAL_HTTP.open(request, timeout=2) as response:
            if response.status != 200:
                return None
            state = json.load(response)
        return state if isinstance(state, dict) else None
    except (OSError, HTTPError, URLError, ValueError):
        return None


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_until_stopped():
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if not any(port_in_use(port) for port in PORTS):
            return
        time.sleep(0.2)


def stop_service():
    if not loaded():
        return
    result = launchctl("bootout", SERVICE)
    if result.returncode != 0:
        raise ManagerError("停止后台服务失败：{}".format(result.stderr.strip()))
    wait_until_stopped()


def process_identity(pid, timeout=5):
    """Include process birth time so a stale file cannot identify a reused PID."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "command="],
        capture_output=True, text=True, timeout=timeout,
        env=dict(os.environ, LC_ALL="C", LANG="C"),
    )
    fields = result.stdout.strip().split(None, 6)
    if result.returncode != 0 or len(fields) != 7:
        return None
    if fields[0] != str(os.getuid()):
        return None
    return {"pid": pid, "started_at": " ".join(fields[1:6]), "command": fields[6]}


def normalized_start(value):
    """Compare a pre-existing localized ps timestamp with the current C locale."""
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
    except (TypeError, ValueError):
        match = re.search(r"(\d{1,2})/\s*(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4})$", str(value))
        if not match:
            return None
        month, day, clock, year = match.groups()
        try:
            return datetime.strptime("%s-%02d-%02d %s" % (year, int(month), int(day), clock), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def recorded_process():
    try:
        record = json.loads(PID_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("server") != str(SERVER):
        return None
    identity = process_identity(record.get("pid"))
    if identity and identity["pid"] == record.get("pid"):
        saved_start = normalized_start(record.get("started_at"))
        current_start = normalized_start(identity["started_at"])
        python = record.get("python")
        expected = str(SERVER)
        if isinstance(python, str) and saved_start is not None and saved_start == current_start:
            # Apple's framework Python re-executes through its Python.app binary.
            framework_binary = str(Path(python).parent.parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python")
            allowed_executables = {python, framework_binary}
            saved_cmd = record.get("command", "")
            live_cmd = identity["command"]
            if saved_cmd in {"{} -u {}".format(exe, expected) for exe in allowed_executables} and live_cmd in {"{} -u {}".format(exe, expected) for exe in allowed_executables}:
                return identity
    return None


def wait_for_health(seconds, process=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if health() is not None:
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.3)
    return False


def start():
    """Start from the current terminal's ordinary macOS permission context."""
    if not SERVER.is_file():
        raise ManagerError("缺少 server.py，请保持整个 html-library 文件夹完整。")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "start.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if health() is not None:
            print("作品库已在运行：{}".format(URL))
            return 0
        previous = recorded_process()
        if previous:
            if wait_for_health(8):
                print("作品库已在运行：{}".format(URL))
                return 0
            raise ManagerError("已验证已有本作品库进程，但它暂未响应，请查看 .data/server.stderr.log。")
        occupied = [str(port) for port in PORTS if port_in_use(port)]
        if occupied:
            raise ManagerError("本地端口 {} 已被占用，未重复启动。".format("、".join(occupied)))
        python = str(Path(sys.executable).resolve())
        with (DATA_DIR / "server.stdout.log").open("ab") as stdout:
            with (DATA_DIR / "server.stderr.log").open("ab") as stderr:
                process = subprocess.Popen(
                    [python, "-u", str(SERVER)], cwd=str(APP_DIR),
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    start_new_session=True, close_fds=True,
                )
        identity = process_identity(process.pid)
        if identity:
            record = dict(identity, server=str(SERVER), python=python)
            PID_FILE.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if not wait_for_health(15, process):
            raise ManagerError(
                "后台启动尚未通过检查，请查看 .data/server.stderr.log。"
                "若 macOS 拦截文稿目录访问，请在终端正常处理系统权限提示后再试。"
            )
        print("作品库已启动：{}".format(URL))
        print("关闭网页和当前终端后，后台会继续收录；重启 Mac 后需再次双击开始使用。")
        return 0


def stop():
    """Stop only the exact process recorded by our ordinary start command."""
    if not PID_FILE.is_file():
        raise ManagerError("没有可验证的启动记录，未停止任何进程。")
    with (DATA_DIR / "start.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = recorded_process()
        if identity is None:
            raise ManagerError("启动记录与当前进程不一致或进程已退出，未停止任何进程。")
        # Check again immediately before signalling; never trust a PID by itself.
        if process_identity(identity["pid"]) != identity:
            raise ManagerError("进程身份已变化，未停止任何进程。")
        try:
            os.kill(identity["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                current = process_identity(
                    identity["pid"], timeout=max(0.001, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                break
            if current != identity:
                PID_FILE.unlink(missing_ok=True)
                print("已停止作品库后台收录，数据和原始 HTML 均已保留。")
                return 0
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise ManagerError("已发送停止请求，但进程在 5 秒内尚未退出；没有强制终止。")


def remove_failed_launch_agent():
    if owned_plist():
        stop_service()
        PLIST.unlink()


def install():
    require_macos()
    if not SERVER.is_file():
        raise ManagerError("缺少 server.py，请保持整个 html-library 文件夹完整。")
    is_owned = owned_plist()
    is_loaded = loaded()
    if is_loaded and not is_owned:
        raise ManagerError("已有同名后台服务且无法验证归属，未做更改。")
    if is_loaded:
        stop_service()
    occupied = [str(port) for port in PORTS if port_in_use(port)]
    if occupied:
        raise ManagerError(
            "本地端口 {} 已被其他进程占用，未启动作品库。".format("、".join(occupied))
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    python = str(Path(sys.executable).resolve())
    config = {
        "Label": LABEL,
        "ProgramArguments": [python, "-u", str(SERVER)],
        "WorkingDirectory": str(APP_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(DATA_DIR / "server.stdout.log"),
        "StandardErrorPath": str(DATA_DIR / "server.stderr.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=PLIST.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            plistlib.dump(config, handle)
        temp_path.chmod(0o644)
        os.replace(str(temp_path), str(PLIST))
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    stderr_path = DATA_DIR / "server.stderr.log"
    stderr_offset = stderr_path.stat().st_size if stderr_path.exists() else 0
    try:
        result = launchctl("bootstrap", DOMAIN, str(PLIST))
        if result.returncode != 0:
            raise ManagerError("启用登录自启失败：{}".format(result.stderr.strip()))
        result = launchctl("kickstart", "-k", SERVICE)
        if result.returncode != 0:
            raise ManagerError("启动后台服务失败：{}".format(result.stderr.strip()))
        if not wait_for_health(15):
            log_text = ""
            try:
                with stderr_path.open("rb") as handle:
                    handle.seek(stderr_offset)
                    log_text = handle.read(65536).decode("utf-8", errors="replace")
            except OSError:
                pass
            if "Operation not permitted" in log_text:
                raise ManagerError(
                    "macOS 拒绝后台 Python 访问文稿目录，登录自启未启用。"
                    "可双击“开始使用.command”从终端正常启动。"
                )
            raise ManagerError("服务未通过检查，请查看日志：{}".format(stderr_path))
    except (ManagerError, OSError, subprocess.SubprocessError) as exc:
        try:
            remove_failed_launch_agent()
        except (ManagerError, OSError, subprocess.SubprocessError) as cleanup_exc:
            raise ManagerError("{}；清理失败的自启配置时出错：{}".format(exc, cleanup_exc)) from exc
        raise ManagerError("{}；本次失败的自启配置已移除，避免反复重启。".format(exc)) from exc
    print("作品库已启动，并已设为当前用户登录后自动运行。")
    print("入口：{}".format(URL))
    print("关闭网页后，后台仍会继续收录。")
    return 0


def status():
    state = health()
    print("作品库：{}".format("运行中" if state is not None else "尚未运行或暂时无响应"))
    print("入口：{}".format(URL))
    print("数据目录：{}".format(DATA_DIR))
    if sys.platform == "darwin":
        try:
            configured = owned_plist()
            print("登录自启：{}".format("已设置" if configured else "未设置"))
            print("登录自启服务：{}".format("已加载" if loaded() else "未加载"))
        except ManagerError as exc:
            print("登录自启：{}".format(exc))
    if state is not None:
        for key in ("items", "files", "entries"):
            if isinstance(state.get(key), list):
                print("已收录：{} 个页面".format(len(state[key])))
                break
        if isinstance(state.get("sources"), list):
            print("收录目录：{} 个".format(len(state["sources"])))
        return 0
    print("双击“开始使用.command”即可启动。")
    return 1


def open_library():
    require_macos()
    if health() is None:
        raise ManagerError("作品库尚未启动，请先双击“开始使用.command”。")
    subprocess.run(["/usr/bin/open", URL], check=True, timeout=10)
    return 0


def uninstall():
    require_macos()
    if not owned_plist():
        print("没有本作品库的登录自启配置，无需移除。")
        print("源码、收录数据和原始 HTML 均已保留。")
        return 0
    stop_service()
    PLIST.unlink()
    print("已停止作品库后台服务，并取消登录自启。")
    print("源码、收录数据和原始 HTML 均已保留。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="本地 HTML 作品库启动管理")
    parser.add_argument("command", choices=("start", "stop", "install", "status", "open", "uninstall"))
    args = parser.parse_args()
    try:
        return {"start": start, "stop": stop, "install": install, "status": status, "open": open_library,
                "uninstall": uninstall}[args.command]()
    except (ManagerError, OSError, subprocess.SubprocessError) as exc:
        print("操作未完成：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
