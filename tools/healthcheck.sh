#!/usr/bin/env bash
# tools/healthcheck.sh — проверка живости DCA-бота на Termux.
#
# Дёргает http://localhost:8000/health и пишет результат с timestamp в
# logs/healthcheck.log. Провал проверки помечается явным FAIL, чтобы его
# было легко найти в логе.
#
# Разовый запуск (удобно для cron / termux-job-scheduler):
#     ~/bybit-dca-bot/tools/healthcheck.sh
#     echo $?        # 0 — бот ответил, !=0 — health-check провалился
#
# Фоновый демон (самый простой вариант без cron — цикл + sleep):
#     ~/bybit-dca-bot/tools/healthcheck.sh --daemon-interval 300
#
# Переменные окружения (все необязательны):
#     HEALTHCHECK_URL   адрес health-эндпоинта (по умолчанию http://localhost:8000/health)
#     HEALTHCHECK_LOG   куда писать (по умолчанию <бот>/logs/healthcheck.log)
#     HEALTHCHECK_TIMEOUT  таймаут curl в секундах (по умолчанию 10)
#
# Настройка периодического запуска в Termux — см. комментарий ниже.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"

URL="${HEALTHCHECK_URL:-http://localhost:8000/health}"
LOG="${HEALTHCHECK_LOG:-$BOT_DIR/logs/healthcheck.log}"
TIMEOUT="${HEALTHCHECK_TIMEOUT:-10}"

mkdir -p "$(dirname "$LOG")"

stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }

check_once() {
    # ВАЖНО: без else ветки $? после `if cmd; then ... fi` всегда 0, когда
    # условие ложно (bash), — поэтому код выхода curl берём сразу после вызова.
    curl -fsS --max-time "$TIMEOUT" "$URL" >/dev/null 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "$(stamp) OK    $URL" >>"$LOG"
    else
        echo "$(stamp) FAIL  $URL  (curl exit $rc)" >>"$LOG"
    fi
    return "$rc"
}

INTERVAL=""
if [ "${1:-}" = "--daemon-interval" ]; then
    INTERVAL="${2:-}"
fi

if [ -n "$INTERVAL" ]; then
    # Демон-фоллбэк: без cron и termux-job-scheduler (например, Android не
    # дал поставить cronie). Пока процесс жив, проверяет каждые N секунд.
    while :; do
        check_once >/dev/null 2>&1 || true
        sleep "$INTERVAL"
    done
fi

check_once
rc=$?
exit "$rc"

# ---------------------------------------------------------------------------
# Как запускать периодически в Termux
# ---------------------------------------------------------------------------
#
# Вариант A — termux-job-scheduler (Termux:API, JobScheduler Android):
#   pkg install termux-api
#   termux-job-scheduler --script "$HOME/bybit-dca-bot/tools/healthcheck.sh" \
#       --interval-ms 300000 --persisted true
#   termux-job-scheduler --list        # посмотреть активные задания
#   termux-job-scheduler --remove "$HOME/bybit-dca-bot/tools/healthcheck.sh"
#   Минимальный интервал JobScheduler ~15 минут; задание живёт, пока жив
#   системный планировщик, и переживает перезагрузку при --persisted.
#
# Вариант B — cron (termux-services + cronie), интервал произвольный:
#   pkg install cronie termux-services
#   sv-enable crond                    # включить демон crond
#   sv status crond                    # проверить, что работает
#   crontab -e
#   # добавить строку (каждые 5 минут):
#   */5 * * * * ~/bybit-dca-bot/tools/healthcheck.sh
#
# Вариант C — фоновый демон-цикл (без зависимостей):
#   nohup ~/bybit-dca-bot/tools/healthcheck.sh --daemon-interval 300 \
#       >/dev/null 2>&1 &
#
# Во всех вариантах держите Termux под termux-wake-lock, иначе Android
# усыпит устройство и ни cron, ни демон, ни бот не будут выполняться:
#   termux-wake-lock
#
# Провал видно по FAIL-строкам в логе:
#   grep FAIL logs/healthcheck.log
