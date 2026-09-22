#!/usr/bin/env python3
"""bot.py — proxy to reference/bot.py when imported, daemon when run directly"""
import sys
from pathlib import Path

if __name__ != "__main__":
    # When imported as `import bot` (e.g., from backtest/bot_config), re-export reference/bot
    import importlib.util
    ref_path = Path(__file__).parent / "specs/001-bybit-dca-testnet/reference/bot.py"
    spec = importlib.util.spec_from_file_location("_ref_bot", str(ref_path))
    _ref = importlib.util.module_from_spec(spec)
    sys.modules["_ref_bot"] = _ref
    # ensure pricing etc. found
    import sys as _sys2
    _ref_dir = str(ref_path.parent)
    if _ref_dir not in _sys2.path:
        _sys2.path.insert(0, _ref_dir)
    spec.loader.exec_module(_ref)
    for _k in dir(_ref):
        if not _k.startswith("_"):
            globals()[_k] = getattr(_ref, _k)
else:
    # Daemon for prompt: keep alive and launch executor
    import time, logging, subprocess, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[logging.FileHandler("logs/bot_live.log"), logging.StreamHandler()])
    log = logging.getLogger("bot")
    log.info("bot.py daemon started")
    try:
        proc = subprocess.Popen([sys.executable, "executor.py"])
        log.info(f"executor PID {proc.pid}")
        while True:
            log.info("bot heartbeat: alive")
            time.sleep(60)
            if proc.poll() is not None:
                log.warning(f"executor exited {proc.returncode}, restarting")
                proc = subprocess.Popen([sys.executable, "executor.py"])
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:
            pass
