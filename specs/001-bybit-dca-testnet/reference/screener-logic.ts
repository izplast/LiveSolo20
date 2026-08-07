/**
 * Референсная логика скринера — Bybit USDT Perpetual, этап Testnet.
 *
 * Назначение файла: зафиксировать поведение скринера в проверяемом виде до фазы
 * планирования. Это справочный артефакт спецификации (`specs/001-bybit-dca-testnet/`),
 * а не рабочий модуль: в проекте пока нет ни package.json, ни tsconfig.json, и
 * окончательное место кода определит /speckit-plan.
 *
 * Трассировка на требования spec.md:
 *   FR-001  — данные только из WS-потока, REST используется для стартового прогрева и справочников
 *   FR-002  — волатильность 1м по задокументированной формуле, диапазон 0.9–2.5%
 *   FR-003  — отсечение выше/ниже диапазона с фиксацией причины
 *   FR-004  — подтверждение совпадением направления тренда на 15м
 *   FR-005  — сигнал: тикер, направление, цена на момент сигнала, метка времени
 *   FR-006  — все пороги и таймфреймы в конфигурации
 *   FR-007  — направление сделки = направление подтверждённого тренда (лонг и шорт)
 *   FR-016  — детерминированный signalId, по которому бот отсекает дубликаты
 *   FR-017  — предфильтр вселенной по biржевым ограничениям (maxLeverage, статус торгов)
 *   FR-025  — интервалы недоступности потока помечаются как «слепые»
 *   FR-026  — без внешних зависимостей: глобальный WebSocket и fetch из Node (Termux)
 *   FR-027  — автопереподключение с backoff и ограничением частоты попыток
 *   FR-030  — signalId пригоден как основа orderLinkId для идемпотентности
 *   FR-031  — жёсткий запрет на боевой контур
 *   FR-032  — проверка расхождения часов устройства с биржевым временем
 *
 * Требования к среде: Node.js >= 22 (глобальные WebSocket и fetch). В Termux:
 * `pkg install nodejs-lts`. Внешних npm-зависимостей нет намеренно — см. FR-026.
 *
 * Компилируется только при `strict: true` (нужен strictNullChecks): разбор
 * результата evaluateCandidate опирается на сужение размеченного union по полю
 * `pass`, а без strictNullChecks сужение не работает и tsc даёт TS2339.
 *
 * Проверено (2026-07-31): tsc 5.6 --strict --noEmit без ошибок; чистые функции
 * (volatilityPct, trendDirection, evaluateCandidate, guard-ы) прогнаны на
 * синтетических свечах, включая границы 0.9 / 2.5%. Сетевая часть — WS, REST,
 * прогрев, backoff — не проверялась исполнением: нужен доступ к Bybit Testnet.
 */

// ─────────────────────────────────────────────────────────────────────────────
// Конфигурация (FR-006)
// ─────────────────────────────────────────────────────────────────────────────

/** Единственный допустимый контур на этом этапе (FR-031). */
export type Network = 'testnet' | 'mainnet';

export interface ScreenerConfig {
  /** Контур. Значение 'mainnet' приводит к отказу при старте — см. assertTestnet. */
  net: Network;

  /** Таймфреймы Bybit в минутах, строками — как их принимает WS-топик kline. */
  timeframes: {
    /** Младший ТФ, на котором считается волатильность и быстрый тренд. */
    fast: '1' | '3' | '5';
    /** Старший ТФ, подтверждающий направление. */
    slow: '15' | '30' | '60';
  };

  /** Метрика волатильности на младшем ТФ (FR-002). */
  volatility: {
    /** Нижняя граница включительно, %. */
    minPct: number;
    /** Верхняя граница включительно, %. Выше — «слишком рискованно» (FR-003). */
    maxPct: number;
    /** Сколько последних ЗАКРЫТЫХ свечей усредняется. */
    windowCandles: number;
  };

  /** Определение направления тренда на обоих ТФ (FR-004). */
  trend: {
    /** На сколько закрытых свечей назад смотрим на младшем ТФ. */
    fastLookback: number;
    /** То же на старшем ТФ. */
    slowLookback: number;
    /**
     * Порог значимости движения, %. Движение слабее считается боковиком
     * (direction = 0) и сигнала не даёт — защита от шумовых знаков.
     */
    minMovePct: number;
  };

  /** Отбор торгуемой вселенной (FR-017). */
  universe: {
    quoteCoin: 'USDT';
    /** Если задано — работаем только по этим тикерам (для отладки). */
    include?: string[];
    /** Всегда исключать. */
    exclude?: string[];
    /**
     * Плечо, которое бот собирается ставить. Символы с maxLeverage ниже
     * отбрасываются заранее, с фиксацией причины, вместо отказа ордера.
     */
    requiredLeverage: number;
  };

  /**
   * Пауза между сигналами по одному тикеру. Бот и так отсекает дубликаты по
   * открытому циклу (FR-016), но после закрытия цикла тикер снова свободен, и
   * без паузы скринер выдаёт сигнал на каждой закрывающейся свече.
   */
  signalCooldownMs: number;

  /** Что попадает в журнал отсечений (FR-003, FR-022). */
  rejects: {
    /**
     * 'all'        — каждое отсечение по каждому символу на каждой свече;
     * 'candidates' — только там, где истории достаточно и символ дошёл до
     *                проверки волатильности (по умолчанию: журнал остаётся
     *                читаемым, но все отсечения по волатильности видны);
     * 'none'       — не журналировать (не рекомендуется на этапе калибровки).
     */
    emit: 'all' | 'candidates' | 'none';
  };

  ws: {
    /** Топиков на одно соединение (2 топика на символ: fast + slow). */
    topicsPerConnection: number;
    /** Bybit принимает не более 10 args в одном сообщении подписки. */
    subscribeBatchSize: number;
    /** Bybit требует ping не реже раза в 20 с. */
    pingIntervalMs: number;
    /** Нет ни одного сообщения дольше этого — считаем соединение мёртвым. */
    staleTimeoutMs: number;
    backoff: {
      baseMs: number;
      maxMs: number;
      /** Доля случайного разброса, чтобы соединения не переподключались синхронно. */
      jitter: number;
    };
  };

  rest: {
    /** Одновременных запросов при стартовом прогреве истории. */
    seedConcurrency: number;
    /** Допустимое расхождение часов с биржей, мс (FR-032). */
    maxClockSkewMs: number;
    /** Повторных попыток на один REST-запрос. */
    retries: number;
  };
}

export const DEFAULT_CONFIG: ScreenerConfig = {
  net: 'testnet',
  timeframes: { fast: '1', slow: '15' },
  // Значения из spec.md; подлежат калибровке по итогам первого прогона.
  volatility: { minPct: 0.9, maxPct: 2.5, windowCandles: 5 },
  trend: { fastLookback: 3, slowLookback: 2, minMovePct: 0.05 },
  universe: { quoteCoin: 'USDT', requiredLeverage: 10 },
  signalCooldownMs: 5 * 60_000,
  rejects: { emit: 'candidates' },
  ws: {
    topicsPerConnection: 200,
    subscribeBatchSize: 10,
    pingIntervalMs: 15_000,
    staleTimeoutMs: 45_000,
    backoff: { baseMs: 1_000, maxMs: 60_000, jitter: 0.3 },
  },
  rest: { seedConcurrency: 6, maxClockSkewMs: 3_000, retries: 3 },
};

const ENDPOINTS: Record<Network, { rest: string; wsLinear: string }> = {
  testnet: {
    rest: 'https://api-testnet.bybit.com',
    wsLinear: 'wss://stream-testnet.bybit.com/v5/public/linear',
  },
  mainnet: {
    rest: 'https://api.bybit.com',
    wsLinear: 'wss://stream.bybit.com/v5/public/linear',
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Домен
// ─────────────────────────────────────────────────────────────────────────────

/** 1 — вверх, -1 — вниз, 0 — боковик (сигнала не даёт). */
export type Direction = 1 | -1 | 0;

/** Сторона сделки в терминах Bybit. */
export type Side = 'Buy' | 'Sell';

/** Закрытая свеча. Незакрытые в расчёт не берутся (FR-002). */
export interface Candle {
  /** Время открытия свечи, мс — используется как ключ дедупликации. */
  start: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

/** Сигнал, передаваемый боту (FR-005). */
export interface Signal {
  /**
   * Детерминированный идентификатор: один и тот же сигнал, пересчитанный после
   * переподключения, даёт тот же id. Бот использует его для отсечения дубликатов
   * (FR-016) и как основу orderLinkId для идемпотентности (FR-030).
   */
  signalId: string;
  symbol: string;
  side: Side;
  /** Цена на момент возникновения сигнала = close триггерной свечи (FR-005). */
  price: number;
  /** Метка времени возникновения сигнала, мс (локальные часы, сверенные с биржей). */
  signalTs: number;
  /** Диагностика для калибровки порогов. */
  diagnostics: {
    volatilityPct: number;
    directionFast: Direction;
    directionSlow: Direction;
    /** Время открытия свечи младшего ТФ, закрытие которой дало сигнал. */
    triggerCandleStart: number;
    /** Задержка от закрытия свечи до формирования сигнала, мс. */
    detectionLagMs: number;
  };
}

export type RejectReason =
  | 'insufficient_history'
  | 'volatility_below_min'
  | 'volatility_above_max'
  | 'trend_flat_fast'
  | 'trend_flat_slow'
  | 'trend_mismatch'
  | 'cooldown'
  | 'universe_not_trading'
  | 'universe_excluded'
  | 'universe_max_leverage';

/** Отсечение кандидата с причиной (FR-003, FR-022). */
export interface Reject {
  symbol: string;
  reason: RejectReason;
  ts: number;
  /** Значения, по которым принято решение — для разбора и калибровки. */
  details?: Record<string, number | string | null>;
}

/** Ограничения инструмента, нужные скринеру и боту (FR-017). */
export interface InstrumentInfo {
  symbol: string;
  status: string;
  maxLeverage: number;
  qtyStep: number;
  minOrderQty: number;
  tickSize: number;
}

/** Интервал недоступности потока (FR-025). */
export interface BlindInterval {
  from: number;
  to: number;
  durationMs: number;
  /** Символы, данные по которым были недоступны. */
  symbols: string[];
  cause: 'disconnect' | 'stale' | 'startup';
}

export interface ScreenerHandlers {
  onSignal(signal: Signal): void;
  onReject?(reject: Reject): void;
  /** Начало «слепого» интервала: поток по этим символам потерян. */
  onStreamDown?(info: { at: number; symbols: string[]; cause: BlindInterval['cause'] }): void;
  /** Конец «слепого» интервала. */
  onStreamUp?(interval: BlindInterval): void;
  /** Технические события для журнала: подписки, backoff, ошибки разбора. */
  onDiagnostic?(event: { at: number; kind: string; message: string; data?: unknown }): void;
}

// ─────────────────────────────────────────────────────────────────────────────
// Чистые функции: волатильность и тренд (тестируются без сети)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Волатильность на младшем ТФ (FR-002).
 *
 * Формула зафиксирована в spec.md → Assumptions: средний процентный размах
 * последних N ЗАКРЫТЫХ свечей, где размах одной свечи = (high - low) / low * 100.
 *
 * Возвращает null, если закрытых свечей меньше N — это не «ноль волатильности»,
 * а отсутствие данных, и оно должно приводить к 'insufficient_history',
 * а не к отсечению по нижней границе.
 */
export function volatilityPct(candles: readonly Candle[], windowCandles: number): number | null {
  if (candles.length < windowCandles || windowCandles <= 0) return null;
  const window = candles.slice(-windowCandles);
  let sum = 0;
  for (const c of window) {
    if (!(c.low > 0)) return null; // защита от битых данных: делить на ноль нельзя
    sum += ((c.high - c.low) / c.low) * 100;
  }
  return sum / windowCandles;
}

/**
 * Направление тренда на интервале (FR-004).
 *
 * Знак изменения цены закрытия за lookback закрытых свечей. Движение слабее
 * minMovePct считается боковиком: на минутном ТФ шум легко даёт случайный знак,
 * а ложное «совпадение трендов» — это лишняя сделка.
 *
 * Возвращает null при недостатке истории (отличать от боковика).
 */
export function trendDirection(
  candles: readonly Candle[],
  lookback: number,
  minMovePct: number,
): Direction | null {
  if (lookback <= 0 || candles.length < lookback + 1) return null;
  const last = candles[candles.length - 1];
  const past = candles[candles.length - 1 - lookback];
  if (!(past.close > 0)) return null;
  const movePct = ((last.close - past.close) / past.close) * 100;
  if (Math.abs(movePct) < minMovePct) return 0;
  return movePct > 0 ? 1 : -1;
}

/**
 * Допуск для граничных сравнений волатильности.
 *
 * Диапазон в spec.md объявлен включительным, но размах, посчитанный в double,
 * даёт на ровной границе значения вида 0.8999999999999879 — без допуска
 * кандидат ровно на 0.9% отсекался бы как «ниже минимума». Величина допуска
 * на восемь порядков меньше шага калибровки и на решение не влияет.
 */
const BOUNDARY_EPSILON = 1e-9;

/** Направление подтверждённого тренда → сторона сделки (FR-007). */
export function directionToSide(direction: Exclude<Direction, 0>): Side {
  return direction === 1 ? 'Buy' : 'Sell';
}

/**
 * Решение по одному кандидату на закрытии свечи младшего ТФ.
 *
 * Порядок проверок задаёт приоритет причин отсечения в журнале: сначала наличие
 * истории, затем волатильность (FR-002, FR-003), затем подтверждение тренда
 * (FR-004). Функция чистая — её проверяют юнит-тестами на синтетических свечах.
 */
export function evaluateCandidate(
  fast: readonly Candle[],
  slow: readonly Candle[],
  cfg: ScreenerConfig,
): { pass: true; direction: Exclude<Direction, 0>; volatilityPct: number; directionSlow: Direction }
  | { pass: false; reason: RejectReason; details: Record<string, number | string | null> } {
  const vol = volatilityPct(fast, cfg.volatility.windowCandles);
  const dirFast = trendDirection(fast, cfg.trend.fastLookback, cfg.trend.minMovePct);
  const dirSlow = trendDirection(slow, cfg.trend.slowLookback, cfg.trend.minMovePct);

  if (vol === null || dirFast === null || dirSlow === null) {
    return {
      pass: false,
      reason: 'insufficient_history',
      details: { fastCandles: fast.length, slowCandles: slow.length },
    };
  }
  if (vol > cfg.volatility.maxPct + BOUNDARY_EPSILON) {
    // Ключевое отсечение этапа: «слишком рискованно» (FR-003, SC-006).
    return { pass: false, reason: 'volatility_above_max', details: { volatilityPct: vol, maxPct: cfg.volatility.maxPct } };
  }
  if (vol < cfg.volatility.minPct - BOUNDARY_EPSILON) {
    return { pass: false, reason: 'volatility_below_min', details: { volatilityPct: vol, minPct: cfg.volatility.minPct } };
  }
  if (dirFast === 0) {
    return { pass: false, reason: 'trend_flat_fast', details: { volatilityPct: vol } };
  }
  if (dirSlow === 0) {
    return { pass: false, reason: 'trend_flat_slow', details: { volatilityPct: vol, directionFast: dirFast } };
  }
  if (dirSlow !== dirFast) {
    return {
      pass: false,
      reason: 'trend_mismatch',
      details: { volatilityPct: vol, directionFast: dirFast, directionSlow: dirSlow },
    };
  }
  return { pass: true, direction: dirFast, volatilityPct: vol, directionSlow: dirSlow };
}

// ─────────────────────────────────────────────────────────────────────────────
// Состояние по символу
// ─────────────────────────────────────────────────────────────────────────────

class SymbolState {
  readonly fast: Candle[] = [];
  readonly slow: Candle[] = [];
  lastSignalAt = 0;

  constructor(
    private readonly fastCapacity: number,
    private readonly slowCapacity: number,
  ) {}

  /**
   * Добавляет закрытую свечу. Возвращает true, если свеча новая.
   *
   * Bybit может присылать один и тот же закрытый kline повторно (в частности,
   * снапшотом после переподключения), а прогрев через REST пересекается с WS —
   * поэтому дедупликация по start обязательна, иначе одна свеча попадёт в окно
   * дважды и исказит волатильность.
   */
  push(tf: 'fast' | 'slow', candle: Candle): boolean {
    const buf = tf === 'fast' ? this.fast : this.slow;
    const cap = tf === 'fast' ? this.fastCapacity : this.slowCapacity;
    const last = buf[buf.length - 1];
    if (last && candle.start <= last.start) {
      if (last.start === candle.start) buf[buf.length - 1] = candle; // уточнение той же свечи
      return false;
    }
    buf.push(candle);
    while (buf.length > cap) buf.shift();
    return true;
  }

  seed(tf: 'fast' | 'slow', candles: Candle[]): void {
    for (const c of candles) this.push(tf, c);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Скринер
// ─────────────────────────────────────────────────────────────────────────────

interface ShardState {
  index: number;
  symbols: string[];
  socket: WebSocket | null;
  pingTimer: ReturnType<typeof setInterval> | null;
  staleTimer: ReturnType<typeof setTimeout> | null;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  attempt: number;
  lastMessageAt: number;
  /** Момент потери связи; null — соединение живо. */
  downSince: number | null;
  closedByUs: boolean;
}

export class BybitScreener {
  private readonly cfg: ScreenerConfig;
  private readonly handlers: ScreenerHandlers;
  private readonly states = new Map<string, SymbolState>();
  private readonly instruments = new Map<string, InstrumentInfo>();
  private shards: ShardState[] = [];
  private running = false;
  /** Сдвиг биржевого времени относительно локального, мс (FR-032). */
  private clockSkewMs = 0;

  constructor(handlers: ScreenerHandlers, config: Partial<ScreenerConfig> = {}) {
    this.cfg = mergeConfig(DEFAULT_CONFIG, config);
    this.handlers = handlers;
    assertTestnet(this.cfg);
    assertConfigSane(this.cfg);
  }

  /** Биржевое время: локальные часы с поправкой на измеренный сдвиг. */
  now(): number {
    return Date.now() + this.clockSkewMs;
  }

  get endpoints() {
    return ENDPOINTS[this.cfg.net];
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    const startedAt = this.now();

    await this.checkClockSkew();                       // FR-032
    const symbols = await this.loadUniverse();         // FR-017
    this.diag('universe', `отобрано символов: ${symbols.length}`, { symbols: symbols.length });

    // Прогрев истории через REST: WS отдаёт только новые свечи, и без прогрева
    // скринер молчал бы первые windowCandles минут и slowLookback*15 минут.
    // Стартовый интервал до готовности данных — тоже «слепой» (FR-025).
    this.handlers.onStreamDown?.({ at: startedAt, symbols, cause: 'startup' });
    await this.seedHistory(symbols);
    const seededAt = this.now();
    this.handlers.onStreamUp?.({
      from: startedAt, to: seededAt, durationMs: seededAt - startedAt, symbols, cause: 'startup',
    });

    this.shards = chunk(symbols, this.cfg.ws.topicsPerConnection / 2).map((group, index) => ({
      index,
      symbols: group,
      socket: null,
      pingTimer: null,
      staleTimer: null,
      reconnectTimer: null,
      attempt: 0,
      lastMessageAt: 0,
      downSince: seededAt,
      closedByUs: false,
    }));
    for (const shard of this.shards) this.connect(shard);
  }

  async stop(): Promise<void> {
    this.running = false;
    for (const shard of this.shards) {
      shard.closedByUs = true;
      clearTimers(shard);
      try {
        shard.socket?.close();
      } catch {
        /* уже закрыт */
      }
      shard.socket = null;
    }
    this.shards = [];
  }

  // ── REST ──────────────────────────────────────────────────────────────────

  /**
   * Расхождение часов устройства с биржевым (FR-032).
   *
   * На Android после сна часы уходят, а подпись приватных запросов Bybit
   * привязана к timestamp — без этой проверки бот получит серию отказов
   * с непрозрачной причиной. Скринер измеряет сдвиг один раз при старте и
   * применяет его ко всем своим меткам времени, чтобы задержка сигнал→ордер
   * считалась по одной шкале.
   */
  async checkClockSkew(): Promise<number> {
    const sentAt = Date.now();
    const res = await this.restGet<{ timeNano: string }>('/v5/market/time');
    const receivedAt = Date.now();
    const roundTrip = receivedAt - sentAt;
    const serverMs = Number(res.timeNano) / 1e6;
    // Компенсируем половину round-trip: ответ пришёл уже «постарев».
    this.clockSkewMs = Math.round(serverMs + roundTrip / 2 - receivedAt);
    if (Math.abs(this.clockSkewMs) > this.cfg.rest.maxClockSkewMs) {
      throw new Error(
        `Часы устройства расходятся с биржевым временем на ${this.clockSkewMs} мс ` +
          `(допустимо ${this.cfg.rest.maxClockSkewMs} мс). Синхронизируйте время до начала торговли.`,
      );
    }
    this.diag('clock', `сдвиг часов ${this.clockSkewMs} мс`, { skewMs: this.clockSkewMs, roundTrip });
    return this.clockSkewMs;
  }

  /** Вселенная инструментов + ограничения (FR-017). */
  async loadUniverse(): Promise<string[]> {
    const selected: string[] = [];
    let cursor: string | undefined;

    do {
      const page = await this.restGet<{
        list: Array<{
          symbol: string;
          contractType: string;
          status: string;
          quoteCoin: string;
          leverageFilter: { maxLeverage: string };
          lotSizeFilter: { qtyStep: string; minOrderQty: string };
          priceFilter: { tickSize: string };
        }>;
        nextPageCursor?: string;
      }>('/v5/market/instruments-info', { category: 'linear', limit: '1000', ...(cursor ? { cursor } : {}) });

      for (const it of page.list) {
        if (it.contractType !== 'LinearPerpetual' || it.quoteCoin !== this.cfg.universe.quoteCoin) continue;

        const info: InstrumentInfo = {
          symbol: it.symbol,
          status: it.status,
          maxLeverage: Number(it.leverageFilter.maxLeverage),
          qtyStep: Number(it.lotSizeFilter.qtyStep),
          minOrderQty: Number(it.lotSizeFilter.minOrderQty),
          tickSize: Number(it.priceFilter.tickSize),
        };
        this.instruments.set(info.symbol, info);

        const reject = this.screenInstrument(info);
        if (reject) {
          this.emitReject({ symbol: info.symbol, reason: reject.reason, ts: this.now(), details: reject.details }, true);
          continue;
        }
        selected.push(info.symbol);
      }
      cursor = page.nextPageCursor || undefined;
    } while (cursor);

    if (selected.length === 0) throw new Error('Вселенная инструментов пуста — проверьте фильтры конфигурации.');
    return selected;
  }

  /** Ограничения инструмента, отсекающие его до всякой торговли (FR-017, FR-018). */
  private screenInstrument(
    info: InstrumentInfo,
  ): { reason: RejectReason; details: Record<string, number | string | null> } | null {
    const { include, exclude, requiredLeverage } = this.cfg.universe;
    if (info.status !== 'Trading') {
      return { reason: 'universe_not_trading', details: { status: info.status } };
    }
    if (include && !include.includes(info.symbol)) {
      return { reason: 'universe_excluded', details: { rule: 'include-list' } };
    }
    if (exclude?.includes(info.symbol)) {
      return { reason: 'universe_excluded', details: { rule: 'exclude-list' } };
    }
    if (info.maxLeverage < requiredLeverage) {
      // Плечо не подменяем молча — spec.md → Edge Cases.
      return { reason: 'universe_max_leverage', details: { maxLeverage: info.maxLeverage, required: requiredLeverage } };
    }
    return null;
  }

  /** Стартовый прогрев окон истории по обоим ТФ. */
  private async seedHistory(symbols: string[]): Promise<void> {
    const fastLimit = Math.max(this.cfg.volatility.windowCandles, this.cfg.trend.fastLookback + 1) + 2;
    const slowLimit = this.cfg.trend.slowLookback + 3;

    await pool(symbols, this.cfg.rest.seedConcurrency, async (symbol) => {
      const state = this.stateFor(symbol);
      try {
        const [fast, slow] = await Promise.all([
          this.fetchKline(symbol, this.cfg.timeframes.fast, fastLimit),
          this.fetchKline(symbol, this.cfg.timeframes.slow, slowLimit),
        ]);
        state.seed('fast', fast);
        state.seed('slow', slow);
      } catch (err) {
        // Символ без прогрева не теряется: он доберёт историю из WS и до тех пор
        // будет отсекаться как 'insufficient_history'.
        this.diag('seed_failed', `не удалось прогреть ${symbol}: ${errorMessage(err)}`, { symbol });
      }
    });
  }

  /**
   * История свечей. Bybit отдаёт список от новых к старым и включает текущую
   * незакрытую свечу — её отбрасываем, иначе размах неполной свечи занизит
   * волатильность (FR-002 требует только закрытые свечи).
   */
  private async fetchKline(symbol: string, interval: string, limit: number): Promise<Candle[]> {
    const res = await this.restGet<{ list: string[][] }>('/v5/market/kline', {
      category: 'linear',
      symbol,
      interval,
      limit: String(limit + 1),
    });
    const intervalMs = Number(interval) * 60_000;
    const nowMs = this.now();
    return res.list
      .map(([start, open, high, low, close]) => ({
        start: Number(start),
        open: Number(open),
        high: Number(high),
        low: Number(low),
        close: Number(close),
      }))
      .filter((c) => c.start + intervalMs <= nowMs) // только закрытые
      .sort((a, b) => a.start - b.start)
      .slice(-limit);
  }

  private async restGet<T>(path: string, params: Record<string, string> = {}): Promise<T> {
    const url = new URL(path, this.endpoints.rest);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);

    let lastError: unknown;
    for (let attempt = 0; attempt <= this.cfg.rest.retries; attempt++) {
      if (attempt > 0) await sleep(backoffDelay(attempt, this.cfg.ws.backoff));
      try {
        const res = await fetch(url, { headers: { accept: 'application/json' } });
        if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
        const body = (await res.json()) as { retCode: number; retMsg: string; result: T };
        if (body.retCode !== 0) throw new Error(`Bybit retCode=${body.retCode}: ${body.retMsg}`);
        return body.result;
      } catch (err) {
        lastError = err;
      }
    }
    throw new Error(`REST ${path} не удался после ${this.cfg.rest.retries + 1} попыток: ${errorMessage(lastError)}`);
  }

  // ── WebSocket (FR-001, FR-027) ────────────────────────────────────────────

  private connect(shard: ShardState): void {
    if (!this.running) return;

    const socket = new WebSocket(this.endpoints.wsLinear);
    shard.socket = socket;
    shard.closedByUs = false;

    socket.addEventListener('open', () => {
      shard.attempt = 0;
      shard.lastMessageAt = this.now();
      this.subscribe(shard);
      this.armPing(shard);
      this.armStaleWatchdog(shard);
      this.markShardUp(shard, 'disconnect');
    });

    socket.addEventListener('message', (event: MessageEvent) => {
      shard.lastMessageAt = this.now();
      this.armStaleWatchdog(shard);
      this.handleMessage(shard, typeof event.data === 'string' ? event.data : String(event.data));
    });

    socket.addEventListener('error', () => {
      // Подробности приходят следующим за error событием close.
      this.diag('ws_error', `ошибка соединения shard#${shard.index}`, { shard: shard.index });
    });

    socket.addEventListener('close', () => {
      clearTimers(shard);
      shard.socket = null;
      if (shard.closedByUs || !this.running) return;
      this.markShardDown(shard, 'disconnect');
      this.scheduleReconnect(shard);
    });
  }

  private scheduleReconnect(shard: ShardState): void {
    shard.attempt += 1;
    const delay = backoffDelay(shard.attempt, this.cfg.ws.backoff);
    this.diag('ws_reconnect', `shard#${shard.index}: переподключение через ${delay} мс (попытка ${shard.attempt})`, {
      shard: shard.index,
      delay,
      attempt: shard.attempt,
    });
    shard.reconnectTimer = setTimeout(() => this.connect(shard), delay);
  }

  private subscribe(shard: ShardState): void {
    const topics = shard.symbols.flatMap((s) => [
      `kline.${this.cfg.timeframes.fast}.${s}`,
      `kline.${this.cfg.timeframes.slow}.${s}`,
    ]);
    for (const batch of chunk(topics, this.cfg.ws.subscribeBatchSize)) {
      shard.socket?.send(JSON.stringify({ op: 'subscribe', args: batch }));
    }
    this.diag('ws_subscribe', `shard#${shard.index}: подписка на ${topics.length} топиков`, {
      shard: shard.index,
      topics: topics.length,
    });
  }

  private armPing(shard: ShardState): void {
    if (shard.pingTimer) clearInterval(shard.pingTimer);
    shard.pingTimer = setInterval(() => {
      if (shard.socket?.readyState === WebSocket.OPEN) shard.socket.send(JSON.stringify({ op: 'ping' }));
    }, this.cfg.ws.pingIntervalMs);
  }

  /**
   * Сторож тишины. На Android соединение после сна устройства часто остаётся
   * «открытым», но данные по нему не идут — без этого сторожа скринер молча
   * ослепнет, а прогон будет выглядеть успешным (SC-007).
   */
  private armStaleWatchdog(shard: ShardState): void {
    if (shard.staleTimer) clearTimeout(shard.staleTimer);
    shard.staleTimer = setTimeout(() => {
      this.diag('ws_stale', `shard#${shard.index}: нет сообщений ${this.cfg.ws.staleTimeoutMs} мс, переподключаемся`, {
        shard: shard.index,
      });
      this.markShardDown(shard, 'stale');
      try {
        shard.socket?.close();
      } catch {
        /* уже закрыт */
      }
    }, this.cfg.ws.staleTimeoutMs);
  }

  private handleMessage(shard: ShardState, raw: string): void {
    let msg: {
      topic?: string;
      op?: string;
      success?: boolean;
      ret_msg?: string;
      data?: Array<Record<string, string | number | boolean>>;
    };
    try {
      msg = JSON.parse(raw);
    } catch {
      this.diag('ws_parse_error', `shard#${shard.index}: неразбираемое сообщение`, { shard: shard.index });
      return;
    }

    if (msg.op === 'subscribe' && msg.success === false) {
      this.diag('ws_subscribe_failed', `shard#${shard.index}: подписка отклонена: ${msg.ret_msg ?? ''}`, {
        shard: shard.index,
      });
      return;
    }
    if (msg.op === 'pong' || msg.op === 'ping' || !msg.topic || !msg.data) return;

    const parts = msg.topic.split('.'); // kline.<interval>.<symbol>
    if (parts[0] !== 'kline') return;
    const interval = parts[1];
    const symbol = parts[2];
    const tf: 'fast' | 'slow' | null =
      interval === this.cfg.timeframes.fast ? 'fast' : interval === this.cfg.timeframes.slow ? 'slow' : null;
    if (!tf || !symbol) return;

    const state = this.stateFor(symbol);
    for (const item of msg.data) {
      if (item.confirm !== true) continue; // только закрытые свечи (FR-002)
      const candle: Candle = {
        start: Number(item.start),
        open: Number(item.open),
        high: Number(item.high),
        low: Number(item.low),
        close: Number(item.close),
      };
      if (!Number.isFinite(candle.start) || !(candle.low > 0)) continue;
      const isNew = state.push(tf, candle);
      // Решение принимается только на закрытии свечи МЛАДШЕГО ТФ: старший ТФ
      // лишь подтверждает направление и сам сигналов не порождает (FR-004).
      if (isNew && tf === 'fast') this.evaluate(symbol, state, candle);
    }
  }

  // ── Решение по кандидату ──────────────────────────────────────────────────

  private evaluate(symbol: string, state: SymbolState, trigger: Candle): void {
    const ts = this.now();
    const verdict = evaluateCandidate(state.fast, state.slow, this.cfg);

    if (!verdict.pass) {
      this.emitReject(
        { symbol, reason: verdict.reason, ts, details: verdict.details },
        verdict.reason !== 'insufficient_history',
      );
      return;
    }

    // Пауза действует только после успешного прохождения фильтров: иначе тикер,
    // отсечённый по волатильности, необоснованно «отдыхал» бы.
    if (ts - state.lastSignalAt < this.cfg.signalCooldownMs) {
      this.emitReject(
        {
          symbol,
          reason: 'cooldown',
          ts,
          details: { sinceLastSignalMs: ts - state.lastSignalAt, cooldownMs: this.cfg.signalCooldownMs },
        },
        true,
      );
      return;
    }
    state.lastSignalAt = ts;

    const intervalMs = Number(this.cfg.timeframes.fast) * 60_000;
    const signal: Signal = {
      signalId: `${symbol}:${this.cfg.timeframes.fast}:${trigger.start}`,
      symbol,
      side: directionToSide(verdict.direction),
      price: trigger.close,
      signalTs: ts,
      diagnostics: {
        volatilityPct: round(verdict.volatilityPct, 4),
        directionFast: verdict.direction,
        directionSlow: verdict.directionSlow,
        triggerCandleStart: trigger.start,
        // Сколько прошло от закрытия свечи до формирования сигнала. Входит в
        // задержку сигнал→ордер (FR-019, SC-003) и показывает, сколько из
        // бюджета 2 с съедает сам скринер.
        detectionLagMs: ts - (trigger.start + intervalMs),
      },
    };
    this.handlers.onSignal(signal);
  }

  private emitReject(reject: Reject, isCandidate: boolean): void {
    const mode = this.cfg.rejects.emit;
    if (mode === 'none') return;
    if (mode === 'candidates' && !isCandidate) return;
    this.handlers.onReject?.(reject);
  }

  // ── «Слепые» интервалы (FR-025) ───────────────────────────────────────────

  private markShardDown(shard: ShardState, cause: BlindInterval['cause']): void {
    if (shard.downSince !== null) return; // уже помечен
    shard.downSince = this.now();
    this.handlers.onStreamDown?.({ at: shard.downSince, symbols: [...shard.symbols], cause });
  }

  private markShardUp(shard: ShardState, cause: BlindInterval['cause']): void {
    if (shard.downSince === null) return;
    const to = this.now();
    const from = shard.downSince;
    shard.downSince = null;
    this.handlers.onStreamUp?.({ from, to, durationMs: to - from, symbols: [...shard.symbols], cause });
  }

  // ── Прочее ────────────────────────────────────────────────────────────────

  private stateFor(symbol: string): SymbolState {
    let state = this.states.get(symbol);
    if (!state) {
      const fastCap = Math.max(this.cfg.volatility.windowCandles, this.cfg.trend.fastLookback + 1) + 2;
      const slowCap = this.cfg.trend.slowLookback + 3;
      state = new SymbolState(fastCap, slowCap);
      this.states.set(symbol, state);
    }
    return state;
  }

  /** Ограничения инструмента для бота: размер входа надо привести к шагу (FR-017). */
  instrument(symbol: string): InstrumentInfo | undefined {
    return this.instruments.get(symbol);
  }

  private diag(kind: string, message: string, data?: unknown): void {
    this.handlers.onDiagnostic?.({ at: this.now(), kind, message, data });
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Проверки конфигурации
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Запрет боевого контура на этапе Testnet (FR-031, SC-008).
 *
 * Проверка стоит в конструкторе, до любого сетевого вызова: единственный
 * надёжный момент — до того, как процесс что-то отправит.
 */
export function assertTestnet(cfg: ScreenerConfig): void {
  if (cfg.net !== 'testnet') {
    throw new Error(
      'Этап MVP допускает только Bybit Testnet (FR-031). ' +
        'Боевой контур вне рамок текущего этапа — смена net требует отдельного решения.',
    );
  }
}

/** Раннее обнаружение бессмысленных комбинаций параметров. */
export function assertConfigSane(cfg: ScreenerConfig): void {
  const problems: string[] = [];
  const { minPct, maxPct, windowCandles } = cfg.volatility;
  if (!(minPct > 0)) problems.push('volatility.minPct должен быть больше нуля');
  if (!(maxPct > minPct)) problems.push('volatility.maxPct должен быть больше minPct');
  if (!Number.isInteger(windowCandles) || windowCandles < 1) problems.push('volatility.windowCandles — целое >= 1');
  if (cfg.trend.fastLookback < 1 || cfg.trend.slowLookback < 1) problems.push('trend.*Lookback — целое >= 1');
  if (cfg.trend.minMovePct < 0) problems.push('trend.minMovePct не может быть отрицательным');
  if (cfg.universe.requiredLeverage < 1) problems.push('universe.requiredLeverage >= 1');
  if (cfg.ws.subscribeBatchSize < 1 || cfg.ws.subscribeBatchSize > 10) {
    problems.push('ws.subscribeBatchSize в пределах 1..10 — Bybit не принимает больше 10 args на сообщение');
  }
  if (cfg.ws.topicsPerConnection < 2) problems.push('ws.topicsPerConnection >= 2 (по 2 топика на символ)');
  if (cfg.ws.pingIntervalMs >= 20_000) problems.push('ws.pingIntervalMs < 20000 — Bybit закрывает соединение без ping');
  if (cfg.ws.staleTimeoutMs <= cfg.ws.pingIntervalMs) problems.push('ws.staleTimeoutMs должен превышать pingIntervalMs');
  if (problems.length) throw new Error(`Некорректная конфигурация скринера:\n- ${problems.join('\n- ')}`);
}

// ─────────────────────────────────────────────────────────────────────────────
// Утилиты
// ─────────────────────────────────────────────────────────────────────────────

function mergeConfig(base: ScreenerConfig, override: Partial<ScreenerConfig>): ScreenerConfig {
  return {
    ...base,
    ...override,
    timeframes: { ...base.timeframes, ...override.timeframes },
    volatility: { ...base.volatility, ...override.volatility },
    trend: { ...base.trend, ...override.trend },
    universe: { ...base.universe, ...override.universe },
    rejects: { ...base.rejects, ...override.rejects },
    ws: { ...base.ws, ...override.ws, backoff: { ...base.ws.backoff, ...override.ws?.backoff } },
    rest: { ...base.rest, ...override.rest },
  };
}

/** Экспоненциальный backoff с разбросом — ограничивает частоту попыток (FR-027). */
function backoffDelay(attempt: number, cfg: ScreenerConfig['ws']['backoff']): number {
  const raw = Math.min(cfg.maxMs, cfg.baseMs * 2 ** (attempt - 1));
  const spread = raw * cfg.jitter;
  return Math.round(raw - spread / 2 + Math.random() * spread);
}

function chunk<T>(items: readonly T[], size: number): T[][] {
  const step = Math.max(1, Math.floor(size));
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += step) out.push(items.slice(i, i + step));
  return out;
}

/** Ограниченный по параллелизму обход — чтобы прогрев не упёрся в лимиты REST. */
async function pool<T>(items: readonly T[], limit: number, worker: (item: T) => Promise<void>): Promise<void> {
  const queue = [...items];
  const runners = Array.from({ length: Math.max(1, Math.min(limit, queue.length)) }, async () => {
    for (;;) {
      const item = queue.shift();
      if (item === undefined) return;
      await worker(item);
    }
  });
  await Promise.all(runners);
}

function clearTimers(shard: ShardState): void {
  if (shard.pingTimer) clearInterval(shard.pingTimer);
  if (shard.staleTimer) clearTimeout(shard.staleTimer);
  if (shard.reconnectTimer) clearTimeout(shard.reconnectTimer);
  shard.pingTimer = null;
  shard.staleTimer = null;
  shard.reconnectTimer = null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ─────────────────────────────────────────────────────────────────────────────
// Открытые вопросы к фазе планирования
// ─────────────────────────────────────────────────────────────────────────────

/**
 * 1. Контракт передачи сигнала боту (spec.md → Assumptions: способ доставки
 *    входит в измеряемую задержку). Здесь скринер отдаёт сигнал колбэком —
 *    то есть в одном процессе с ботом, задержка доставки близка к нулю. Если
 *    план разведёт их по процессам (IPC, локальный сокет, файл-очередь), в
 *    Signal нужно добавить метку времени отправки, иначе задержка доставки
 *    сольётся с задержкой биржи и SC-003 станет непроверяемым.
 *
 * 2. Цена сигнала = close закрытой минутной свечи, поэтому к моменту сигнала
 *    ей уже detectionLagMs миллисекунд. Это осознанный выбор: свеча — тот же
 *    источник, по которому принято решение. Альтернатива — держать поток
 *    tickers.<symbol> и брать lastPrice: проскальзывание в журнале выйдет
 *    меньше, но измерять оно будет уже не «цену на момент решения». Выбор
 *    влияет на трактовку SC-004 и должен быть зафиксирован в плане.
 *
 * 3. Порог trend.minMovePct = 0.05% выбран без данных. Первый прогон покажет
 *    распределение отсечений 'trend_flat_fast' — если их доля велика, порог
 *    завышен и часть сигналов теряется впустую.
 *
 * 4. Сколько символов реально держать: 2 топика на символ × ~500 линейных
 *    перпетуалов = ~1000 подписок и заметный поток сообщений на Android.
 *    Нагрузку на CPU и батарею в Termux надо измерить на первом прогоне; при
 *    нехватке — сузить вселенную по обороту, а не увеличивать topicsPerConnection.
 */
