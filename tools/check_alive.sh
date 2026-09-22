#!/bin/bash
# Regression test for diagnosing-bugs H1: bot/screener/watchdog liveness
FAIL=0
for pat in "bot\.py" "executor\.py" "live_screener_midcap" "watchdog"; do
  if ! pgrep -f "$pat" > /dev/null; then echo "FAIL $pat"; FAIL=1; else echo "OK $pat"; fi
done
for f in "logs/bot.log" "live_100_700_timeline.csv" "logs/screener-events.jsonl"; do
  age=$(($(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0)))
  if [ "$age" -gt 300 ]; then echo "FAIL $f stale ${age}s"; FAIL=1; else echo "OK $f ${age}s"; fi
done
[ $FAIL -eq 0 ] && echo "GREEN" || echo "RED"
exit $FAIL
