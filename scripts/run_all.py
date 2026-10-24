"""Run every agent as its own process (each has its own seed, port and mailbox). Headless-friendly.

    python scripts/run_all.py [agent ...]        # default: all four

- No terminal or stdin needed: output goes to stdout, one line per log line, prefixed with time and agent name.
- A crashed agent is restarted with backoff (up to MAX_RESTARTS within RESTART_WINDOW_S); the others keep running.
  If one keeps crashing, everything is stopped and the exit code is non-zero so a supervisor can notice.
- Ctrl+C and SIGTERM (systemd, Docker, `kill`) stop all agents cleanly.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ["discovery", "analyst", "action", "orchestrator"]  # workers first so the Orchestrator can reach them
MAX_RESTARTS = 5
RESTART_WINDOW_S = 600.0
BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)

_stop = threading.Event()


def log(text: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {text}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # a legacy console that can't show some character
        print(line.encode("ascii", "replace").decode(), flush=True)


def pump(name: str, proc: subprocess.Popen) -> None:
    for line in proc.stdout:
        log(f"[{name}] {line.rstrip()}")


def start(name: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH", "")]))
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "copilot.main", name], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8", errors="replace",
    )
    threading.Thread(target=pump, args=(name, proc), daemon=True).start()
    return proc


def kill_tree(proc: subprocess.Popen) -> None:
    # The Windows venv launcher spawns the real interpreter as a child, so kill the whole tree.
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    names = sys.argv[1:] or AGENTS
    unknown = [n for n in names if n not in AGENTS]
    if unknown:
        log(f"[run_all] unknown agent(s): {', '.join(unknown)} (choose from {', '.join(AGENTS)})")
        return 2
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break, and what some service wrappers send
        signal.signal(signal.SIGBREAK, lambda *_: _stop.set())

    procs: dict[str, subprocess.Popen] = {}
    crashes: dict[str, list[float]] = {n: [] for n in names}
    exit_code = 0
    try:
        for name in names:
            procs[name] = start(name)
            log(f"[run_all] started {name}")
            _stop.wait(1.0)
        while not _stop.wait(1.0):
            for name, proc in list(procs.items()):
                if proc.poll() is None:
                    continue
                now = time.monotonic()
                crashes[name] = [t for t in crashes[name] if now - t < RESTART_WINDOW_S] + [now]
                if len(crashes[name]) > MAX_RESTARTS:
                    log(f"[run_all] {name} keeps crashing (exit code {proc.returncode}); stopping everything")
                    exit_code = 1
                    _stop.set()
                    break
                delay = BACKOFF_S[min(len(crashes[name]) - 1, len(BACKOFF_S) - 1)]
                log(f"[run_all] {name} exited with code {proc.returncode}; restarting in {delay:.0f}s "
                    f"({len(crashes[name])}/{MAX_RESTARTS})")
                if _stop.wait(delay):
                    break
                procs[name] = start(name)
    finally:
        log("[run_all] stopping all agents...")
        for proc in procs.values():
            kill_tree(proc)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
