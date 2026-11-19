"""
Strategia Supertrend (entry) + filtro di trend EMA 50/100, timeframe TIMEFRAME_MINUTES (30 minuti).

Questo file contiene TUTTA e SOLO la logica di trading. È lo stesso identico codice
usato dal backtest (backtest.py) e da un eventuale loop live:
- non importa nulla del backtest;
- non accede a file, rete, exchange, API o orologio di sistema;
- riceve candele CHIUSE del timeframe, una alla volta, ed emette decisioni (Action);
- calcola gli indicatori in modo incrementale con stato interno, quindi per
  costruzione non può vedere dati futuri.

Le uscite avvengono SOLO per stop loss o take profit, gestiti dall'esecuzione
(simulata nel backtest, ordini OCO sull'exchange in live). La strategia non emette
mai azioni di chiusura.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Parametri (fissi, NON ottimizzati)
# ---------------------------------------------------------------------------
TIMEFRAME_MINUTES = 30   # timeframe delle candele passate alla strategia (30 minuti)
ST_ATR_PERIOD = 10        # periodo ATR (RMA di Wilder) del Supertrend
ST_MULTIPLIER = 3.0       # moltiplicatore delle bande del Supertrend
EMA_FAST = 50             # EMA veloce (filtro di trend)
EMA_SLOW = 100            # EMA lenta (filtro di trend)
RR = 3.0                  # rapporto rendimento/rischio del take profit
RISK_PER_TRADE = 0.01     # frazione dell'equity rischiata per trade
WARMUP_CANDLES = 500      # candele del timeframe senza segnali all'avvio (backtest e live)

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------
LONG = "LONG"
SHORT = "SHORT"

IS_UP_TREND = 1
IS_DOWN_TREND = -1

ACTION_OPEN = "OPEN"

OUTCOME_ENTRY = "ENTRY"
OUTCOME_REJECTED_EMA = "SCARTATO_EMA"
OUTCOME_IGNORED_POSITION = "IGNORATO_POSIZIONE_APERTA"


# ---------------------------------------------------------------------------
# Strutture dati
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candle:
    """Candela CHIUSA del timeframe (TIMEFRAME_MINUTES). `time` è l'orario di apertura della candela, in UTC."""
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Position:
    """Posizione aperta. Se non c'è posizione si usa None (flat)."""
    side: str      # LONG o SHORT
    entry: float   # prezzo di ingresso effettivo
    sl: float      # stop loss fisso
    tp: float      # take profit fisso
    qty: float     # quantità in BTC


@dataclass(frozen=True)
class Action:
    """
    Decisione emessa dalla strategia. L'unico tipo esistente è OPEN: la strategia
    non chiude mai le posizioni (le uscite sono solo SL/TP).
    Va eseguita all'apertura della candela successiva; TP e quantità si calcolano
    sul prezzo di ingresso effettivo con take_profit() e size_position().
    """
    side: str
    sl: float
    type: str = ACTION_OPEN

    def __post_init__(self) -> None:
        if self.type != ACTION_OPEN:
            raise ValueError("La strategia può emettere solo azioni OPEN")
        if self.side not in (LONG, SHORT):
            raise ValueError(f"side non valido: {self.side}")


@dataclass(frozen=True)
class Signal:
    """Diagnostica di un cambio di trend del Supertrend (nessuna logica qui)."""
    time: datetime          # orario di apertura della candela di cambio
    direction: str          # LONG (passaggio a isUpTrend) o SHORT (a isDownTrend)
    st_line: float          # linea del Supertrend sulla candela di cambio
    ema_fast: float
    ema_slow: float
    outcome: str            # ENTRY / SCARTATO_EMA / IGNORATO_POSIZIONE_APERTA


# ---------------------------------------------------------------------------
# Indicatori incrementali
# ---------------------------------------------------------------------------
class IncrementalEMA:
    """
    EMA sulla chiusura, alpha = 2 / (N + 1).
    Seed esplicito: SMA dei primi N valori; disponibile dall'N-esimo valore.
    """

    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.value: Optional[float] = None
        self._seed_sum = 0.0
        self._count = 0

    def update(self, x: float) -> Optional[float]:
        self._count += 1
        if self.value is None:
            self._seed_sum += x
            if self._count == self.period:
                self.value = self._seed_sum / self.period
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


class IncrementalATR:
    """
    ATR con smoothing di Wilder (RMA).
    TR prima candela = high - low; poi max(high - low, |high - prev close|, |low - prev close|).
    Seed = SMA dei primi N TR (disponibile dalla N-esima candela),
    poi ATR = (prev ATR * (N - 1) + TR) / N.
    """

    def __init__(self, period: int) -> None:
        self.period = period
        self.value: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._seed_sum = 0.0
        self._count = 0

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self._count += 1
        if self.value is None:
            self._seed_sum += tr
            if self._count == self.period:
                self.value = self._seed_sum / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class IncrementalSupertrend:
    """
    Supertrend implementato ESATTAMENTE con le formule di specifica:

        hl2 = (high + low) / 2
        basicUpperBand = hl2 + multiplier * ATR
        basicLowerBand = hl2 - multiplier * ATR
        upperBand = basicUpperBand < prev upperBand or prev close > prev upperBand ? basicUpperBand : prev upperBand
        lowerBand = basicLowerBand > prev lowerBand or prev close < prev lowerBand ? basicLowerBand : prev lowerBand
        trendDirection:
            finché l'ATR non è disponibile: isDownTrend
            altrimenti se la candela precedente era isDownTrend (== prev superTrend == prev upperBand):
                close > upperBand ? isUpTrend : isDownTrend
            altrimenti:
                close < lowerBand ? isDownTrend : isUpTrend
        superTrend = isUpTrend ? lowerBand : upperBand

    Sulla prima candela con ATR disponibile: bande = bande base, direzione = isDownTrend.
    `changed` è True quando la direzione della candela corrente differisce da quella
    della candela precedente (prima dell'ATR la direzione è per definizione isDownTrend).
    """

    def __init__(self, atr_period: int, multiplier: float) -> None:
        self.multiplier = multiplier
        self.atr = IncrementalATR(atr_period)
        self.upper_band: Optional[float] = None
        self.lower_band: Optional[float] = None
        self.direction: int = IS_DOWN_TREND
        self.value: Optional[float] = None
        self.changed: bool = False
        self._prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> Tuple[int, Optional[float], bool]:
        atr = self.atr.update(high, low, close)
        prev_direction = self.direction
        if atr is None:
            # ATR non ancora disponibile: nessuna banda, trend ribassista per definizione
            self.direction = IS_DOWN_TREND
            self.value = None
        else:
            hl2 = (high + low) / 2.0
            basic_upper = hl2 + self.multiplier * atr
            basic_lower = hl2 - self.multiplier * atr
            if self.upper_band is None:
                # prima candela con ATR: nessun valore precedente
                upper = basic_upper
                lower = basic_lower
                direction = IS_DOWN_TREND
            else:
                prev_upper = self.upper_band
                prev_lower = self.lower_band
                prev_close = self._prev_close
                upper = basic_upper if (basic_upper < prev_upper or prev_close > prev_upper) else prev_upper
                lower = basic_lower if (basic_lower > prev_lower or prev_close < prev_lower) else prev_lower
                if prev_direction == IS_DOWN_TREND:
                    direction = IS_UP_TREND if close > upper else IS_DOWN_TREND
                else:
                    direction = IS_DOWN_TREND if close < lower else IS_UP_TREND
            self.upper_band = upper
            self.lower_band = lower
            self.direction = direction
            self.value = lower if direction == IS_UP_TREND else upper
        self.changed = self.direction != prev_direction
        self._prev_close = close
        return self.direction, self.value, self.changed


# ---------------------------------------------------------------------------
# Funzioni pure condivise da backtest e live
# ---------------------------------------------------------------------------
def ema_trend(ema_fast: Optional[float], ema_slow: Optional[float]) -> Optional[str]:
    """Trend dato dalle EMA: LONG se EMA veloce > lenta, SHORT se <, None se uguali o non disponibili."""
    if ema_fast is None or ema_slow is None:
        return None
    if ema_fast > ema_slow:
        return LONG
    if ema_fast < ema_slow:
        return SHORT
    return None


def evaluate_trend_change(
    new_direction: int,
    st_line: float,
    ema_fast: Optional[float],
    ema_slow: Optional[float],
    position: Optional[Position],
) -> Tuple[List[Action], str]:
    """
    Regole di apertura, valutate sulla candela di cambio di trend alla sua chiusura.
    - Con una posizione aperta il cambio è ignorato (non messo in coda).
    - LONG: passaggio a isUpTrend con EMA veloce > lenta.
    - SHORT: passaggio a isDownTrend con EMA veloce < lenta.
    - SL = linea del Supertrend sulla candela di cambio, senza buffer.
    Restituisce (lista di Action, esito).
    """
    side = LONG if new_direction == IS_UP_TREND else SHORT
    if position is not None:
        return [], OUTCOME_IGNORED_POSITION
    if ema_trend(ema_fast, ema_slow) != side:
        return [], OUTCOME_REJECTED_EMA
    return [Action(side=side, sl=st_line)], OUTCOME_ENTRY


def stop_distance_valid(side: str, entry: float, sl: float) -> bool:
    """La distanza tra ingresso effettivo e SL deve essere strettamente positiva nel verso giusto."""
    if side == LONG:
        return entry - sl > 0.0
    if side == SHORT:
        return sl - entry > 0.0
    raise ValueError(f"side non valido: {side}")


def size_position(equity: float, entry: float, sl: float, side: str) -> float:
    """
    Quantità (in BTC) tale che, se lo SL viene colpito al suo livello, la perdita lorda
    sia RISK_PER_TRADE * equity. Calcolata sul prezzo di ingresso EFFETTIVO.
    Restituisce 0.0 se la distanza dallo SL è nulla o negativa (gap): trade da saltare.
    I costi (commissioni, slippage) non sono inclusi nel rischio.
    """
    if equity <= 0.0 or not stop_distance_valid(side, entry, sl):
        return 0.0
    return (equity * RISK_PER_TRADE) / abs(entry - sl)


def take_profit(side: str, entry: float, sl: float) -> float:
    """TP fisso a RR volte il rischio, calcolato sul prezzo di ingresso effettivo."""
    if side == LONG:
        return entry + RR * (entry - sl)
    if side == SHORT:
        return entry - RR * (sl - entry)
    raise ValueError(f"side non valido: {side}")


# ---------------------------------------------------------------------------
# Strategia
# ---------------------------------------------------------------------------
class SupertrendEmaStrategy:
    """
    Strategia a stato interno. Uso (identico in backtest e live):
        strategy = SupertrendEmaStrategy()
        strategy.warmup(storico_candele_chiuse)            # almeno WARMUP_CANDLES candele
        actions = strategy.on_candle_close(candle, position)   # a ogni chiusura di candela
    """

    def __init__(self) -> None:
        self.supertrend = IncrementalSupertrend(ST_ATR_PERIOD, ST_MULTIPLIER)
        self.ema_fast = IncrementalEMA(EMA_FAST)
        self.ema_slow = IncrementalEMA(EMA_SLOW)
        self.candles_seen = 0
        self.last_time: Optional[datetime] = None
        # ultimo cambio di trend valutato (diagnostica); None finché non ce n'è uno
        self.last_signal: Optional[Signal] = None
        # True se l'ultima candela passata a on_candle_close ha prodotto un cambio valutato
        self.signal_on_last_candle = False

    # -- stato -------------------------------------------------------------
    @property
    def ready(self) -> bool:
        """True quando il warm-up è completo e la strategia può generare segnali."""
        return self.candles_seen >= WARMUP_CANDLES

    def _update_indicators(self, candle: Candle) -> None:
        """Aggiorna tutti gli indicatori con una candela chiusa (sempre, anche in posizione)."""
        if self.last_time is not None and candle.time <= self.last_time:
            raise ValueError(f"Candele non in ordine cronologico: {candle.time} <= {self.last_time}")
        self.last_time = candle.time
        self.supertrend.update(candle.high, candle.low, candle.close)
        self.ema_fast.update(candle.close)
        self.ema_slow.update(candle.close)
        self.candles_seen += 1

    # -- interfaccia -------------------------------------------------------
    def warmup(self, candles: Iterable[Candle]) -> None:
        """Aggiorna gli indicatori con candele storiche chiuse senza generare segnali."""
        for candle in candles:
            self._update_indicators(candle)
        self.signal_on_last_candle = False

    def on_candle_close(self, candle: Candle, position: Optional[Position]) -> List[Action]:
        """
        Da chiamare alla chiusura di ogni candela del timeframe.
        Aggiorna SEMPRE indicatori e Supertrend; restituisce [] oppure [Action OPEN]
        da eseguire all'apertura della candela successiva. Con posizione aperta: sempre [].
        Durante le prime WARMUP_CANDLES candele non genera segnali.
        """
        was_ready = self.ready
        self._update_indicators(candle)
        self.signal_on_last_candle = False
        # i segnali partono solo dalla candela successiva alle prime WARMUP_CANDLES
        if not was_ready or not self.supertrend.changed:
            return []
        actions, outcome = evaluate_trend_change(
            self.supertrend.direction,
            self.supertrend.value,
            self.ema_fast.value,
            self.ema_slow.value,
            position,
        )
        self.last_signal = Signal(
            time=candle.time,
            direction=LONG if self.supertrend.direction == IS_UP_TREND else SHORT,
            st_line=self.supertrend.value,
            ema_fast=self.ema_fast.value,
            ema_slow=self.ema_slow.value,
            outcome=outcome,
        )
        self.signal_on_last_candle = True
        return actions
