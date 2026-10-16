# AGENTS.md — постоянные правила проекта

## Часовой пояс и время (дефолт, не спрашивать)
- Всегда использовать **Europe/Kyiv (киевское время, UTC+2/+3)**.
- Никогда не спрашивать у пользователя дату/время сессии — брать системное время и конвертировать в Kyiv.
- В отчётах всегда указывать обе метки: `Kyiv (EEST/EET)` + `UTC`.
- Команда для текущего времени: `TZ=Europe/Kyiv date '+%Y-%m-%d %H:%M:%S %Z'`.

## Отчёт «что по сделкам за 1 час» (дефолт, не спрашивать период)
- По умолчанию период = **последние 60 минут от текущего киевского времени**.
- Не спрашивать даты сессии — просто запускать:
  `python3 tools/last_hour_kyiv.py [--hours 1]`
- Скрипт сам читает:
  - `logs/screener-events.jsonl` (сигналы),
  - `logs/bot-events.jsonl`, `logs/paper-dca-events.jsonl`,
    `logs/bot3-achop-events.jsonl`, `logs/bot4-achop-solo-events.jsonl` (сделки),
  - `logs/bot.log`, `logs/paper_dca.log`, `logs/bot3.log`, `logs/bot4.log` (heartbeat),
  - `live_100_700_timeline.csv` (пульс скринера).
- Если процессов нет (`pgrep -f live_screener|bot.py|executor` пусто) и логи старше 5 мин —
  прямо сказать, что боты стоят с `HH:MM Kyiv`, и предложить рестарт через `watchdog.sh` / `bot.py`.

## Bybit-контур: DEMO (не testnet, не mainnet)
- `config/config.yml`: `bybit.demo: true`, `bybit.testnet: false`.
- Ключи демо — **только через env, не в файл**: `/root/.bybit_demo_env` (chmod 600, вне репо).
- Рестарт только с окружением: `source /root/.bybit_demo_env` перед запуском
  `watchdog.sh` / `bot.py`, иначе исполнители падают с `требует API-ключи`.
- watchdog запускать откреплённо: `setsid nohup bash watchdog.sh >> logs/watchdog.log 2>&1 < /dev/null &`
  (иначе висящий дочерний процесс вешает вызывающий шелл).
- Не плодить дубли: один `bot.py --live` → один `executor.py --live` (дубли = двойные ордера
  на одном демо-счёте). Проверка: `pgrep -a python3`.
