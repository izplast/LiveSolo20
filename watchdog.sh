#!/bin/bash
# watchdog.sh — авто-перезапуск live_screener_midcap.py и bot.py при зависании
FILE="live_100_700_timeline.csv"
BOT_FILE="logs/bot.log"
PAPER_FILE="logs/paper_dca.log"
BOT3_FILE="logs/bot3.log"
BOT4_FILE="logs/bot4.log"
LOG="logs/watchdog.log"
RESTART_CMD="nohup python3 live_screener_midcap.py >> logs/screener_v2_live.log 2>&1 &"
BOT_RESTART_CMD="nohup python3 bot.py >> logs/bot.log 2>&1 &"
PAPER_RESTART_CMD="nohup python3 executor_dca_paper.py >> logs/paper_dca.log 2>&1 &"
BOT3_RESTART_CMD="nohup python3 executor_achop_paper.py >> logs/bot3.log 2>&1 &"
BOT4_RESTART_CMD="nohup python3 executor_achop_solo_paper.py >> logs/bot4.log 2>&1 &"
mkdir -p logs
echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG started PID $$, watching $FILE, $BOT_FILE, $PAPER_FILE, $BOT3_FILE and $BOT4_FILE (threshold 300s)" >> "$LOG"
while true; do
    TS=$(date '+%Y-%m-%d %H:%M:%S')
    # --- Screener check ---
    if [ -f "$FILE" ]; then
        NOW=$(date +%s)
        MTIME=$(stat -c %Y "$FILE" 2>/dev/null || echo 0)
        AGE=$((NOW - MTIME))
        if [ "$AGE" -gt 300 ]; then
            SCREENER_PID=$(pgrep -f live_screener_midcap.py | head -1)
            if [ -n "$SCREENER_PID" ]; then
                ELAPSED=$(ps -o etimes= -p "$SCREENER_PID" 2>/dev/null | tr -d ' ')
                if [ -n "$ELAPSED" ] && [ "$ELAPSED" -lt 600 ]; then
                    echo "[$TS] WATCHDOG: $FILE stale ${AGE}s but screener PID $SCREENER_PID up ${ELAPSED}s <600s -> skip restart (warmup)" >> "$LOG"
                else
                    echo "[$TS] WATCHDOG: $FILE stale ${AGE}s >300s -> pkill + restart" >> "$LOG"
                    pkill -f live_screener_midcap.py 2>>"$LOG" || true
                    sleep 2
                    if pgrep -f live_screener_midcap.py >/dev/null 2>&1; then
                        pkill -9 -f live_screener_midcap.py 2>>"$LOG" || true
                    fi
                    eval $RESTART_CMD
                    echo "[$TS] WATCHDOG: restarted live_screener_midcap.py PID $! (age was ${AGE}s)" >> "$LOG"
                fi
            else
                echo "[$TS] WATCHDOG: $FILE stale ${AGE}s >300s (no process) -> start" >> "$LOG"
                eval $RESTART_CMD
                echo "[$TS] WATCHDOG: started live_screener_midcap.py PID $! (age was ${AGE}s)" >> "$LOG"
            fi
        fi
    else
        echo "[$TS] WATCHDOG: $FILE not found -> start" >> "$LOG"
        pkill -f live_screener_midcap.py 2>>"$LOG" || true
        sleep 1
        eval $RESTART_CMD
        echo "[$TS] WATCHDOG: started live_screener_midcap.py PID $! (file missing)" >> "$LOG"
    fi

    # --- Bot check (bot.py + executor.py) ---
    BOT_PID=$(pgrep -f "bot\.py" | head -1)
    EXEC_PID=$(pgrep -f "executor\.py" | head -1)
    BOT_AGE=999999
    if [ -f "$BOT_FILE" ]; then
        NOW_B=$(date +%s)
        MTIME_B=$(stat -c %Y "$BOT_FILE" 2>/dev/null || echo 0)
        BOT_AGE=$((NOW_B - MTIME_B))
    fi
    if [ -z "$BOT_PID" ] || [ "$BOT_AGE" -gt 300 ]; then
        if [ -z "$BOT_PID" ]; then
            REASON="no process"
        else
            REASON="stale ${BOT_AGE}s >300s"
        fi
        echo "[$TS] WATCHDOG: $BOT_FILE $REASON -> pkill + restart bot" >> "$LOG"
        pkill -f executor.py 2>>"$LOG" || true
        pkill -f bot.py 2>>"$LOG" || true
        sleep 2
        if pgrep -f "bot\.py" >/dev/null 2>&1; then
            pkill -9 -f bot.py 2>>"$LOG" || true
        fi
        if pgrep -f "executor\.py" >/dev/null 2>&1; then
            pkill -9 -f executor.py 2>>"$LOG" || true
        fi
        eval $BOT_RESTART_CMD
        echo "[$TS] WATCHDOG: restarted bot.py PID $! ($REASON)" >> "$LOG"
    else
        # optional: log healthy occasionally
        :
    fi

    # --- Paper Soft DCA check (executor_dca_paper.py) ---
    PAPER_PID=$(pgrep -f "executor_dca_paper\.py" | head -1)
    PAPER_AGE=999999
    if [ -f "$PAPER_FILE" ]; then
        NOW_P=$(date +%s)
        MTIME_P=$(stat -c %Y "$PAPER_FILE" 2>/dev/null || echo 0)
        PAPER_AGE=$((NOW_P - MTIME_P))
    fi
    if [ -z "$PAPER_PID" ] || [ "$PAPER_AGE" -gt 300 ]; then
        if [ -z "$PAPER_PID" ]; then
            REASON_P="no process"
        else
            REASON_P="stale ${PAPER_AGE}s >300s"
        fi
        echo "[$TS] WATCHDOG: $PAPER_FILE $REASON_P -> pkill + restart paper" >> "$LOG"
        pkill -f executor_dca_paper.py 2>>"$LOG" || true
        sleep 2
        if pgrep -f "executor_dca_paper\.py" >/dev/null 2>&1; then
            pkill -9 -f executor_dca_paper.py 2>>"$LOG" || true
        fi
        eval $PAPER_RESTART_CMD
        echo "[$TS] WATCHDOG: restarted executor_dca_paper.py PID $! ($REASON_P)" >> "$LOG"
    fi

    # --- Bot3 ACHOP check (executor_achop_paper.py) ---
    BOT3_PID=$(pgrep -f "executor_achop_paper\.py" | head -1)
    BOT3_AGE=999999
    if [ -f "$BOT3_FILE" ]; then
        NOW_B3=$(date +%s)
        MTIME_B3=$(stat -c %Y "$BOT3_FILE" 2>/dev/null || echo 0)
        BOT3_AGE=$((NOW_B3 - MTIME_B3))
    fi
    if [ -z "$BOT3_PID" ] || [ "$BOT3_AGE" -gt 300 ]; then
        if [ -z "$BOT3_PID" ]; then
            REASON_B3="no process"
        else
            REASON_B3="stale ${BOT3_AGE}s >300s"
        fi
        echo "[$TS] WATCHDOG: $BOT3_FILE $REASON_B3 -> pkill + restart bot3" >> "$LOG"
        pkill -f executor_achop_paper.py 2>>"$LOG" || true
        sleep 2
        if pgrep -f "executor_achop_paper\.py" >/dev/null 2>&1; then
            pkill -9 -f executor_achop_paper.py 2>>"$LOG" || true
        fi
        eval $BOT3_RESTART_CMD
        echo "[$TS] WATCHDOG: restarted executor_achop_paper.py PID $! ($REASON_B3)" >> "$LOG"
    fi

    # --- Bot4 ACHOP Solo check (executor_achop_solo_paper.py) ---
    BOT4_PID=$(pgrep -f "executor_achop_solo_paper\.py" | head -1)
    BOT4_AGE=999999
    if [ -f "$BOT4_FILE" ]; then
        NOW_B4=$(date +%s)
        MTIME_B4=$(stat -c %Y "$BOT4_FILE" 2>/dev/null || echo 0)
        BOT4_AGE=$((NOW_B4 - MTIME_B4))
    fi
    if [ -z "$BOT4_PID" ] || [ "$BOT4_AGE" -gt 300 ]; then
        if [ -z "$BOT4_PID" ]; then
            REASON_B4="no process"
        else
            REASON_B4="stale ${BOT4_AGE}s >300s"
        fi
        echo "[$TS] WATCHDOG: $BOT4_FILE $REASON_B4 -> pkill + restart bot4" >> "$LOG"
        pkill -f executor_achop_solo_paper.py 2>>"$LOG" || true
        sleep 2
        if pgrep -f "executor_achop_solo_paper\.py" >/dev/null 2>&1; then
            pkill -9 -f executor_achop_solo_paper.py 2>>"$LOG" || true
        fi
        eval $BOT4_RESTART_CMD
        echo "[$TS] WATCHDOG: restarted executor_achop_solo_paper.py PID $! ($REASON_B4)" >> "$LOG"
    fi

    # --- Log rotation: screener_v2_live.log >500MB -> truncate ---
    if [ -f "logs/screener_v2_live.log" ]; then
        SIZE=$(stat -c %s "logs/screener_v2_live.log" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt 524288000 ]; then
            echo "[$TS] WATCHDOG: logs/screener_v2_live.log size ${SIZE} >500MB -> truncate" >> "$LOG"
            truncate -s 0 "logs/screener_v2_live.log" 2>>"$LOG" || : > "logs/screener_v2_live.log"
            echo "[$TS] WATCHDOG: truncated logs/screener_v2_live.log" >> "$LOG"
        fi
    fi
    if [ -f "logs/bot3.log" ]; then
        SIZE=$(stat -c %s "logs/bot3.log" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt 524288000 ]; then
            echo "[$TS] WATCHDOG: logs/bot3.log size ${SIZE} >500MB -> truncate" >> "$LOG"
            truncate -s 0 "logs/bot3.log" 2>>"$LOG" || : > "logs/bot3.log"
            echo "[$TS] WATCHDOG: truncated logs/bot3.log" >> "$LOG"
        fi
    fi
    if [ -f "logs/bot4.log" ]; then
        SIZE=$(stat -c %s "logs/bot4.log" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt 524288000 ]; then
            echo "[$TS] WATCHDOG: logs/bot4.log size ${SIZE} >500MB -> truncate" >> "$LOG"
            truncate -s 0 "logs/bot4.log" 2>>"$LOG" || : > "logs/bot4.log"
            echo "[$TS] WATCHDOG: truncated logs/bot4.log" >> "$LOG"
        fi
    fi

    sleep 60
done
