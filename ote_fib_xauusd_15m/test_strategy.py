"""
Test della strategia OTE (strategy.py) e del motore di backtest (backtest.py).
Esecuzione: python test_strategy.py   (non serve pytest)

Contenuto:
- confronto di ATR, EMA, trend, pivot, zigzag e dealing range con un'implementazione di riferimento
  indipendente (loop numpy sull'intera storia, che segue alla lettera le definizioni);
- assenza di look-ahead (troncamento della storia), determinismo, sizing;
- casi limite su dati sintetici costruiti apposta;
- lettura dei due formati HistData, classificazione dei buchi e verifica del fuso.
I dati usati sono SINTETICI: servono solo a verificare il codice, non dicono nulla sulla strategia.
"""

from __future__ import annotations

import io
import math
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402
import strategy as st  # noqa: E402

TOL = 1e-6
NY = "America/New_York"


# ---------------------------------------------------------------------------
# Generatori di dati sintetici
# ---------------------------------------------------------------------------
def market_minutes(start: str, end: str) -> pd.DatetimeIndex:
    """Minuti UTC di mercato aperto dell'oro: domenica 18:00 NY -> venerdì 17:00 NY, pausa 17-18 NY."""
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), freq="1min", inclusive="left")
    ny = idx.tz_convert(NY)
    wd, hr = ny.weekday, ny.hour
    open_ = np.where(wd == 5, False, np.where(wd == 6, hr >= 18, np.where(wd == 4, hr < 17, hr != 17)))
    return idx[open_].as_unit("ns")


def synthetic_minutes(start: str = "2023-01-01", end: str = "2023-03-15", seed: int = 7,
                      base: float = 1850.0) -> pd.DataFrame:
    """Random walk 1m con regimi di drift (trend alternati) sugli orari reali dell'oro, prezzi > 0."""
    rng = np.random.default_rng(seed)
    idx = market_minutes(start, end)
    n = len(idx)
    regime_len = rng.integers(600, 3000, size=n // 600 + 2)
    drift = np.repeat(rng.normal(0, 0.03, size=len(regime_len)), regime_len)[:n]
    ret = drift + rng.normal(0, 0.25, size=n)
    close = base + np.cumsum(ret)
    open_ = np.r_[base, close[:-1]] + rng.normal(0, 0.02, size=n)
    wick_up = np.abs(rng.normal(0, 0.12, size=n))
    wick_dn = np.abs(rng.normal(0, 0.12, size=n))
    high = np.maximum(open_, close) + wick_up
    low = np.minimum(open_, close) - wick_dn
    return pd.DataFrame({"time_ns": idx.asi8, "open": open_.round(3), "high": high.round(3), "low": low.round(3),
                         "close": close.round(3), "volume": np.zeros(n), "source_file": "sintetico"})


def to_histdata_generic(df: pd.DataFrame, tz_hours: float = -5.0) -> str:
    local = pd.to_datetime(df["time_ns"], utc=True).dt.tz_localize(None) + pd.Timedelta(hours=tz_hours)
    lines = [f"{t:%Y%m%d %H%M%S};{o:.6f};{h:.6f};{l:.6f};{c:.6f};0"
             for t, o, h, l, c in zip(local, df["open"], df["high"], df["low"], df["close"])]
    return "\n".join(lines) + "\n"


def to_histdata_mt(df: pd.DataFrame, tz_hours: float = -5.0) -> str:
    local = pd.to_datetime(df["time_ns"], utc=True).dt.tz_localize(None) + pd.Timedelta(hours=tz_hours)
    lines = [f"{t:%Y.%m.%d},{t:%H:%M},{o:.3f},{h:.3f},{l:.3f},{c:.3f},0"
             for t, o, h, l, c in zip(local, df["open"], df["high"], df["low"], df["close"])]
    return "\n".join(lines) + "\n"


def candles_from_closes(closes: List[float], wick: float = 0.2, start: str = "2023-01-02 00:00") -> List[st.Candle]:
    """Candele 15m consecutive: open = close precedente, high/low = max/min + wick."""
    t0 = pd.Timestamp(start, tz="UTC")
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(st.Candle(t0 + pd.Timedelta(minutes=15 * i), o, max(o, c) + wick, min(o, c) - wick, c, 0.0))
        prev = c
    return out


def sine_trend_closes(n: int, slope: float = 0.5, amp: float = 8.0, period: int = 40, base: float = 1000.0) -> List[float]:
    """Trend rialzista con oscillazioni regolari: pivot alternati, BOS a ogni massimo, EMA ben separate."""
    return [base + slope * i + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def md_from_candles(candles: List[st.Candle]) -> bt.MarketData:
    """MarketData 1m da candele 15m: 15 candele 1m per candela, interpolazione lineare open -> close."""
    rows = []
    for c in candles:
        t0 = pd.Timestamp(c.time).value
        path = np.linspace(c.open, c.close, 16)
        for j in range(15):
            o, cl = path[j], path[j + 1]
            h, l = max(o, cl), min(o, cl)
            if j == 5:
                h = c.high
            if j == 9:
                l = c.low
            rows.append((t0 + j * bt.MINUTE_NS, o, max(h, o, cl), min(l, o, cl), cl))
    df = pd.DataFrame(rows, columns=["time_ns", "open", "high", "low", "close"])
    df["volume"] = 0.0
    return bt.build_market_data(df)


# ---------------------------------------------------------------------------
# Implementazione di riferimento indipendente (storia completa, nessuno stato incrementale)
# ---------------------------------------------------------------------------
def ref_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = st.ATR_PERIOD) -> np.ndarray:
    n = len(c)
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = np.full(n, np.nan)
    if n >= period:
        out[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def ref_ema(c: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(c), np.nan)
    if len(c) >= period:
        a = 2.0 / (period + 1)
        out[period - 1] = np.mean(c[:period])
        for i in range(period, len(c)):
            out[i] = a * c[i] + (1 - a) * out[i - 1]
    return out


def ref_pivots(o, h, l, c) -> List[Tuple[int, str, float, int]]:
    """(indice, tipo, prezzo, candela di conferma), nell'ordine di elaborazione."""
    L, R = st.PIVOT_LEFT, st.PIVOT_RIGHT
    out = []
    for i in range(L, len(c) - R):
        hi = h[i] > np.max(h[i - L:i]) and h[i] >= np.max(h[i + 1:i + R + 1])
        lo = l[i] < np.min(l[i - L:i]) and l[i] <= np.min(l[i + 1:i + R + 1])
        ev = []
        if hi:
            ev.append((i, "H", float(h[i]), i + R))
        if lo:
            ev.append((i, "L", float(l[i]), i + R))
        if hi and lo:
            ev = sorted(ev, key=lambda e: 0 if (e[1] == "L") == (c[i] >= o[i]) else 1)
        out.extend(ev)
    return out


def ref_ranges(o, h, l, c) -> List[Dict]:
    """Zigzag + dealing range con esito (strategia sempre flat e senza ordini pendenti)."""
    atr = ref_atr(h, l, c)
    ef, es = ref_ema(c, st.EMA_FAST), ref_ema(c, st.EMA_SLOW)
    zz: List[Tuple[int, str, float]] = []
    out = []
    for (i, kind, price, conf) in ref_pivots(o, h, l, c):
        if not zz:
            zz.append((i, kind, price))
            changed = True
        elif zz[-1][1] == kind:
            better = price > zz[-1][2] if kind == "H" else price < zz[-1][2]
            if better:
                zz[-1] = (i, kind, price)
            changed = better
        else:
            zz.append((i, kind, price))
            changed = True
        if not changed or conf < st.WARMUP_CANDLES or len(zz) < 2:
            continue
        last, prev = zz[-1], zz[-2]
        before = zz[-3] if len(zz) >= 3 else None
        bull = last[1] == "H"
        hi_pt, lo_pt = (last, prev) if bull else (prev, last)
        rh, rl = hi_pt[2], lo_pt[2]
        span = rh - rl
        entry = rl + span * (1 - st.ENTRY_LEVEL) if bull else rh - span * (1 - st.ENTRY_LEVEL)
        a, b = (lo_pt[0], hi_pt[0]) if bull else (hi_pt[0], lo_pt[0])
        in_buf = a >= conf - st.SWING_BUFFER + 1
        gap = ef[a:b + 1] - es[a:b + 1]
        sw_atr = atr[a:b + 1]
        # incroci: segni non nulli consecutivi diversi (gap = 0 o NaN non ha segno)
        signs = np.sign(np.nan_to_num(gap, nan=0.0))
        nz = signs[signs != 0]
        crosses = int(np.sum(nz[1:] != nz[:-1])) if len(nz) > 1 else 0
        directed = gap if bull else -gap
        ok = np.isfinite(directed) & np.isfinite(sw_atr) & (sw_atr > 0)
        ratio = np.where(ok, directed / np.where(ok, sw_atr, 1.0), -np.inf)
        max_sep = float(np.max(ratio)) if ok.any() else float("nan")
        separated = bool(np.any(ok & (directed >= st.EMA_SEP_MIN_ATR * np.nan_to_num(sw_atr))))
        trend = 0 if not (np.isfinite(ef[conf]) and np.isfinite(es[conf])) else int(np.sign(ef[conf] - es[conf]))
        if bull:
            touched = bool(np.any(l[hi_pt[0]:conf + 1] <= entry))
        else:
            touched = bool(np.any(h[lo_pt[0]:conf + 1] >= entry))
        if trend != (1 if bull else -1):
            res = st.SCARTATO_EMA
        elif not in_buf:
            res = st.SCARTATO_SWING_LUNGO
        elif crosses > st.MAX_EMA_CROSSES:
            res = st.SCARTATO_EMA_INCROCI
        elif not separated:
            res = st.SCARTATO_EMA_VICINE
        elif before is None or (bull and not rh > before[2]) or (not bull and not rl < before[2]):
            res = st.SCARTATO_BOS
        elif not (span >= st.MIN_RANGE_ATR * atr[conf]):
            res = st.SCARTATO_AMPIEZZA
        elif touched:
            res = st.SCARTATO_GIA_TOCCATO
        else:
            res = st.ORDINE_PIAZZATO
        out.append({"confirm_index": conf, "side": st.LONG if bull else st.SHORT, "range_high": rh, "range_low": rl,
                    "range_high_index": hi_pt[0], "range_low_index": lo_pt[0], "crosses": crosses,
                    "max_sep": max_sep, "outcome": res})
    return out


def run_strategy_flat(candles: List[st.Candle]) -> Tuple[st.OteFibStrategy, List[Dict], List[Tuple]]:
    """Alimenta la strategia sempre flat e senza ordini; restituisce range e pivot (con conferma)."""
    s = st.OteFibStrategy()
    legs, pivots = [], []
    for c in candles:
        s.on_candle_close(c, None, None)
        legs.extend(dict(x) for x in s.legs_this_candle)
        pivots.extend((p.index, p.kind, p.price, s.index) for p in s.pivots_this_candle)
    return s, legs, pivots


def arrays(candles: List[st.Candle]):
    return (np.array([c.open for c in candles]), np.array([c.high for c in candles]),
            np.array([c.low for c in candles]), np.array([c.close for c in candles]))


_CACHE: Dict = {}


def synthetic_md() -> bt.MarketData:
    if "md" not in _CACHE:
        _CACHE["md"] = bt.build_market_data(synthetic_minutes())
    return _CACHE["md"]


def synthetic_candles() -> List[st.Candle]:
    md = synthetic_md()
    return [md.candle(k) for k in range(len(md.t15))]


# ---------------------------------------------------------------------------
# Test: indicatori e struttura vs riferimento
# ---------------------------------------------------------------------------
def test_indicators_vs_reference():
    candles = synthetic_candles()
    o, h, l, c = arrays(candles)
    atr_ref, ef_ref, es_ref = ref_atr(h, l, c), ref_ema(c, st.EMA_FAST), ref_ema(c, st.EMA_SLOW)
    s = st.OteFibStrategy()
    atr_inc, ef_inc, es_inc, trend_inc = [], [], [], []
    for cd in candles:
        s.on_candle_close(cd, None, None)
        atr_inc.append(np.nan if s.atr.value is None else s.atr.value)
        ef_inc.append(np.nan if s.ema_fast.value is None else s.ema_fast.value)
        es_inc.append(np.nan if s.ema_slow.value is None else s.ema_slow.value)
        trend_inc.append(s.trend)
    atr_inc, ef_inc, es_inc = map(np.array, (atr_inc, ef_inc, es_inc))
    # disponibilità esatta dal seed
    assert np.isnan(atr_inc[st.ATR_PERIOD - 2]) and not np.isnan(atr_inc[st.ATR_PERIOD - 1])
    assert np.isnan(es_inc[st.EMA_SLOW - 2]) and not np.isnan(es_inc[st.EMA_SLOW - 1])
    w = st.WARMUP_CANDLES
    assert np.max(np.abs(atr_inc[w:] - atr_ref[w:])) < TOL
    assert np.max(np.abs(ef_inc[w:] - ef_ref[w:])) < TOL
    assert np.max(np.abs(es_inc[w:] - es_ref[w:])) < TOL
    assert np.max(np.abs(atr_inc[st.ATR_PERIOD - 1:] - atr_ref[st.ATR_PERIOD - 1:])) < TOL
    trend_ref = np.where(np.isfinite(ef_ref) & np.isfinite(es_ref), np.sign(ef_ref - es_ref), 0).astype(int)
    assert (np.array(trend_inc) == trend_ref).all(), "segno del trend EMA diverso dal riferimento"
    print(f"    candele 15m: {len(candles)}; trend rialzista {int((trend_ref == 1).sum())}, "
          f"ribassista {int((trend_ref == -1).sum())}")


def test_pivots_vs_reference():
    candles = synthetic_candles()
    o, h, l, c = arrays(candles)
    _, _, piv = run_strategy_flat(candles)
    ref = [(i, k, p, conf) for (i, k, p, conf) in ref_pivots(o, h, l, c)]
    assert len(piv) == len(ref) and len(ref) > 100, (len(piv), len(ref))
    for a, b in zip(piv, ref):
        assert a[0] == b[0] and a[1] == b[1] and a[2] == b[2] and a[3] == b[3], (a, b)
    print(f"    pivot confermati: {len(ref)} (coincidenza 100%, anche la candela di conferma)")


def test_ranges_vs_reference():
    candles = synthetic_candles()
    o, h, l, c = arrays(candles)
    _, legs, _ = run_strategy_flat(candles)
    ref = ref_ranges(o, h, l, c)
    assert len(legs) == len(ref) and len(ref) > 50, (len(legs), len(ref))
    for a, b in zip(legs, ref):
        for key in ["confirm_index", "side", "range_high", "range_low", "range_high_index", "range_low_index",
                    "crosses", "outcome"]:
            assert a[key] == b[key], (key, a, b)
        assert (math.isnan(a["max_sep"]) and math.isnan(b["max_sep"])) or abs(a["max_sep"] - b["max_sep"]) < TOL, (a, b)
    counts = pd.Series([x["outcome"] for x in ref]).value_counts().to_dict()
    print(f"    dealing range: {len(ref)} (coincidenza 100%); esiti: {counts}")
    assert counts.get(st.ORDINE_PIAZZATO, 0) > 0


# ---------------------------------------------------------------------------
# Test: look-ahead e determinismo
# ---------------------------------------------------------------------------
def test_no_lookahead():
    md = synthetic_md()
    res = bt.run_replay(md, record_actions=True, verbose=False)
    candles = [md.candle(k) for k in range(len(md.t15))]
    actions_by_k = {k: a for k, a in res.actions_log}
    inputs_by_k = {k: (p, q) for k, p, q in res.inputs_log}
    with_actions = [k for k, a in res.actions_log if a]
    n_place = sum(1 for _, a in res.actions_log for x in a if x.type == st.PLACE_LIMIT)
    n_cancel = sum(1 for _, a in res.actions_log for x in a if x.type == st.CANCEL)
    assert n_place > 5 and n_cancel > 3, (n_place, n_cancel)
    rng = np.random.default_rng(1)
    others = [k for k, _ in res.actions_log if k not in set(with_actions)]
    sample = sorted(set(with_actions) | set(rng.choice(others, size=min(60, len(others)), replace=False).tolist()))
    assert len(sample) >= 50
    for N in sample:
        s = st.OteFibStrategy()
        s.warmup(candles[:st.WARMUP_CANDLES])
        acts = None
        for k in range(st.WARMUP_CANDLES, N + 1):
            p, q = inputs_by_k[k]
            p = None if p is None else st.Position(**vars(p))
            q = None if q is None else st.PendingOrder(**vars(q))
            acts = s.on_candle_close(candles[k], p, q)
        assert acts == actions_by_k[N], (N, acts, actions_by_k[N])
    # verifica end-to-end: dati 1m troncati alla fine della candela N -> stesse Action alla candela N
    for N in with_actions[:3] + with_actions[-2:]:
        cut = int(md.end[N])
        df = pd.DataFrame({"time_ns": md.t1[:cut], "open": md.o1[:cut], "high": md.h1[:cut], "low": md.l1[:cut],
                           "close": md.c1[:cut], "volume": 0.0})
        r2 = bt.run_replay(bt.build_market_data(df), record_actions=True, verbose=False)
        assert dict(r2.actions_log)[N] == actions_by_k[N]
    print(f"    punti campionati: {len(sample)} (tutti i PLACE_LIMIT={n_place} e CANCEL={n_cancel})")


def test_determinism():
    md = synthetic_md()
    a = bt.run_replay(md, verbose=False).trades
    b = bt.run_replay(md, verbose=False).trades
    assert len(a) > 0
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    print(f"    trade identici in due run: {len(a)}")


# ---------------------------------------------------------------------------
# Test: sizing e funzioni pure
# ---------------------------------------------------------------------------
def test_sizing():
    q, r = st.position_sizing(10_000, 2000.0, 1990.0, st.LONG)     # rischio 100 USD, 10 USD/oz -> 0.10 lotti
    assert q == 0.10 and r is None
    q, _ = st.position_sizing(10_000, 2000.0, 1993.0, st.LONG)      # 100/(7*100)=0.1428 -> 0.14
    assert q == 0.14
    q, r = st.position_sizing(10_000, 2000.0, 1000.0, st.LONG)      # 0.001 lotti -> sotto MIN_LOT
    assert q == 0.0 and r == st.SKIP_MIN_LOT
    q, r = st.position_sizing(10_000, 1990.0, 1990.0, st.LONG)      # distanza nulla
    assert q == 0.0 and r == st.SKIP_SL_DISTANCE
    q, r = st.position_sizing(10_000, 1985.0, 1990.0, st.LONG)      # gap oltre lo SL
    assert q == 0.0 and r == st.SKIP_SL_DISTANCE
    q, r = st.position_sizing(10_000, 2010.0, 2000.0, st.SHORT)     # short con SL sotto il fill
    assert q == 0.0 and r == st.SKIP_SL_DISTANCE
    assert st.size_position(10_000, 2000.0, 2010.0, st.SHORT) == 0.10
    rng = np.random.default_rng(3)
    for _ in range(20000):
        eq = float(rng.uniform(500, 1e6))
        e = float(rng.uniform(1500, 3000))
        d = float(rng.uniform(0.01, 60))
        q = st.size_position(eq, e, e - d, st.LONG)
        assert abs(q / st.LOT_STEP - round(q / st.LOT_STEP)) < 1e-9
        assert q == 0 or q >= st.MIN_LOT
        assert q * d * st.CONTRACT_SIZE_OZ <= eq * st.RISK_PER_TRADE * (1 + 1e-9)
        nxt = q + st.LOT_STEP                                         # arrotondamento PER DIFETTO
        assert nxt * d * st.CONTRACT_SIZE_OZ > eq * st.RISK_PER_TRADE * (1 - 1e-9)
    # livelli
    assert st.fib_level(st.LONG, 110, 100, 0.0) == 110 and st.fib_level(st.LONG, 110, 100, 1.0) == 100
    assert st.fib_level(st.SHORT, 110, 100, 0.0) == 100 and st.fib_level(st.SHORT, 110, 100, 1.0) == 110
    assert abs(st.fib_level(st.LONG, 110, 100, 0.705) - 102.95) < 1e-12
    assert abs(st.fib_level(st.SHORT, 110, 100, 0.705) - 107.05) < 1e-12
    assert st.take_profit(st.LONG, 110, 100) == 110 and st.take_profit(st.SHORT, 110, 100) == 100


# ---------------------------------------------------------------------------
# Test: casi limite su dati sintetici
# ---------------------------------------------------------------------------
def test_pivot_known_only_after_confirmation():
    closes = [100.0] * 30
    closes[15] = 110.0           # picco isolato sulla candela 15
    candles = candles_from_closes(closes, wick=0.0)
    s = st.OteFibStrategy()
    seen_at = None
    for i, c in enumerate(candles):
        s.on_candle_close(c, None, None)
        for p in s.pivots_this_candle:
            if p.kind == st.PIVOT_HIGH and p.index == 15:
                seen_at = i
    assert seen_at == 15 + st.PIVOT_RIGHT, seen_at
    # nessun pivot noto prima: controllo con la strategia alimentata fino a i+4
    s2 = st.OteFibStrategy()
    for c in candles[:15 + st.PIVOT_RIGHT]:
        s2.on_candle_close(c, None, None)
    assert all(p.index != 15 for p in s2.zigzag)


def test_equal_highs_tie_rule():
    # massimi uguali sulle candele 10 e 12: a destra basta >=, a sinistra serve >
    highs = [100.0] * 25
    highs[10] = highs[12] = 105.0
    candles = [st.Candle(pd.Timestamp("2023-01-02", tz="UTC") + pd.Timedelta(minutes=15 * i), 99.0, h, 98.0, 99.0)
               for i, h in enumerate(highs)]
    _, _, piv = run_strategy_flat(candles)
    highs_found = [(i, conf) for (i, k, p, conf) in piv if k == "H"]
    assert highs_found == [(10, 15)], highs_found
    # stessa candela swing high e swing low: low -> high se close >= open, altrimenti high -> low
    for o_, c_, expected in [(99.0, 99.5, ["L", "H"]), (99.5, 99.0, ["H", "L"])]:
        cs = [st.Candle(pd.Timestamp("2023-01-02", tz="UTC") + pd.Timedelta(minutes=15 * i), 99.0, 100.0, 98.0, 99.0)
              for i in range(20)]
        cs[8] = st.Candle(cs[8].time, o_, 110.0, 90.0, c_)
        _, _, pv = run_strategy_flat(cs)
        assert [k for (i, k, p, conf) in pv if i == 8] == expected


def test_ema_filter_rules():
    common = dict(swing_in_buffer=True, crosses=0, separated=True, range_high=110.0, range_low=100.0,
                  prev_extreme=None, atr_conf=1.0, touched=False)
    # rialzista con EMA50 < EMA100 -> SCARTATO_EMA; ribassista con EMA50 > EMA100 -> SCARTATO_EMA
    assert st.validate_range(st.LONG, 99.0, 100.0, **dict(common, prev_extreme=105.0)) == st.SCARTATO_EMA
    assert st.validate_range(st.SHORT, 101.0, 100.0, **dict(common, prev_extreme=95.0)) == st.SCARTATO_EMA
    # EMA uguali -> nessun range valido
    assert st.validate_range(st.LONG, 100.0, 100.0, **dict(common, prev_extreme=105.0)) == st.SCARTATO_EMA
    assert st.validate_range(st.SHORT, 100.0, 100.0, **dict(common, prev_extreme=105.0)) == st.SCARTATO_EMA
    # concordi -> valido
    assert st.validate_range(st.LONG, 101.0, 100.0, **dict(common, prev_extreme=105.0)) == st.VALIDO
    assert st.validate_range(st.SHORT, 99.0, 100.0, **dict(common, prev_extreme=101.0)) == st.VALIDO


def _sep_outcome(side: str, gaps: List[float], atr: float = 2.0) -> Tuple[str, int, float]:
    crosses, max_sep, _, separated = st.swing_ema_stats(side, gaps, [atr] * len(gaps))
    ef, es = (101.0, 100.0) if side == st.LONG else (99.0, 100.0)
    out = st.validate_range(side, ef, es, True, crosses, separated, 110.0, 100.0,
                            105.0 if side == st.LONG else 105.0, 1.0, False)
    return out, crosses, max_sep


def test_ema_separation_cases():
    # ATR = 2 -> soglia di separazione = 0.5 * 2 = 1.0
    assert _sep_outcome(st.LONG, [0.1, 0.2, 0.4, 0.8, 1.2])[0] == st.VALIDO            # strette che si allargano
    assert _sep_outcome(st.LONG, [0.1, 0.3, 0.5, 0.9, 0.6])[0] == st.SCARTATO_EMA_VICINE  # strette per tutto lo swing
    assert _sep_outcome(st.LONG, [1.5, 1.2, 0.8, 0.4, 0.2])[0] == st.VALIDO            # larghe che si stringono
    o, cr, ms = _sep_outcome(st.LONG, [-1.5, -1.2, -0.9, -0.5, -0.2])                  # larghe solo al ribasso
    assert o == st.SCARTATO_EMA_VICINE and cr == 0 and ms < 0
    o, cr, _ = _sep_outcome(st.LONG, [-0.4, -0.2, 0.0, 0.3, 1.1])                      # un incrocio, poi largo
    assert o == st.VALIDO and cr == 1
    o, cr, _ = _sep_outcome(st.LONG, [-0.4, 0.2, -0.2, 0.5, 1.4])                      # tre incroci
    assert o == st.SCARTATO_EMA_INCROCI and cr == 3
    o, cr, _ = _sep_outcome(st.LONG, [0.4, -0.2, 1.5, 1.4])                            # due incroci
    assert o == st.SCARTATO_EMA_INCROCI and cr == 2
    o, cr, ms = _sep_outcome(st.LONG, [0.2, 0.5, 1.0, 0.7])                            # esattamente sulla soglia
    assert o == st.VALIDO and ms == 0.5
    o, cr, _ = _sep_outcome(st.LONG, [0.5, 0.0, 0.0, 0.6, 1.2])                        # gap = 0 mantiene il segno
    assert cr == 0 and o == st.VALIDO
    o, cr, _ = _sep_outcome(st.SHORT, [0.3, -0.2, -0.6, -1.3])                         # ribassista speculare
    assert o == st.VALIDO and cr == 1
    assert _sep_outcome(st.SHORT, [1.5, 1.2, 0.9, 0.2])[0] == st.SCARTATO_EMA_VICINE
    # swing più lungo del buffer
    assert st.validate_range(st.LONG, 101.0, 100.0, False, 0, True, 110.0, 100.0, 105.0, 1.0, False) == \
        st.SCARTATO_SWING_LUNGO


def test_bos_amplitude_touched_rules():
    base = dict(swing_in_buffer=True, crosses=0, separated=True)
    # senza BOS (massimo non oltre lo swing high precedente, o swing precedente assente)
    assert st.validate_range(st.LONG, 101, 100, range_high=110, range_low=100, prev_extreme=110, atr_conf=1,
                             touched=False, **base) == st.SCARTATO_BOS
    assert st.validate_range(st.LONG, 101, 100, range_high=110, range_low=100, prev_extreme=None, atr_conf=1,
                             touched=False, **base) == st.SCARTATO_BOS
    assert st.validate_range(st.SHORT, 99, 100, range_high=110, range_low=100, prev_extreme=100, atr_conf=1,
                             touched=False, **base) == st.SCARTATO_BOS
    # ampiezza sotto MIN_RANGE_ATR x ATR (10 < 3 x 3.5) e sulla soglia (10 >= 3 x 10/3)
    assert st.validate_range(st.LONG, 101, 100, range_high=110, range_low=100, prev_extreme=105, atr_conf=3.5,
                             touched=False, **base) == st.SCARTATO_AMPIEZZA
    assert st.validate_range(st.LONG, 101, 100, range_high=110, range_low=100, prev_extreme=105, atr_conf=2.5,
                             touched=False, **base) == st.VALIDO
    # livello già toccato
    assert st.validate_range(st.LONG, 101, 100, range_high=110, range_low=100, prev_extreme=105, atr_conf=1,
                             touched=True, **base) == st.SCARTATO_GIA_TOCCATO


def warm_trend_candles(n: int = 560) -> List[st.Candle]:
    return candles_from_closes(sine_trend_closes(n))


def first_place(candles: List[st.Candle]) -> Tuple[st.OteFibStrategy, int, st.Action]:
    s = st.OteFibStrategy()
    for i, c in enumerate(candles):
        acts = s.on_candle_close(c, None, None)
        for a in acts:
            if a.type == st.PLACE_LIMIT:
                return s, i, a
    raise AssertionError("nessun PLACE_LIMIT nello scenario")


def test_end_to_end_place_and_gia_toccato():
    candles = warm_trend_candles(700)
    s, i, a = first_place(candles)
    assert i >= st.WARMUP_CANDLES and a.side == st.LONG
    assert a.limit_price == st.fib_level(st.LONG, a.range_high, a.range_low, st.ENTRY_LEVEL)
    assert a.sl == a.range_low and a.tp == a.range_high
    # stesso scenario, ma subito dopo il massimo il prezzo scende sotto il livello d'ingresso prima della conferma
    hi_k = int(np.searchsorted([pd.Timestamp(c.time).value for c in candles], pd.Timestamp(a.range_high_time).value))
    closes = [c.close for c in candles]
    closes2 = list(closes)
    closes2[hi_k + 2] = a.limit_price - 1.0
    closes2[hi_k + 3] = closes[hi_k + 3]
    cs2 = candles_from_closes(closes2)
    s2 = st.OteFibStrategy()
    outcome = None
    for k, c in enumerate(cs2[:i + 1]):
        s2.on_candle_close(c, None, None)
        for leg in s2.legs_this_candle:
            if leg["range_high_index"] == hi_k:
                outcome = leg["outcome"]
    assert outcome == st.SCARTATO_GIA_TOCCATO, outcome


def drive(candles: List[st.Candle], start_strategy: Optional[st.OteFibStrategy] = None):
    """Esecuzione minima senza fill: tiene l'ordine pendente finché non arriva un CANCEL."""
    s = start_strategy or st.OteFibStrategy()
    pend = None
    log = []
    for i, c in enumerate(candles):
        acts = s.on_candle_close(c, None, pend)
        for a in acts:
            if a.type == st.CANCEL:
                assert pend is not None and a.id == pend.id
                pend = None
            elif a.type == st.PLACE_LIMIT:
                pend = st.PendingOrder(a.id, a.side, a.limit_price, a.sl, a.tp, c.time, 0, a.range_high, a.range_low)
        log.append((i, acts))
    return s, log


def test_cancel_nuovo_estremo():
    candles = warm_trend_candles(700)
    _, log = drive(candles)
    placed = {}
    checked = 0
    for i, acts in log:
        for a in acts:
            if a.type == st.PLACE_LIMIT:
                placed[a.id] = (i, a)
            if a.type == st.CANCEL and a.reason == st.NUOVO_ESTREMO:
                pi, pa = placed[a.id]
                assert candles[i].high > pa.range_high
                assert all(candles[k].high <= pa.range_high for k in range(pi + 1, i))
                checked += 1
    assert checked > 0


def test_cancel_expiry():
    candles = warm_trend_candles(700)
    s, i, a = first_place(candles)
    # dopo il piazzamento: prezzo piatto tra limite e massimo (nessun fill, nessun nuovo estremo, nessun pivot)
    flat = (a.limit_price + a.range_high) / 2
    t0 = pd.Timestamp(candles[i].time)
    pend = st.PendingOrder(a.id, a.side, a.limit_price, a.sl, a.tp, t0, 0, a.range_high, a.range_low)
    got = None
    for k in range(1, 80):
        c = st.Candle(t0 + pd.Timedelta(minutes=15 * k), flat, flat + 0.1, flat - 0.1, flat)
        acts = s.on_candle_close(c, None, pend)
        if acts:
            got = (k, acts)
            break
    assert got is not None and got[0] == st.ORDER_EXPIRY_CANDLES, got
    assert got[1] == [st.Action(type=st.CANCEL, id=a.id, reason=st.SCADUTO)]
    assert pend.candles_alive == st.ORDER_EXPIRY_CANDLES


def test_cancel_trend_inverted():
    # funzione pura
    c = st.Candle(0, 100, 101, 99, 100)
    assert st.pending_cancel_reason(st.LONG, 110, 90, c, 99.0, 100.0, 1) == st.TREND_EMA_INVERTITO
    assert st.pending_cancel_reason(st.LONG, 110, 90, c, 100.0, 100.0, 1) == st.TREND_EMA_INVERTITO
    assert st.pending_cancel_reason(st.SHORT, 110, 90, c, 101.0, 100.0, 1) == st.TREND_EMA_INVERTITO
    assert st.pending_cancel_reason(st.SHORT, 110, 90, c, 100.0, 100.0, 1) == st.TREND_EMA_INVERTITO
    assert st.pending_cancel_reason(st.LONG, 110, 90, c, 101.0, 100.0, 1) is None
    # nella strategia: forziamo lo stato delle EMA (white-box) dopo un piazzamento
    candles = warm_trend_candles(700)
    s, i, a = first_place(candles)
    pend = st.PendingOrder(a.id, a.side, a.limit_price, a.sl, a.tp, candles[i].time, 0, a.range_high, a.range_low)
    s.ema_fast.value = s.ema_slow.value - 50.0
    nxt = candles[i + 1]
    assert nxt.high <= a.range_high
    acts = s.on_candle_close(nxt, None, pend)
    assert acts == [st.Action(type=st.CANCEL, id=a.id, reason=st.TREND_EMA_INVERTITO)], acts
    # con una posizione aperta l'inversione delle EMA non produce nulla (nessuna chiusura)
    s.ema_fast.value = s.ema_slow.value - 50.0
    pos = st.Position(st.LONG, a.limit_price, a.sl, a.tp, 0.1)
    assert s.on_candle_close(candles[i + 2], pos, None) == []


def test_replace_and_position_open():
    candles = warm_trend_candles(700)
    # range valido con un ordine pendente (di un range precedente) -> CANCEL SOSTITUITO + PLACE_LIMIT
    s, i, a = first_place(candles)
    s_ref = st.OteFibStrategy()
    old = st.PendingOrder("VECCHIO", st.LONG, 900.0, 890.0, 1500.0, candles[0].time, 3, 1500.0, 890.0)
    for c in candles[:i]:
        s_ref.on_candle_close(c, None, None)
    acts = s_ref.on_candle_close(candles[i], None, old)
    assert [x.type for x in acts] == [st.CANCEL, st.PLACE_LIMIT]
    assert acts[0].id == "VECCHIO" and acts[0].reason == st.SOSTITUITO and acts[1].limit_price == a.limit_price
    # con una posizione aperta un nuovo range valido NON genera Action
    s_pos = st.OteFibStrategy()
    pos = st.Position(st.LONG, 1000.0, 990.0, 1100.0, 0.1)
    for c in candles[:i]:
        s_pos.on_candle_close(c, None, None)
    assert s_pos.on_candle_close(candles[i], pos, None) == []
    assert s_pos.last_leg["outcome"] == st.IGNORATO_POSIZIONE_APERTA
    # e per tutto il resto dello scenario con posizione aperta: sempre lista vuota
    for c in candles[i + 1:]:
        assert s_pos.on_candle_close(c, pos, None) == []


def test_never_close_actions():
    assert set(st.__dict__.get(n) for n in ["PLACE_LIMIT", "CANCEL"]) == {"PLACE_LIMIT", "CANCEL"}
    assert not any(n.upper().startswith("CLOSE") for n in dir(st))
    md = synthetic_md()
    res = bt.run_replay(md, record_actions=True, verbose=False)
    types = {a.type for _, acts in res.actions_log for a in acts}
    assert types <= {st.PLACE_LIMIT, st.CANCEL}, types
    _, log = drive(warm_trend_candles(700))
    assert {a.type for _, acts in log for a in acts} <= {st.PLACE_LIMIT, st.CANCEL}


# ---------------------------------------------------------------------------
# Test: esecuzione (backtest)
# ---------------------------------------------------------------------------
def _scenario_md(modify: Callable[[pd.DataFrame, int, st.Action], None]) -> Tuple[bt.ReplayResult, int, st.Action, bt.MarketData]:
    """Scenario a trend regolare: modifica le candele 1m della candela successiva al primo piazzamento."""
    candles = warm_trend_candles(700)
    md0 = md_from_candles(candles)
    res0 = bt.run_replay(md0, record_actions=True, verbose=False)
    k, acts = next((k, a) for k, a in res0.actions_log if any(x.type == st.PLACE_LIMIT for x in a))
    a = next(x for x in acts if x.type == st.PLACE_LIMIT)
    df = pd.DataFrame({"time_ns": md0.t1, "open": md0.o1, "high": md0.h1, "low": md0.l1, "close": md0.c1, "volume": 0.0})
    modify(df, k, a)
    md = bt.build_market_data(df)
    return bt.run_replay(md, record_actions=True, verbose=False), k, a, md


def _set_next_candle(df: pd.DataFrame, k: int, a: st.Action, md0_start: int, price: float) -> None:
    rows = slice(md0_start, md0_start + 15)
    df.loc[df.index[rows], ["open", "high", "low", "close"]] = price


def test_fill_strict_and_same_bar_sl():
    candles = warm_trend_candles(700)
    md0 = md_from_candles(candles)

    def make(low_offset: Optional[float], sl_hit: bool = False):
        def modify(df, k, a):
            start = int(md0.start[k + 1])
            above = a.limit_price + 1.0
            df.loc[start:start + 14, ["open", "high", "low", "close"]] = above
            if low_offset is not None:
                df.loc[start + 5, "low"] = a.limit_price + low_offset if not sl_hit else a.sl - 0.5
                if sl_hit:
                    df.loc[start + 5, "high"] = a.tp + 5.0   # anche il TP "toccato": ignorato sulla candela del fill
        return modify

    # low == limite: nessun fill
    res, k, a, md = _scenario_md(make(0.0))
    t_bar = int(md0.t1[int(md0.start[k + 1]) + 5])
    assert not any(pd.Timestamp(x).value == t_bar for x in res.trades.get("fill_time", []))
    assert res.orders.set_index("order_id").loc[a.id, "outcome"] != bt.ORDER_FILLED or \
        res.trades.set_index("order_id").loc[a.id, "fill_time"].value != t_bar
    # low < limite: fill al limite su quella candela base
    res, k, a, md = _scenario_md(make(-0.01))
    tr = res.trades.set_index("order_id").loc[a.id]
    assert tr["fill_time"].value == t_bar and tr["fill_price"] == a.limit_price and not tr["fill_at_open"]
    # fill e SL nella stessa candela base -> chiuso in SL (il TP sulla candela del fill non conta)
    res, k, a, md = _scenario_md(make(0.0, sl_hit=True))
    tr = res.trades.set_index("order_id").loc[a.id]
    assert tr["fill_time"].value == t_bar and tr["exit_time"].value == t_bar
    assert tr["exit_reason"] == bt.EXIT_SL and tr["exit_price"] == a.sl and tr["pnl"] < 0
    assert tr["r"] < -1.0     # perdita di 1R più i costi


def test_gap_fill_and_exit_at_open():
    candles = warm_trend_candles(700)
    md0 = md_from_candles(candles)

    def modify(df, k, a):
        start = int(md0.start[k + 1])
        gap_open = a.limit_price - 0.5 * (a.limit_price - a.sl)    # apre già sotto il limite (ma sopra lo SL)
        df.loc[start:start + 14, ["open", "high", "low", "close"]] = gap_open
        nxt = int(md0.start[k + 2])
        df.loc[nxt:nxt + 14, ["open", "high", "low", "close"]] = a.sl - 3.0   # apre sotto lo SL -> uscita all'open
    res, k, a, md = _scenario_md(modify)
    tr = res.trades.set_index("order_id").loc[a.id]
    assert tr["fill_at_open"] and tr["fill_price"] == a.limit_price - 0.5 * (a.limit_price - a.sl)
    assert tr["exit_reason"] == bt.EXIT_SL and tr["exit_at_open"] and tr["exit_price"] == a.sl - 3.0

    def modify_skip(df, k, a):
        start = int(md0.start[k + 1])
        df.loc[start:start + 14, ["open", "high", "low", "close"]] = a.sl - 1.0   # gap oltre lo SL
    res, k, a, md = _scenario_md(modify_skip)
    assert res.orders.set_index("order_id").loc[a.id, "outcome"] == st.SKIP_SL_DISTANCE
    assert a.id not in set(res.trades.get("order_id", []))


def test_costs_and_pnl():
    candles = warm_trend_candles(700)
    md0 = md_from_candles(candles)

    def modify(df, k, a):
        start = int(md0.start[k + 1])
        df.loc[start:start + 14, ["open", "high", "low", "close"]] = a.limit_price + 1.0
        df.loc[start + 5, "low"] = a.limit_price - 0.01
        nxt = int(md0.start[k + 2])
        df.loc[nxt:nxt + 14, ["open", "high", "low", "close"]] = a.limit_price + 1.0
        df.loc[nxt + 3, "high"] = a.tp + 0.01
    res, k, a, md = _scenario_md(modify)
    tr = res.trades.set_index("order_id").loc[a.id]
    assert tr["exit_reason"] == bt.EXIT_TP and tr["exit_price"] == a.tp
    q = tr["qty_lots"]
    cps = bt.SPREAD_PER_OZ / 2 + bt.SLIPPAGE_PER_OZ
    exp = ((a.tp - cps) - (a.limit_price + cps)) * q * 100 - 2 * bt.COMMISSION_PER_LOT_SIDE * q
    assert abs(tr["pnl"] - exp) < 1e-6
    assert abs(tr["r_theoretical"] - st.ENTRY_LEVEL / (1 - st.ENTRY_LEVEL)) < 1e-6
    assert tr["risk_pct_effective"] <= 1.0 + 1e-9


# ---------------------------------------------------------------------------
# Test: dati HistData, buchi, fuso
# ---------------------------------------------------------------------------
def test_histdata_loading_and_gaps():
    df = synthetic_minutes("2023-01-01", "2023-02-05", seed=11)
    # buco anomalo di 90 minuti un mercoledì alle 15:00 UTC
    t = pd.to_datetime(df["time_ns"], utc=True)
    hole = (t >= pd.Timestamp("2023-01-11 15:00", tz="UTC")) & (t < pd.Timestamp("2023-01-11 16:30", tz="UTC"))
    df = df[~hole].reset_index(drop=True)
    tmp = tempfile.mkdtemp()
    try:
        half = len(df) // 2
        gen_a = to_histdata_generic(df.iloc[:half + 100])         # sovrapposizione di 100 righe tra file
        gen_b = "\n" + to_histdata_generic(df.iloc[half:])
        with open(os.path.join(tmp, "DAT_ASCII_XAUUSD_M1_202301.csv"), "w") as f:
            f.write(gen_a)
        with zipfile.ZipFile(os.path.join(tmp, "HISTDATA_COM_ASCII_XAUUSD_M1202302.zip"), "w") as z:
            z.writestr("DAT_ASCII_XAUUSD_M1_202302.csv", gen_b)
            z.writestr("DAT_ASCII_XAUUSD_M1_202302.txt", "report HistData")
        bt.setup_logging(os.path.join(tmp, "out"))
        md, rep, files = bt.load_market_data(tmp, "XAUUSD_M1_2023_2024", -5)
        assert len(files) == 2 and rep.exact_duplicates == 100 and len(rep.file_overlaps) == 1
        assert rep.rows_clean == len(df)
        assert np.array_equal(md.t1, df["time_ns"].to_numpy())
        g = rep.gaps
        an = g[g["categoria"] == bt.GAP_ANOMALOUS]
        assert len(an) == 1 and int(an["minuti_mancanti"].iloc[0]) == 90, an
        assert (g["categoria"] == bt.GAP_WEEKEND).sum() >= 4 and (g["categoria"] == bt.GAP_DAILY).sum() >= 15
        assert rep.week_fail_ratio == 0.0 and not rep.tz_anomaly
        # formato MetaTrader in un file unico con intestazione e righe vuote
        with open(os.path.join(tmp, "XAUUSD_M1_2023_2024.csv"), "w") as f:
            f.write("Date,Time,Open,High,Low,Close,Volume\n\n" + to_histdata_mt(df))
        md2, rep2, files2 = bt.load_market_data(tmp, "XAUUSD_M1_2023_2024", -5)
        assert len(files2) == 1 and rep2.spurious_rows >= 1 and np.array_equal(md2.t1, df["time_ns"].to_numpy())
        # fuso sbagliato di un'ora -> anomalia segnalata, conversione non cambiata
        md3, rep3, _ = bt.load_market_data(tmp, "XAUUSD_M1_2023_2024", -4)
        assert rep3.tz_anomaly and md3.t1[0] == df["time_ns"].iloc[0] - 3600 * 10 ** 9
        # dati oltre il 2025 -> errore
        late = df.copy()
        late["time_ns"] = late["time_ns"] + int(pd.Timedelta(days=3 * 365 + 10).value)
        with open(os.path.join(tmp, "XAUUSD_M1_2023_2024.csv"), "w") as f:
            f.write(to_histdata_generic(late))
        try:
            bt.load_market_data(tmp, "XAUUSD_M1_2023_2024", -5)
            raise AssertionError("doveva fallire per date oltre il 2025")
        except ValueError as e:
            assert "2025" in str(e)
        # timeframe non 1 minuto -> errore
        coarse = df.iloc[::5]
        with open(os.path.join(tmp, "XAUUSD_M1_2023_2024.csv"), "w") as f:
            f.write(to_histdata_generic(coarse))
        try:
            bt.load_market_data(tmp, "XAUUSD_M1_2023_2024", -5)
            raise AssertionError("doveva fallire per timeframe diverso da 1 minuto")
        except ValueError as e:
            assert "1 minuto" in str(e)
    finally:
        bt.log.handlers.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_aggregation_15m():
    df = synthetic_minutes("2023-01-01", "2023-01-10", seed=5)
    df = df.drop(index=[100, 101]).reset_index(drop=True)   # due candele base mancanti
    md = bt.build_market_data(df)
    t = pd.to_datetime(df["time_ns"], utc=True)
    b = t.dt.floor("15min")
    g = df.groupby(b.to_numpy())
    assert np.array_equal(md.t15, np.array([x.value for x in pd.to_datetime(list(g.groups.keys()), utc=True)]))
    assert np.allclose(md.o15, g["open"].first().to_numpy()) and np.allclose(md.c15, g["close"].last().to_numpy())
    assert np.allclose(md.h15, g["high"].max().to_numpy()) and np.allclose(md.l15, g["low"].min().to_numpy())
    assert (md.t15 % bt.TF_NS == 0).all() and (md.n_base < 15).sum() >= 1


# ---------------------------------------------------------------------------
def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[OK]   {name}")
        except Exception:
            failed += 1
            print(f"[FAIL] {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} test superati")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
