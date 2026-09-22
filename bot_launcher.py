#!/usr/bin/env python3
import time, logging, subprocess, sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[logging.FileHandler("logs/bot_live.log"), logging.StreamHandler()])
log = logging.getLogger("bot")
log.info("bot launcher started")
proc = subprocess.Popen([sys.executable, "executor.py"])
log.info(f"executor PID {proc.pid}")
try:
    while True:
        log.info("bot heartbeat: alive")
        time.sleep(60)
        if proc.poll() is not None:
            log.warning(f"executor exited {proc.returncode}, restarting")
            proc = subprocess.Popen([sys.executable, "executor.py"])
except KeyboardInterrupt:
    proc.terminate()
PY
chmod +x bot_launcher.py bot.py
ls -lh bot.py bot_launcher.py
head -n 5 bot.py
wc -l bot.py
# test import bot now should work (should find reference via bot.py at root which is now reference copy)
python3 -c "import bot; print('bot tp_price', hasattr(bot, 'tp_price'))" 2>&1
# kill old bot 28874 (which is wrapper)
kill 28874 2>&1; sleep 2
# launch new bot via launcher
nohup python3 bot_launcher.py > logs/bot_live.log 2>&1 & echo "bot launcher PID $!"
sleep 2
ps aux | grep -E "bot|executor" | grep -v grep | head -20
tail -n 10 logs/bot_live.log 2>&1 | head -n 10
