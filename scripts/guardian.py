# deepsleep 守护进程：监控 MaiBot(8001) 与本地 embedding 服务(8100)。
# 连续 2 次探测失败才动手，恢复前有冷却，避免重启风暴。
# 停止：直接杀本进程（停止全部.bat 会按 *deepsleep* 路径杀掉它）；重启服务请用各自的启动脚本。
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "guardian.log")
PIDFILE = os.path.join(BASE, ".guardian.pid")
CHECK_INTERVAL = 180
# 巡检报告：每 3 小时跑一次 health_report.py，台账落盘 + 报告文件供用户查看。
REPORT_INTERVAL = 3 * 3600
REPORT_SCRIPT = os.path.join(BASE, "health_report.py")
ARCHIVE_SCRIPT = os.path.join(BASE, "archive_images.py")
REPORT_OUT_DIR = os.path.join(BASE, "out")
REPORT_PYTHON = os.path.join(BASE, "embedding_server", ".venv", "Scripts", "python.exe")
FAILS_NEEDED = 2
RELAUNCH_COOLDOWN = 240
MAX_CONSECUTIVE_RELAUNCH = 3
GIVE_UP_SECONDS = 1800

SERVICES = {
    "maibot": {"port": 8001, "launch": "wscript //nologo D:\\deepsleep\\hidden_run.vbs \"cmd /c D:\\deepsleep\\start_maibot_silent.bat\""},
    "embedding": {"port": 8100, "launch": "wscript //nologo D:\\deepsleep\\hidden_run.vbs \"cmd /c D:\\deepsleep\\embedding_server\\启动embedding服务.bat\""},
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def launch(cmdline: str) -> None:
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{cmdline}'}}; $r.ReturnValue"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        log(f"launch {cmdline!r} -> return={out.stdout.strip()[:40]}")
    except Exception as exc:  # noqa: BLE001
        log(f"launch {cmdline!r} FAILED: {exc}")


def run_health_report() -> None:
    """跑巡检脚本：结果写 out/巡检报告.md，故障追加到 health_events.jsonl。"""
    if not (os.path.exists(REPORT_SCRIPT) and os.path.exists(REPORT_PYTHON)):
        log("巡检脚本或解释器缺失，跳过")
        return
    try:
        os.makedirs(REPORT_OUT_DIR, exist_ok=True)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [REPORT_PYTHON, REPORT_SCRIPT, "--hours", "4"],
            capture_output=True, text=True, cwd=BASE, env=env, timeout=600,
            encoding="utf-8", errors="replace",
        )
        report = result.stdout or ""
        stamp = time.strftime("%Y-%m-%d %H:%M")
        target = os.path.join(REPORT_OUT_DIR, f"巡检报告_{time.strftime('%Y%m%d_%H%M')}.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(report)
        with open(os.path.join(REPORT_OUT_DIR, "最新巡检报告.md"), "w", encoding="utf-8") as fh:
            fh.write(report)
        # 顺带把新图归档（幂等，已存在的会跳过）
        if os.path.exists(ARCHIVE_SCRIPT):
            try:
                subprocess.run([REPORT_PYTHON, ARCHIVE_SCRIPT, "--copy"],
                               capture_output=True, text=True, cwd=BASE, env=env, timeout=300,
                               encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                log(f"图片归档失败: {exc}")

        abnormal = "无异常" not in report or "异常 ——" in report
        log(f"巡检完成 {stamp} -> {os.path.basename(target)}（{'发现问题' if abnormal else '一切正常'}）")
        if result.stderr.strip():
            log(f"巡检 stderr: {result.stderr.strip()[:300]}")
    except Exception as exc:  # noqa: BLE001
        log(f"巡检执行失败: {exc}")


def acquire_instance_lock():
    """持有 Windows 命名互斥对象；PID 文件仅用于诊断。"""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, r"Local\deepsleep_guardian")
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return None
    return kernel, handle


def main() -> None:
    lock = acquire_instance_lock()
    if lock is None:
        log("已有守护进程在跑，本实例退出")
        return
    try:
        run_guardian()
    finally:
        lock[0].CloseHandle(lock[1])


def run_guardian() -> None:
    with open(PIDFILE, "w", encoding="ascii") as f:
        f.write(str(os.getpid()))

    fails = {name: 0 for name in SERVICES}
    cooldown_until = 0.0
    consecutive_relaunch = {name: 0 for name in SERVICES}
    last_relaunch_at = {name: 0.0 for name in SERVICES}
    log(f"守护启动 pid={os.getpid()} 监控: {json.dumps({k: v['port'] for k, v in SERVICES.items()})}")

    next_report_at = time.time()  # 启动先跑一次，之后每 REPORT_INTERVAL
    while True:
        now = time.time()
        if now >= next_report_at:
            run_health_report()
            next_report_at = time.time() + REPORT_INTERVAL
        if now < cooldown_until:
            time.sleep(15)
            continue
        acted = False
        for name, cfg in SERVICES.items():
            if port_up(cfg["port"]):
                fails[name] = 0
                consecutive_relaunch[name] = 0
                continue
            fails[name] += 1
            log(f"{name} 端口 {cfg['port']} 不通 ({fails[name]}/{FAILS_NEEDED})")
            if fails[name] < FAILS_NEEDED:
                continue
            if now - last_relaunch_at[name] > GIVE_UP_SECONDS:
                consecutive_relaunch[name] = 0
            if consecutive_relaunch[name] >= MAX_CONSECUTIVE_RELAUNCH:
                log(f"CRITICAL: {name} 连续拉起 {consecutive_relaunch[name]} 次未恢复，等待冷却结束")
                continue
            log(f"ACTION: 拉起 {name} (第 {consecutive_relaunch[name] + 1} 次)")
            launch(cfg["launch"])
            consecutive_relaunch[name] += 1
            last_relaunch_at[name] = now
            fails[name] = 0
            acted = True
        if acted:
            cooldown_until = time.time() + RELAUNCH_COOLDOWN
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
