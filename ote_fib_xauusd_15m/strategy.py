"""
Strategia OTE (Optimal Trade Entry) su Fibonacci per XAUUSD, multi-timeframe:
- swing pivot, dealing range ed ingresso su candele M5 (TIMEFRAME_MINUTES = 5);
- filtro di trend con EMA 50 / EMA 100 calcolate su candele H1 (TREND_TIMEFRAME_MINUTES = 60), che la
  strategia costruisce da sola, in modo causale, aggregando le candele M5 chiuse.

Questo file contiene TUTTA la logica di trading e nient'altro. È lo stesso identico codice usato
dal backtest (backtest.py) e da un eventuale loop live: non importa nulla del backtest, non accede
a file, rete, broker, API od orologio di sistema.

Logica (versione causale dell'indicatore TradingView "OTE visible chart" di @twingall):
- l'indicatore usa massimo e minimo del grafico VISIBILE (look-ahead puro); qui il "grafico
  visibile" diventa l'ULTIMO DEALING RANGE CAUSALE, definito da swing pivot confermati
  (PIVOT_LEFT/PIVOT_RIGHT) e da uno zigzag con alternanza obbligatoria;
- un pivot sulla candela i è noto solo alla chiusura della candela i + PIVOT_RIGHT (75 minuti dopo);
- filtro di trend EMA 50 / EMA 100 su H1: con trend rialzista solo OTE long, con trend ribassista solo
  OTE short; in più le EMA H1 devono essere separate (>= EMA_SEP_MIN_ATR x ATR H1) nella direzione del
  range per almeno una candela dello swing e non devono incrociarsi più di MAX_EMA_CROSSES volte.
  Una candela H1 è considerata chiusa alla chiusura della candela M5 delle :55 oppure, se quella manca
  (buco), all'arrivo della prima candela M5 di un'ora successiva: le EMA H1 non usano mai dati futuri;
- filtri del range: BOS, ampiezza minima in ATR, livello di ingresso non ancora toccato;
- ingresso SOLO con ordine limite al livello 0.705 (sweet spot della zona OTE 0.618-0.786),
  stop loss all'origine dell'impulso (livello 1.0), take profit all'estremo dell'impulso (0.0);
- l'ordine pendente si cancella per NUOVO_ESTREMO, SCADUTO, SOSTITUITO o TREND_EMA_INVERTITO;
- la strategia non emette MAI azioni di chiusura: le uscite sono solo SL e TP lato esecuzione.

La strategia riceve candele M5 CHIUSE una alla volta (on_candle_close) ed emette una lista di
Action (PLACE_LIMIT / CANCEL) valide dall'apertura della candela successiva. Tutti gli indicatori
sono incrementali con buffer a lunghezza fissa.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Parametri (FISSI: non vanno ottimizzati)
# ---------------------------------------------------------------------------
TIMEFRAME_MINUTES = 5           # timeframe delle candele alimentate alla strategia (swing ed entry)
TREND_TIMEFRAME_MINUTES = 60    # timeframe delle EMA di trend (H1), aggregato dalla strategia
PIVOT_LEFT = 5
PIVOT_RIGHT = 5
ATR_PERIOD = 14
MIN_RANGE_ATR = 3.0
OTE_LOW = 0.618
OTE_HIGH = 0.786
ENTRY_LEVEL = 0.705
TP_LEVEL = 0.0
SL_LEVEL = 1.0                  # origine dell'impulso (non è un parametro libero: SL senza buffer)
MID_LEVEL = 0.5                 # midline, solo per i grafici
EMA_FAST = 50
EMA_SLOW = 100
EMA_SEP_MIN_ATR = 0.5
MAX_EMA_CROSSES = 1
SWING_BUFFER = 5000
ORDER_EXPIRY_CANDLES = 48
USE_BODIES = False
RISK_PER_TRADE = 0.01
WARMUP_CANDLES = 500            # candele M5
WARMUP_TREND_CANDLES = 200      # candele H1 chiuse (EMA 100 seminata a 100 + 100 di assestamento)
CONTRACT_SIZE_OZ = 100
MIN_LOT = 0.01
LOT_STEP = 0.01

# ---------------------------------------------------------------------------
# Costanti simboliche
# ---------------------------------------------------------------------------
LONG = "long"
SHORT = "short"

PLACE_LIMIT = "PLACE_LIMIT"
CANCEL = "CANCEL"

# esiti dei dealing range
ORDINE_PIAZZATO = "ORDINE_PIAZZATO"
SCARTATO_EMA = "SCARTATO_EMA"
SCARTATO_EMA_INCROCI = "SCARTATO_EMA_INCROCI"
SCARTATO_EMA_VICINE = "SCARTATO_EMA_VICINE"
SCARTATO_SWING_LUNGO = "SCARTATO_SWING_LUNGO"
SCARTATO_BOS = "SCARTATO_BOS"
SCARTATO_AMPIEZZA = "SCARTATO_AMPIEZZA"
SCARTATO_GIA_TOCCATO = "SCARTATO_GIA_TOCCATO"
IGNORATO_POSIZIONE_APERTA = "IGNORATO_POSIZIONE_APERTA"
RANGE_OUTCOMES = (
    ORDINE_PIAZZATO, SCARTATO_EMA, SCARTATO_EMA_INCROCI, SCARTATO_EMA_VICINE, SCARTATO_SWING_LUNGO,
    SCARTATO_BOS, SCARTATO_AMPIEZZA, SCARTATO_GIA_TOCCATO, IGNORATO_POSIZIONE_APERTA,
)
VALIDO = "VALIDO"   # esito interno dei filtri (diventa ORDINE_PIAZZATO o IGNORATO_POSIZIONE_APERTA)

# motivi di cancellazione
NUOVO_ESTREMO = "NUOVO_ESTREMO"
SCADUTO = "SCADUTO"
SOSTITUITO = "SOSTITUITO"
TREND_EMA_INVERTITO = "TREND_EMA_INVERTITO"
CANCEL_REASONS = (NUOVO_ESTREMO, SCADUTO, SOSTITUITO, TREND_EMA_INVERTITO)

# motivi per cui un fill non apre la posizione (decisi da size_position / position_sizing)
SKIP_SL_DISTANCE = "SALTATO_DISTANZA_SL_NULLA_O_NEGATIVA"
SKIP_MIN_LOT = "SALTATO_SOTTO_MIN_LOT"

# tipi di punto dello zigzag
PIVOT_HIGH = "H"
PIVOT_LOW = "L"


# ---------------------------------------------------------------------------
# Strutture dati dell'interfaccia
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candle:
    """Candela M5 CHIUSA. `time` = orario di apertura in UTC (datetime o pandas.Timestamp, con fuso UTC)."""
    time: Any
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Position:
    """Posizione aperta (None se flat). qty in lotti (1 lotto = CONTRACT_SIZE_OZ once)."""
    side: str
    entry: float
    sl: float
    tp: float
    qty: float


@dataclass
class PendingOrder:
    """
    Ordine limite pendente (None se assente). Lo crea l'esecuzione (backtest o live) a partire da un
    PLACE_LIMIT. Il contatore `candles_alive` è gestito dalla strategia (lo aggiorna a ogni chiusura).
    """
    id: str
    side: str
    limit_price: float
    sl: float
    tp: float
    created_time: Any
    candles_alive: int = 0
    range_high: float = float("nan")
    range_low: float = float("nan")


@dataclass(frozen=True)
class Action:
    """
    Decisione della strategia. Solo due tipi:
    - PLACE_LIMIT: id, side, limit_price, sl, tp e livelli del range;
    - CANCEL: id, reason.
    Non esiste un'azione di chiusura della posizione.
    """
    type: str
    id: str
    side: Optional[str] = None
    limit_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_high_time: Any = None
    range_low_time: Any = None
    reason: Optional[str] = None


@dataclass
class SwingPoint:
    """Punto dello zigzag (swing pivot confermato)."""
    kind: str          # PIVOT_HIGH / PIVOT_LOW
    price: float
    index: int         # indice della candela del pivot nella sequenza alimentata
    time: Any
    confirm_index: int  # indice della candela di conferma (index + PIVOT_RIGHT)


# ---------------------------------------------------------------------------
# Funzioni pure (usate sia dal backtest sia dal live)
# ---------------------------------------------------------------------------
def fib_level(side: str, range_high: float, range_low: float, level: float) -> float:
    """
    Livello di Fibonacci con la formula dell'indicatore:
    long:  range_low  + (range_high - range_low) * (1 - level)
    short: range_high - (range_high - range_low) * (1 - level)
    0 = estremo finale dell'impulso, 1 = origine dell'impulso, 0.5 = midline.
    """
    span = range_high - range_low
    if side == LONG:
        return range_low + span * (1.0 - level)
    if side == SHORT:
        return range_high - span * (1.0 - level)
    raise ValueError(f"side non valido: {side}")


def take_profit(side: str, range_high: float, range_low: float) -> float:
    """TP fisso al livello TP_LEVEL (0.0 = estremo dell'impulso), definito dal range e non dal fill."""
    return fib_level(side, range_high, range_low, TP_LEVEL)


def stop_loss(side: str, range_high: float, range_low: float) -> float:
    """SL esattamente sull'origine dell'impulso (range_low per i long, range_high per gli short)."""
    if side == LONG:
        return range_low
    if side == SHORT:
        return range_high
    raise ValueError(f"side non valido: {side}")


def entry_price(side: str, range_high: float, range_low: float) -> float:
    """Prezzo del limite: livello ENTRY_LEVEL (0.705, sweet spot della zona OTE)."""
    return fib_level(side, range_high, range_low, ENTRY_LEVEL)


def position_sizing(equity: float, entry: float, sl: float, side: str) -> Tuple[float, Optional[str]]:
    """
    Quantità in lotti per rischiare RISK_PER_TRADE dell'equity sulla distanza entry-SL.
    `entry` è il prezzo di fill effettivo (il limite, oppure l'open se la candela apre oltre il limite).
    Restituisce (qty, motivo_scarto): qty = 0 e un motivo se il trade va saltato:
    - distanza dallo SL nulla o negativa (gap oltre lo SL) -> SKIP_SL_DISTANCE;
    - quantità sotto MIN_LOT dopo l'arrotondamento per difetto -> SKIP_MIN_LOT.
    L'arrotondamento è sempre PER DIFETTO al LOT_STEP: il rischio effettivo non supera mai l'1%.
    """
    if side == LONG:
        dist = entry - sl
    elif side == SHORT:
        dist = sl - entry
    else:
        raise ValueError(f"side non valido: {side}")
    if not (dist > 0.0) or not math.isfinite(dist):
        return 0.0, SKIP_SL_DISTANCE
    if not (equity > 0.0):
        return 0.0, SKIP_MIN_LOT
    risk_usd = equity * RISK_PER_TRADE
    raw_lots = risk_usd / (dist * CONTRACT_SIZE_OZ)
    steps = math.floor(raw_lots / LOT_STEP + 1e-9)
    # protezione contro errori di arrotondamento in virgola mobile: mai oltre il rischio
    while steps > 0 and steps * LOT_STEP * dist * CONTRACT_SIZE_OZ > risk_usd * (1.0 + 1e-12):
        steps -= 1
    qty = round(steps * LOT_STEP, 8)
    if qty < MIN_LOT - 1e-12:
        return 0.0, SKIP_MIN_LOT
    return qty, None


def size_position(equity: float, entry: float, sl: float, side: str = LONG) -> float:
    """Quantità in lotti (0.0 = trade da saltare). Vedi position_sizing per il motivo dello scarto."""
    return position_sizing(equity, entry, sl, side)[0]


def ema_trend(ema_fast: Optional[float], ema_slow: Optional[float]) -> int:
    """+1 rialzista (EMA 50 > EMA 100), -1 ribassista, 0 neutro (uguali o non disponibili)."""
    if ema_fast is None or ema_slow is None:
        return 0
    if ema_fast > ema_slow:
        return 1
    if ema_fast < ema_slow:
        return -1
    return 0


def side_trend(side: str) -> int:
    return 1 if side == LONG else -1


def swing_ema_stats(side: str, gaps: Sequence[Optional[float]],
                    atrs: Sequence[Optional[float]]) -> Tuple[int, float, int, bool]:
    """
    Statistiche delle EMA durante lo swing (candele in ordine cronologico, estremi inclusi).
    gap = EMA 50 - EMA 100 di ogni candela (None se non disponibile), atr = ATR(14) della candela.
    - incroci: cambi di segno di gap tra candele consecutive; gap = 0 (o non disponibile) non ha
      segno e vale il segno precedente;
    - max_sep: massimo di gap/ATR (long) o -gap/ATR (short); -inf se nessuna candela è calcolabile;
    - separated: esiste una candela con gap >= EMA_SEP_MIN_ATR * ATR (long) o -gap >= ... (short).
      La soglia è inclusa e il confronto è fatto con la moltiplicazione (nessun errore di divisione).
    Restituisce (incroci, max_sep, posizione di max_sep nella sequenza o -1, separated).
    """
    sgn = 1.0 if side == LONG else -1.0
    crosses = 0
    prev_sign = 0
    max_sep = -math.inf
    max_pos = -1
    separated = False
    for k, (g, a) in enumerate(zip(gaps, atrs)):
        if g is None:
            continue
        s = 1 if g > 0 else (-1 if g < 0 else 0)
        if s != 0:
            if prev_sign != 0 and s != prev_sign:
                crosses += 1
            prev_sign = s
        if a is not None and a > 0:
            directed = sgn * g
            sep = directed / a
            if sep > max_sep:
                max_sep = sep
                max_pos = k
            if directed >= EMA_SEP_MIN_ATR * a:
                separated = True
    return crosses, max_sep, max_pos, separated


def validate_range(side: str, ema_fast_conf: Optional[float], ema_slow_conf: Optional[float],
                   swing_in_buffer: bool, crosses: int, separated: bool,
                   range_high: float, range_low: float, prev_extreme: Optional[float],
                   atr_conf: Optional[float], touched: bool) -> str:
    """
    Filtri di validità di un dealing range, nell'ordine: il primo che fallisce determina l'esito.
    0.  trend EMA alla candela di conferma concorde con il lato  -> altrimenti SCARTATO_EMA
    0b. swing nel buffer                                          -> altrimenti SCARTATO_SWING_LUNGO
        incroci <= MAX_EMA_CROSSES                                -> altrimenti SCARTATO_EMA_INCROCI
        EMA separate nella direzione giusta in almeno una candela -> altrimenti SCARTATO_EMA_VICINE
    1.  BOS: estremo oltre lo swing precedente dello stesso tipo  -> altrimenti SCARTATO_BOS
    2.  ampiezza >= MIN_RANGE_ATR * ATR(conferma)                 -> altrimenti SCARTATO_AMPIEZZA
    3.  livello di ingresso non ancora toccato                    -> altrimenti SCARTATO_GIA_TOCCATO
    Restituisce VALIDO se tutti i filtri sono superati.
    """
    if ema_trend(ema_fast_conf, ema_slow_conf) != side_trend(side):
        return SCARTATO_EMA
    if not swing_in_buffer:
        return SCARTATO_SWING_LUNGO
    if crosses > MAX_EMA_CROSSES:
        return SCARTATO_EMA_INCROCI
    if not separated:
        return SCARTATO_EMA_VICINE
    if prev_extreme is None:
        return SCARTATO_BOS
    if side == LONG and not (range_high > prev_extreme):
        return SCARTATO_BOS
    if side == SHORT and not (range_low < prev_extreme):
        return SCARTATO_BOS
    if atr_conf is None or not (range_high - range_low >= MIN_RANGE_ATR * atr_conf):
        return SCARTATO_AMPIEZZA
    if touched:
        return SCARTATO_GIA_TOCCATO
    return VALIDO


def pending_cancel_reason(pending_side: str, range_high: float, range_low: float, candle: Candle,
                          ema_fast: Optional[float], ema_slow: Optional[float],
                          candles_alive: int) -> Optional[str]:
    """
    Motivo di cancellazione di un ordine pendente alla chiusura di una candela (priorità nell'ordine):
    NUOVO_ESTREMO (long: high > range_high; short: low < range_low), TREND_EMA_INVERTITO
    (long: EMA 50 <= EMA 100; short: EMA 50 >= EMA 100), SCADUTO (candles_alive >= ORDER_EXPIRY_CANDLES).
    Il caso SOSTITUITO è gestito a parte (nuovo range valido).
    """
    if pending_side == LONG and candle.high > range_high:
        return NUOVO_ESTREMO
    if pending_side == SHORT and candle.low < range_low:
        return NUOVO_ESTREMO
    if ema_trend(ema_fast, ema_slow) != side_trend(pending_side):
        return TREND_EMA_INVERTITO
    if candles_alive >= ORDER_EXPIRY_CANDLES:
        return SCADUTO
    return None


# ---------------------------------------------------------------------------
# Indicatori incrementali
# ---------------------------------------------------------------------------
class WilderATR:
    """
    ATR di Wilder (RMA). TR della prima candela = high - low; poi max(h-l, |h-pc|, |l-pc|).
    Seed esplicito = SMA dei primi `period` TR: disponibile dalla period-esima candela.
    Poi ATR = (ATR_prec * (period - 1) + TR) / period.
    """

    def __init__(self, period: int = ATR_PERIOD):
        self.period = period
        self.count = 0
        self.prev_close: Optional[float] = None
        self._seed_sum = 0.0
        self.value: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        self.count += 1
        if self.count < self.period:
            self._seed_sum += tr
        elif self.count == self.period:
            self._seed_sum += tr
            self.value = self._seed_sum / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class EMA:
    """EMA con alpha = 2/(N+1) e seed = SMA dei primi N valori (disponibile dalla N-esima candela)."""

    def __init__(self, period: int):
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.count = 0
        self._seed_sum = 0.0
        self.value: Optional[float] = None

    def update(self, x: float) -> Optional[float]:
        self.count += 1
        if self.count < self.period:
            self._seed_sum += x
        elif self.count == self.period:
            self._seed_sum += x
            self.value = self._seed_sum / self.period
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


class RingBuffer:
    """Buffer circolare a lunghezza fissa indicizzato con l'indice assoluto della candela."""

    def __init__(self, size: int):
        self.size = size
        self.data: List[Any] = [None] * size

    def put(self, index: int, value: Any) -> None:
        self.data[index % self.size] = value

    def get(self, index: int) -> Any:
        return self.data[index % self.size]


# ---------------------------------------------------------------------------
# Strategia
# ---------------------------------------------------------------------------
class OteFibStrategy:
    """
    Strategia OTE causale. Uso:
        s = OteFibStrategy()
        s.warmup(storico)                       # almeno WARMUP_CANDLES candele chiuse
        actions = s.on_candle_close(candle, position, pending_order)   # a ogni chiusura 15m
    """

    def __init__(self) -> None:
        self.atr = WilderATR(ATR_PERIOD)              # ATR M5: ampiezza del range
        self.atr_trend = WilderATR(ATR_PERIOD)        # ATR H1: separazione delle EMA H1
        self.ema_fast = EMA(EMA_FAST)                 # EMA 50 H1
        self.ema_slow = EMA(EMA_SLOW)                 # EMA 100 H1
        self.trend_candles = 0                        # candele H1 chiuse
        self._h1: Optional[List[Any]] = None          # candela H1 in costruzione: [inizio, o, h, l, c]
        self.index = -1                                   # indice dell'ultima candela alimentata
        self.last_time: Any = None
        self._window: Deque[Tuple[float, float, float, float, int, Any]] = deque(
            maxlen=PIVOT_LEFT + PIVOT_RIGHT + 1)
        # buffer per candela: gap EMA, ATR, high/low reali, orario
        self._buf_gap = RingBuffer(SWING_BUFFER)      # gap EMA H1 valido alla chiusura di ogni M5
        self._buf_atr = RingBuffer(SWING_BUFFER)      # ATR H1 valido alla chiusura di ogni M5
        self._buf_high = RingBuffer(SWING_BUFFER)
        self._buf_low = RingBuffer(SWING_BUFFER)
        self._buf_time = RingBuffer(SWING_BUFFER)
        self.zigzag: List[SwingPoint] = []                 # ultimi punti (lunghezza limitata)
        self._order_seq = 0
        self._order_placed_index: Dict[str, int] = {}
        self._order_range: Dict[str, Dict[str, Any]] = {}
        # diagnostica
        self.last_leg: Optional[Dict[str, Any]] = None
        self.last_cancel: Optional[Dict[str, Any]] = None
        self.legs_this_candle: List[Dict[str, Any]] = []   # range valutati all'ultima chiusura
        self.cancels_this_candle: List[Dict[str, Any]] = []
        self.pivots_this_candle: List[SwingPoint] = []     # pivot confermati all'ultima chiusura
        self.notes: List[str] = []                         # annotazioni (es. contatore ricostruito)

    # ------------------------------------------------------------------ stato
    @property
    def trend(self) -> int:
        return ema_trend(self.ema_fast.value, self.ema_slow.value)

    @property
    def in_warmup(self) -> bool:
        """True finché non ci sono più di WARMUP_CANDLES candele M5 e almeno WARMUP_TREND_CANDLES candele H1 chiuse."""
        return self.index < WARMUP_CANDLES or self.trend_candles < WARMUP_TREND_CANDLES

    # ------------------------------------------------------------------ interfaccia
    def warmup(self, candles: Iterable[Candle]) -> None:
        """Aggiorna ATR, EMA H1, pivot e zigzag senza generare ordini né valutare range."""
        for c in candles:
            self._update_structure(c, evaluate=False)
        self.legs_this_candle = []
        self.cancels_this_candle = []

    def on_candle_close(self, candle: Candle, position: Optional[Position],
                        pending_order: Optional[PendingOrder]) -> List[Action]:
        """
        Elabora una candela M5 chiusa. Aggiorna SEMPRE indicatori, pivot e zigzag.
        Restituisce le Action attive dall'apertura della candela successiva.
        Con una posizione aperta restituisce sempre una lista vuota.
        """
        self.cancels_this_candle = []
        legs = self._update_structure(candle, evaluate=True)
        self.legs_this_candle = legs
        if self.in_warmup:
            return []

        if position is not None:
            for leg in legs:
                if leg["outcome"] == VALIDO:
                    leg["outcome"] = IGNORATO_POSIZIONE_APERTA
            return []

        actions: List[Action] = []
        valid = [leg for leg in legs if leg["outcome"] == VALIDO]
        if valid:
            # Al massimo un range valido per candela: due range sulla stessa candela hanno lati
            # opposti e il filtro EMA ne ammette uno solo. Si usa comunque l'ultimo elaborato.
            leg = valid[-1]
            if pending_order is not None:
                actions.append(self._cancel(pending_order.id, SOSTITUITO))
            actions.append(self._place(leg))
            return actions

        if pending_order is not None:
            alive = self._candles_alive(pending_order)
            reason = pending_cancel_reason(pending_order.side, pending_order.range_high,
                                           pending_order.range_low, candle, self.ema_fast.value,
                                           self.ema_slow.value, alive)
            if reason is not None:
                actions.append(self._cancel(pending_order.id, reason))
        return actions

    # ------------------------------------------------------------------ interni
    def _candles_alive(self, pending: PendingOrder) -> int:
        """Candele M5 alimentate dal piazzamento (escluso) alla candela corrente (inclusa)."""
        placed = self._order_placed_index.get(pending.id)
        if placed is None:
            alive = int(pending.candles_alive) + 1
            self.notes.append(f"ordine {pending.id} sconosciuto: contatore ricostruito ({alive})")
        else:
            alive = self.index - placed
        pending.candles_alive = alive
        return alive

    def _cancel(self, order_id: str, reason: str) -> Action:
        info = {"id": order_id, "reason": reason, "index": self.index, "time": self.last_time}
        self.last_cancel = info
        self.cancels_this_candle.append(info)
        return Action(type=CANCEL, id=order_id, reason=reason)

    def _place(self, leg: Dict[str, Any]) -> Action:
        self._order_seq += 1
        order_id = f"OTE-{self._order_seq:06d}"
        self._order_placed_index[order_id] = self.index
        leg["outcome"] = ORDINE_PIAZZATO
        leg["order_id"] = order_id
        side = leg["side"]
        return Action(
            type=PLACE_LIMIT, id=order_id, side=side, limit_price=leg["level_705"],
            sl=stop_loss(side, leg["range_high"], leg["range_low"]),
            tp=take_profit(side, leg["range_high"], leg["range_low"]),
            range_high=leg["range_high"], range_low=leg["range_low"],
            range_high_time=leg["range_high_time"], range_low_time=leg["range_low_time"],
        )

    @staticmethod
    def trend_bucket(t: Any) -> Any:
        """Inizio della candela H1 che contiene l'orario t (allineata all'ora UTC)."""
        return t - _dt.timedelta(minutes=t.minute % TREND_TIMEFRAME_MINUTES, seconds=t.second,
                                 microseconds=t.microsecond)

    def _close_trend_candle(self) -> None:
        """Chiude la candela H1 in costruzione e aggiorna EMA 50, EMA 100 e ATR H1."""
        _, o, h, l, c = self._h1
        self.atr_trend.update(h, l, c)
        self.ema_fast.update(c)
        self.ema_slow.update(c)
        self.trend_candles += 1
        self._h1 = None

    def _update_trend(self, c: Candle) -> None:
        """Aggrega la candela M5 nella candela H1 corrente; la chiude quando l'ora è completa."""
        bucket = self.trend_bucket(c.time)
        if self._h1 is not None and bucket != self._h1[0]:
            # la candela delle :55 mancava (buco): l'ora precedente è comunque finita
            self._close_trend_candle()
        if self._h1 is None:
            self._h1 = [bucket, c.open, c.high, c.low, c.close]
        else:
            self._h1[2] = max(self._h1[2], c.high)
            self._h1[3] = min(self._h1[3], c.low)
            self._h1[4] = c.close
        if c.time + _dt.timedelta(minutes=TIMEFRAME_MINUTES) >= bucket + _dt.timedelta(minutes=TREND_TIMEFRAME_MINUTES):
            self._close_trend_candle()

    def _update_structure(self, c: Candle, evaluate: bool) -> List[Dict[str, Any]]:
        """Aggiorna indicatori, buffer, pivot e zigzag; valuta i nuovi range se evaluate=True e fuori dal warm-up."""
        if self.last_time is not None and not (c.time > self.last_time):
            raise ValueError(f"candela non in ordine cronologico: {c.time} dopo {self.last_time}")
        self.last_time = c.time
        self.index += 1
        i = self.index
        self.atr.update(c.high, c.low, c.close)
        self._update_trend(c)
        ef, es = self.ema_fast.value, self.ema_slow.value
        gap = (ef - es) if (ef is not None and es is not None) else None
        self._buf_gap.put(i, gap)
        self._buf_atr.put(i, self.atr_trend.value)
        evaluate = evaluate and not self.in_warmup
        self._buf_high.put(i, c.high)
        self._buf_low.put(i, c.low)
        self._buf_time.put(i, c.time)

        if USE_BODIES:
            a_high, a_low = max(c.open, c.close), min(c.open, c.close)
        else:
            a_high, a_low = c.high, c.low
        self._window.append((a_high, a_low, c.open, c.close, i, c.time))

        self.pivots_this_candle = []
        legs: List[Dict[str, Any]] = []
        if len(self._window) < self._window.maxlen:
            return legs
        w = list(self._window)
        ch, cl, co, cc, ci, ct = w[PIVOT_LEFT]
        left = w[:PIVOT_LEFT]
        right = w[PIVOT_LEFT + 1:]
        is_high = ch > max(x[0] for x in left) and ch >= max(x[0] for x in right)
        is_low = cl < min(x[1] for x in left) and cl <= min(x[1] for x in right)
        pivots: List[SwingPoint] = []
        p_high = SwingPoint(PIVOT_HIGH, ch, ci, ct, i) if is_high else None
        p_low = SwingPoint(PIVOT_LOW, cl, ci, ct, i) if is_low else None
        if p_high and p_low:
            # stessa candela swing high e swing low: low -> high se close >= open, altrimenti high -> low
            pivots = [p_low, p_high] if cc >= co else [p_high, p_low]
        elif p_high:
            pivots = [p_high]
        elif p_low:
            pivots = [p_low]
        for p in pivots:
            self.pivots_this_candle.append(p)
            if self._zigzag_add(p) and evaluate:
                leg = self._evaluate_last_range()
                if leg is not None:
                    legs.append(leg)
                    self.last_leg = leg
        return legs

    def _zigzag_add(self, p: SwingPoint) -> bool:
        """Zigzag con alternanza obbligatoria. Restituisce True se l'ultimo punto è cambiato."""
        if not self.zigzag:
            self.zigzag.append(p)
            return True
        last = self.zigzag[-1]
        if p.kind == last.kind:
            if (p.kind == PIVOT_HIGH and p.price > last.price) or (p.kind == PIVOT_LOW and p.price < last.price):
                self.zigzag[-1] = p
                return True
            return False
        self.zigzag.append(p)
        if len(self.zigzag) > 8:
            del self.zigzag[0]
        return True

    def _evaluate_last_range(self) -> Optional[Dict[str, Any]]:
        """Costruisce e valida il dealing range formato dagli ultimi due punti dello zigzag."""
        if len(self.zigzag) < 2:
            return None
        last, prev = self.zigzag[-1], self.zigzag[-2]
        before = self.zigzag[-3] if len(self.zigzag) >= 3 else None
        i = self.index
        if last.kind == PIVOT_HIGH:
            side, hi, lo = LONG, last, prev
            start, end = lo.index, hi.index
        else:
            side, hi, lo = SHORT, prev, last
            start, end = hi.index, lo.index
        rh, rl = hi.price, lo.price
        atr_conf = self.atr.value
        ef, es = self.ema_fast.value, self.ema_slow.value
        lv618 = fib_level(side, rh, rl, OTE_LOW)
        lv705 = fib_level(side, rh, rl, ENTRY_LEVEL)
        lv786 = fib_level(side, rh, rl, OTE_HIGH)

        swing_in_buffer = start >= i - SWING_BUFFER + 1
        crosses, max_sep, max_pos, separated = 0, -math.inf, -1, False
        max_sep_time = None
        if swing_in_buffer:
            idxs = range(start, end + 1)
            gaps = [self._buf_gap.get(k) for k in idxs]
            atrs = [self._buf_atr.get(k) for k in idxs]
            crosses, max_sep, max_pos, separated = swing_ema_stats(side, gaps, atrs)
            if max_pos >= 0:
                max_sep_time = self._buf_time.get(start + max_pos)

        # livello di ingresso già toccato tra la candela dell'estremo finale e la conferma (inclusa)
        touched = False
        if side == LONG:
            for k in range(hi.index, i + 1):
                if self._buf_low.get(k) <= lv705:
                    touched = True
                    break
        else:
            for k in range(lo.index, i + 1):
                if self._buf_high.get(k) >= lv705:
                    touched = True
                    break

        prev_extreme = before.price if before is not None else None
        outcome = validate_range(side, ef, es, swing_in_buffer, crosses, separated, rh, rl,
                                 prev_extreme, atr_conf, touched)
        return {
            "confirm_index": i, "confirm_time": self.last_time, "side": side,
            "range_high": rh, "range_high_time": hi.time, "range_high_index": hi.index,
            "range_low": rl, "range_low_time": lo.time, "range_low_index": lo.index,
            "prev_extreme": prev_extreme, "atr": atr_conf, "atr_trend": self.atr_trend.value,
            "amplitude_atr": (rh - rl) / atr_conf if atr_conf else float("nan"),
            "ema_fast": ef, "ema_slow": es,
            "level_618": lv618, "level_705": lv705, "level_786": lv786,
            "swing_candles": end - start + 1, "crosses": crosses,
            "max_sep": max_sep if max_pos >= 0 else float("nan"), "max_sep_time": max_sep_time,
            "touched": touched, "outcome": outcome, "order_id": None,
        }
