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
  - `logs/bot-events.jsonl` (сделки),
  - `logs/bot.log` (heartbeat),
  - `live_100_700_timeline.csv` (пульс скринера).
- Если процессов нет (`pgrep -f live_screener|bot.py|executor` пусто) и логи старше 5 мин —
  прямо сказать, что боты стоят с `HH:MM Kyiv`, и предложить `sv restart midcap-bot` /
  `sv restart midcap-screener`.

## Live-процессы: runsv (Termux)
- `bot.py`, `executor.py` и `live_screener_midcap.py` управляются через **runsv**,
  не вручную.
- Сервисы: `midcap-bot` (бот + исполнитель) и `midcap-screener`.
- Статус: `sv status midcap-bot midcap-screener`.
- Рестарт: `sv restart midcap-bot` / `sv restart midcap-screener`.

## Bybit-контур: DEMO (не testnet, не mainnet)
- `config/config.yml`: `bybit.demo: true`, `bybit.testnet: false`.
- Ключи демо — **только через env, не в файл**: `/root/.bybit_demo_env` (chmod 600, вне репо).
- Сервис `midcap-bot` должен получать окружение из `/root/.bybit_demo_env`, иначе
  исполнитель падает с `требует API-ключи`.
- Не плодить дубли: один `bot.py --live` → один `executor.py --live` (дубли = двойные ордера
  на одном демо-счёте). Проверка: `pgrep -a python3`.
