"""
Test della strategia (eseguibili con `python test_strategy.py`, senza pytest).

Copertura:
- nessun look-ahead (strategia alimentata fino alla candela N == run completa alla candela N);
- indicatori incrementali contro un'implementazione di riferimento indipendente;
- determinismo del backtest;
- casi limite su dati sintetici (ombre, EMA uguali/discordi, posizione aperta, niente chiusure);
- dettagli di esecuzione del backtest (SL e TP nella stessa 1m, uscita all'open dopo un buco).
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import strategy as st
import backtest as bt


# ---------------------------------------------------------------------------
# Dati sintetici
# ---------------------------------------------------------------------------
def synthetic_minutes(days: int = 60, seed: int = 7, start: str = "2023-01-01") -> pd.DataFrame:
    """Random walk 1m con regimi di drift alternati, nel formato prodotto da check_and_clean."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    regime_len = 3 * 1440
    drift = np.repeat(rng.choice([-6e-5, 6e-5, 0.0], size=n // regime_len + 1), regime_len)[:n]
    rets = drift + rng.normal(0, 8e-4, size=n)
    close = 20000.0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate(([20000.0], close[:-1]))
    wick_up = np.abs(rng.normal(0, 4e-4, size=n))
    wick_dn = np.abs(rng.normal(0, 4e-4, size=n))
    high = np.maximum(open_, close) * (1 + wick_up)
    low = np.minimum(open_, close) * (1 - wick_dn)
    t0 = pd.Timestamp(start, tz="UTC").value
    time_ns = t0 + np.arange(n, dtype="int64") * bt.MINUTE_NS
    return pd.DataFrame({"time_ns": time_ns, "open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(1, 50, size=n)})


def synthetic_candles(n: int = 3000, seed: int = 3):
    """Candele sintetiche del timeframe della strategia (oggetti Candle)."""
    md = bt.resample_candles(synthetic_minutes(days=n * bt.TF // 1440 + 5, seed=seed))
    return md.candles[:n]


def make_candle(i: int, o: float, h: float, l: float, c: float) -> st.Candle:
    t = datetime(2023, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=bt.TF * i)
    return st.Candle(time=t, open=o, high=h, low=l, close=c, volume=1.0)


# ---------------------------------------------------------------------------
# Implementazione di riferimento indipendente (loop numpy, formule alla lettera)
# ---------------------------------------------------------------------------
def reference_indicators(high, low, close, atr_period=10, mult=3.0, fast=50, slow=100):
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    atr[atr_period - 1] = tr[:atr_period].mean()
    for i in range(atr_period, n):
        atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period

    hl2 = (high + low) / 2
    bub = hl2 + mult * atr
    blb = hl2 - mult * atr
    ub = np.full(n, np.nan)
    lb = np.full(n, np.nan)
    stv = np.full(n, np.nan)
    up, down = 1, -1
    direction = np.full(n, down)
    first = atr_period - 1
    for i in range(first, n):
        if i == first:
            ub[i], lb[i], direction[i] = bub[i], blb[i], down
        else:
            ub[i] = bub[i] if (bub[i] < ub[i - 1] or close[i - 1] > ub[i - 1]) else ub[i - 1]
            lb[i] = blb[i] if (blb[i] > lb[i - 1] or close[i - 1] < lb[i - 1]) else lb[i - 1]
            if stv[i - 1] == ub[i - 1]:
                direction[i] = up if close[i] > ub[i] else down
            else:
                direction[i] = down if close[i] < lb[i] else up
        stv[i] = lb[i] if direction[i] == up else ub[i]

    def ema(period):
        out = np.full(n, np.nan)
        a = 2 / (period + 1)
        out[period - 1] = close[:period].mean()
        for i in range(period, n):
            out[i] = a * close[i] + (1 - a) * out[i - 1]
        return out

    return {"atr": atr, "upper": ub, "lower": lb, "st": stv, "dir": direction,
            "ema_fast": ema(fast), "ema_slow": ema(slow)}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
_MD_CACHE = {}


def market(days=400, seed=7):
    key = (days, seed)
    if key not in _MD_CACHE:
        _MD_CACHE[key] = bt.resample_candles(synthetic_minutes(days=days, seed=seed))
    return _MD_CACHE[key]


def test_indicators_match_reference():
    candles = synthetic_candles(3000)
    h = np.array([c.high for c in candles])
    l = np.array([c.low for c in candles])
    c = np.array([c.close for c in candles])
    ref = reference_indicators(h, l, c, st.ST_ATR_PERIOD, st.ST_MULTIPLIER, st.EMA_FAST, st.EMA_SLOW)
    strat = st.SupertrendEmaStrategy()
    vals = {k: [] for k in ("atr", "st", "upper", "lower", "dir", "ema_fast", "ema_slow")}
    for cd in candles:
        strat.on_candle_close(cd, None)
        s = strat.supertrend
        vals["atr"].append(s.atr.value if s.atr.value is not None else np.nan)
        vals["st"].append(s.value if s.value is not None else np.nan)
        vals["upper"].append(s.upper_band if s.upper_band is not None else np.nan)
        vals["lower"].append(s.lower_band if s.lower_band is not None else np.nan)
        vals["dir"].append(s.direction)
        vals["ema_fast"].append(strat.ema_fast.value if strat.ema_fast.value is not None else np.nan)
        vals["ema_slow"].append(strat.ema_slow.value if strat.ema_slow.value is not None else np.nan)
    w = st.WARMUP_CANDLES
    for k in ("atr", "st", "upper", "lower", "ema_fast", "ema_slow"):
        diff = np.max(np.abs(np.array(vals[k])[w:] - ref[k][w:]))
        assert diff < 1e-6, f"{k}: differenza massima {diff}"
    # anche prima del warm-up, dove definiti
    for k in ("atr", "ema_fast", "ema_slow", "st"):
        a, b = np.array(vals[k]), ref[k]
        mask = ~np.isnan(b)
        assert np.all(~np.isnan(a[mask])) and np.max(np.abs(a[mask] - b[mask])) < 1e-6, k
    same = np.mean(np.array(vals["dir"]) == ref["dir"])
    assert same == 1.0, f"direzione coincidente solo al {same:.4%}"
    n_changes = int(np.sum(np.diff(ref["dir"]) != 0))
    assert n_changes > 20, "dati sintetici con troppi pochi cambi di trend"
    return f"3000 candele, {n_changes} cambi di trend, direzione coincidente al 100%"


def test_atr_ema_seed_availability():
    atr = st.IncrementalATR(10)
    for i in range(9):
        assert atr.update(10 + i, 9 + i, 9.5 + i) is None
    assert atr.update(20, 18, 19) is not None, "ATR deve essere disponibile dalla 10a candela"
    ema = st.IncrementalEMA(50)
    for i in range(49):
        assert ema.update(float(i)) is None
    assert abs(ema.update(49.0) - np.mean(np.arange(50))) < 1e-12, "seed EMA = SMA dei primi N"
    return "seed SMA e disponibilità corretti"


def test_no_lookahead():
    md = market()
    res = bt.run_replay(md, record_inputs=True)
    n = len(md.candles)
    entries = [i for i in range(n) if res.actions_out[i]]
    assert len(entries) >= 10, f"troppi pochi ingressi nei dati sintetici: {len(entries)}"
    rng = np.random.default_rng(0)
    others = rng.choice(np.arange(st.WARMUP_CANDLES, n), size=60, replace=False).tolist()
    samples = sorted(set(entries) | set(others))
    assert len(samples) >= 50
    for N in samples:
        strat = st.SupertrendEmaStrategy()
        strat.warmup(md.candles[:st.WARMUP_CANDLES])
        acts = []
        for k in range(st.WARMUP_CANDLES, N + 1):
            acts = strat.on_candle_close(md.candles[k], res.positions_fed[k])
        assert acts == res.actions_out[N], f"azioni diverse alla candela {N}: {acts} vs {res.actions_out[N]}"

    # troncando i dati 1m a metà, i trade chiusi prima del taglio sono identici
    df = synthetic_minutes(days=400)
    cut = df["time_ns"].iloc[len(df) // 2]
    md_half = bt.resample_candles(df[df["time_ns"] < cut].reset_index(drop=True))
    res_half = bt.run_replay(md_half)
    full = [t for t in res.trades if t["exit_reason"] != "fine_dati" and t["exit_time"].value < cut]
    half = [t for t in res_half.trades if t["exit_reason"] != "fine_dati"]
    assert len(full) > 0 and full == half[:len(full)] and len(half) == len(full), "il futuro influenza il passato"
    return f"{len(samples)} punti campionati ({len(entries)} ingressi) + test di troncamento ({len(full)} trade)"


def test_determinism():
    md = market()
    a = bt.run_replay(md)
    b = bt.run_replay(md)
    assert a.trades == b.trades and a.signals == b.signals and np.array_equal(a.equity, b.equity)
    assert len(a.trades) > 0
    return f"{len(a.trades)} trade identici in due run"


def _downtrend_supertrend():
    s = st.IncrementalSupertrend(10, 3.0)
    price = 30000.0
    for i in range(40):
        price -= 50
        s.update(price + 20, price - 20, price)
    assert s.direction == st.IS_DOWN_TREND
    return s, price


def test_wick_does_not_flip():
    s, price = _downtrend_supertrend()
    ub = s.upper_band
    # ombra ben oltre la banda superiore, chiusura sotto: nessun cambio
    s.update(ub + 2000, price - 20, price)
    assert s.direction == st.IS_DOWN_TREND and not s.changed, "un'ombra senza chiusura ha cambiato il trend"
    ub2 = s.upper_band
    # chiusura oltre la banda: cambio
    s.update(ub2 + 600, ub2 - 100, ub2 + 500)
    assert s.direction == st.IS_UP_TREND and s.changed, "la chiusura oltre la banda deve cambiare il trend"
    # simmetrico in uptrend
    up = st.IncrementalSupertrend(10, 3.0)
    p = 20000.0
    for i in range(40):
        p += 50
        up.update(p + 20, p - 20, p)
    assert up.direction == st.IS_UP_TREND
    lb = up.lower_band
    up.update(p + 20, lb - 2000, p)
    assert up.direction == st.IS_UP_TREND and not up.changed
    return "ombre oltre la linea senza chiusura non cambiano il trend"


def test_wick_no_signal_in_strategy():
    # strategia completa: un'ombra non produce né segnale né azione
    strat = st.SupertrendEmaStrategy()
    price = 40000.0
    i = 0
    for i in range(600):
        price -= 5
        strat.on_candle_close(make_candle(i, price + 5, price + 10, price - 10, price), None)
    assert strat.supertrend.direction == st.IS_DOWN_TREND
    ub = strat.supertrend.upper_band
    acts = strat.on_candle_close(make_candle(i + 1, price, ub + 3000, price - 10, price - 1), None)
    assert acts == [] and not strat.signal_on_last_candle
    return "nessun segnale su ombra"


def test_ema_filter_rules():
    pos = st.Position(side=st.LONG, entry=100.0, sl=90.0, tp=120.0, qty=1.0)
    up, dn = st.IS_UP_TREND, st.IS_DOWN_TREND
    assert st.evaluate_trend_change(up, 95.0, 100.0, 100.0, None) == ([], st.OUTCOME_REJECTED_EMA)
    assert st.evaluate_trend_change(dn, 105.0, 100.0, 100.0, None) == ([], st.OUTCOME_REJECTED_EMA)
    assert st.evaluate_trend_change(up, 95.0, 99.0, 100.0, None) == ([], st.OUTCOME_REJECTED_EMA)
    assert st.evaluate_trend_change(dn, 105.0, 101.0, 100.0, None) == ([], st.OUTCOME_REJECTED_EMA)
    acts, out = st.evaluate_trend_change(up, 95.0, 101.0, 100.0, None)
    assert out == st.OUTCOME_ENTRY and acts == [st.Action(side=st.LONG, sl=95.0)]
    acts, out = st.evaluate_trend_change(dn, 105.0, 99.0, 100.0, None)
    assert out == st.OUTCOME_ENTRY and acts == [st.Action(side=st.SHORT, sl=105.0)]
    # posizione aperta + cambio opposto con EMA concordi: nessuna azione
    assert st.evaluate_trend_change(dn, 105.0, 99.0, 100.0, pos) == ([], st.OUTCOME_IGNORED_POSITION)
    assert st.evaluate_trend_change(up, 95.0, 101.0, 100.0, pos) == ([], st.OUTCOME_IGNORED_POSITION)
    assert st.evaluate_trend_change(up, 95.0, None, 100.0, None) == ([], st.OUTCOME_REJECTED_EMA)
    return "EMA uguali / discordi / posizione aperta"


def test_rules_in_full_replay():
    md = market()
    res = bt.run_replay(md, record_inputs=True)
    sig_by_time = {s["time"]: s for s in res.signals}
    ignored_concordant = 0
    for i in range(st.WARMUP_CANDLES, len(md.candles)):
        acts = res.actions_out[i]
        pos = res.positions_fed[i]
        t = pd.Timestamp(md.candles[i].time)
        for a in acts:
            assert a.type == st.ACTION_OPEN, "la strategia ha emesso un'azione diversa da OPEN"
        if pos is not None:
            assert acts == [], "azione emessa con posizione aperta"
        if acts:
            s = sig_by_time.get(t)
            assert s is not None and s["outcome"] == st.OUTCOME_ENTRY, "ingresso fuori da un cambio di trend"
            a = acts[0]
            assert (a.side == st.LONG and s["ema_fast"] > s["ema_slow"]) or \
                   (a.side == st.SHORT and s["ema_fast"] < s["ema_slow"])
            assert a.sl == s["st_line"]
        s = sig_by_time.get(t)
        if s is not None and s["outcome"] == st.OUTCOME_IGNORED_POSITION:
            assert pos is not None
            if (s["direction"] == st.LONG) == (s["ema_fast"] > s["ema_slow"]) and pos.side != s["direction"]:
                ignored_concordant += 1
    for tr in res.trades:
        assert tr["exit_reason"] in ("SL", "TP", "fine_dati")
    try:
        st.Action(side=st.LONG, sl=1.0, type="CLOSE")
        raise AssertionError("Action CLOSE non deve esistere")
    except ValueError:
        pass
    # scenario costruito: a ogni candela in cui la strategia flat entrerebbe, si passa una posizione
    # aperta di verso opposto (cambio opposto con EMA concordi): nessuna Action deve uscire
    candles = md.candles
    flat = st.SupertrendEmaStrategy()
    entry_idx = [i for i, c in enumerate(candles) if flat.on_candle_close(c, None)]
    assert len(entry_idx) >= 5
    for N in entry_idx[:10]:
        s2 = st.SupertrendEmaStrategy()
        for k in range(N):
            s2.on_candle_close(candles[k], None)
        flat_side = st.SupertrendEmaStrategy()
        for k in range(N):
            flat_side.on_candle_close(candles[k], None)
        side = flat_side.on_candle_close(candles[N], None)[0].side
        opposite = st.SHORT if side == st.LONG else st.LONG
        pos = st.Position(side=opposite, entry=candles[N].close, sl=0.0, tp=0.0, qty=1.0)
        acts = s2.on_candle_close(candles[N], pos)
        assert acts == [] and s2.last_signal.outcome == st.OUTCOME_IGNORED_POSITION
        ignored_concordant += 1
    return f"{len(res.signals)} cambi verificati, {ignored_concordant} cambi opposti concordi ignorati in posizione"


def test_sizing_and_tp():
    assert st.size_position(10000, 100.0, 100.0, st.LONG) == 0.0
    assert st.size_position(10000, 99.0, 100.0, st.LONG) == 0.0
    assert st.size_position(10000, 101.0, 100.0, st.SHORT) == 0.0
    q = st.size_position(10000, 100.0, 98.0, st.LONG)
    assert abs(q * 2.0 - 100.0) < 1e-9
    assert abs(st.take_profit(st.LONG, 100.0, 98.0) - (100.0 + 2.0 * st.RR)) < 1e-12
    assert abs(st.take_profit(st.SHORT, 100.0, 102.0) - (100.0 - 2.0 * st.RR)) < 1e-12
    return "sizing 1% e TP a RR corretti, distanza SL <= 0 => 0"


def test_out_of_order_rejected():
    strat = st.SupertrendEmaStrategy()
    strat.on_candle_close(make_candle(1, 1, 2, 0.5, 1.5), None)
    try:
        strat.on_candle_close(make_candle(1, 1, 2, 0.5, 1.5), None)
        raise AssertionError("candela duplicata accettata")
    except ValueError:
        pass
    return "candele fuori ordine rifiutate"


def _first_long_trade(res, min_minutes=40):
    for t in res.trades:
        if t["side"] == st.LONG and t["duration_min"] >= min_minutes and t["exit_reason"] != "fine_dati":
            return t
    raise AssertionError("nessun trade long adatto nei dati sintetici")


def test_execution_sl_and_tp_same_minute():
    df = synthetic_minutes(days=400)
    res = bt.run_replay(bt.resample_candles(df))
    t = _first_long_trade(res)
    k = int(np.nonzero(df["time_ns"].to_numpy() == t["entry_time"].value)[0][0]) + 1
    df2 = df.copy()
    df2.loc[k, "high"] = t["tp"] * 1.01
    df2.loc[k, "low"] = t["sl"] * 0.99
    res2 = bt.run_replay(bt.resample_candles(df2))
    t2 = [x for x in res2.trades if x["entry_time"] == t["entry_time"]][0]
    assert t2["exit_reason"] == "SL" and t2["exit_time"].value == df["time_ns"].iloc[k]
    assert abs(t2["exit_price"] - t["sl"] * (1 - bt.SLIPPAGE_RATE)) < 1e-6
    return "SL e TP nella stessa 1m => SL"


def test_execution_gap_exit_at_open():
    df = synthetic_minutes(days=400)
    res = bt.run_replay(bt.resample_candles(df))
    t = _first_long_trade(res)
    k = int(np.nonzero(df["time_ns"].to_numpy() == t["entry_time"].value)[0][0])
    drop = list(range(k + 1, k + 31))   # buco di 30 minuti subito dopo l'ingresso
    after = k + 31
    gap_price = t["sl"] * 0.98          # la prima 1m dopo il buco apre sotto lo SL
    df2 = df.copy()
    for col in ("open", "high", "low", "close"):
        df2.loc[after, col] = gap_price
    df2 = df2.drop(index=drop).reset_index(drop=True)
    res2 = bt.run_replay(bt.resample_candles(df2))
    t2 = [x for x in res2.trades if x["entry_time"] == t["entry_time"]][0]
    assert t2["exit_reason"] == "SL" and t2["exit_at_open_beyond_level"] and t2["gap_during_trade"]
    assert abs(t2["exit_price"] - gap_price * (1 - bt.SLIPPAGE_RATE)) < 1e-6, "uscita non all'open dopo il buco"
    return "dopo un buco con apertura oltre lo SL l'uscita è all'open"


def test_data_checks_and_resample():
    df = synthetic_minutes(days=3)
    raw = df.copy()
    raw["source_file"] = "x"
    # duplicato, buco, ordine invertito
    raw = pd.concat([raw, raw.iloc[[10]]], ignore_index=True)
    raw = raw.drop(index=range(100, 103)).reset_index(drop=True)
    raw = raw.iloc[::-1].reset_index(drop=True)
    clean, rep = bt.check_and_clean(raw, {"header_rows": 0, "unit_counts": {}})
    assert rep.exact_duplicates == 1 and not rep.was_sorted and rep.missing_minutes == 3 and len(rep.gaps) == 1
    md = bt.resample_candles(clean)
    assert all(pd.Timestamp(c.time).value % (bt.TF * bt.MINUTE_NS) == 0 for c in md.candles)
    assert int((md.c_minutes < bt.TF).sum()) == 1
    c0 = md.candles[0]
    first15 = df.iloc[:bt.TF].drop(index=range(100, 103))
    assert c0.open == first15["open"].iloc[0] and c0.close == first15["close"].iloc[-1]
    assert c0.high == first15["high"].max() and c0.low == first15["low"].min()
    # limite temporale
    late = synthetic_minutes(days=1, start="2026-01-01")
    late["source_file"] = "y"
    try:
        bt.check_and_clean(late, {"header_rows": 0, "unit_counts": {}})
        raise AssertionError("dati oltre il 2025 accettati")
    except SystemExit:
        pass
    return "duplicati, ordinamento, buchi, aggregazione al timeframe e limite 2025 verificati"


def test_histdata_format_and_gaps():
    import os
    import tempfile
    lines = [
        "20230102 180000;1826.837000;1827.337000;1826.617000;1826.637000;0",
        "20230102 180100;1826.537000;1827.357000;1826.137000;1826.537000;0",
        "20230102 180200;1826.137000;1826.737000;1826.137000;1826.737000;0",
        "20230102 180500;1827.187000;1828.867000;1827.187000;1828.738000;0",
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "XAUUSD_M1_2023_2024.csv")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        assert bt.detect_format(path) == bt.FORMAT_HISTDATA
        assert bt.find_data_files(d, "XAUUSD_M1_2023_2024") == [path]
        raw, info = bt.load_raw_klines([path])
        assert info["format"] == bt.FORMAT_HISTDATA and info["symbol"] == "XAUUSD"
        # 18:00 EST fisso (UTC-5) => 23:00 UTC
        assert bt.ts(raw["time_ns"].iloc[0]) == pd.Timestamp("2023-01-02 23:00", tz="UTC")
        assert raw["open"].iloc[0] == 1826.837 and raw["close"].iloc[3] == 1828.738
        clean, rep = bt.check_and_clean(raw, info)
        assert rep.missing_minutes == 2 and rep.gaps[0][3] == bt.GAP_OTHER
    # classificazione chiusure (orari UTC)
    fri = pd.Timestamp("2023-01-06 22:00", tz="UTC")     # venerdì 17:00 New York
    sun = pd.Timestamp("2023-01-08 22:59", tz="UTC")     # domenica 17:59 New York
    assert bt.classify_gap(fri, sun, int((sun - fri).total_seconds() // 60) + 1, bt.FORMAT_HISTDATA) == bt.GAP_WEEKEND
    daily_s = pd.Timestamp("2023-07-11 21:00", tz="UTC")  # 17:00 New York (ora legale)
    assert bt.classify_gap(daily_s, daily_s + pd.Timedelta(minutes=59), 60, bt.FORMAT_HISTDATA) == bt.GAP_DAILY
    mid = pd.Timestamp("2023-07-11 14:00", tz="UTC")
    assert bt.classify_gap(mid, mid + pd.Timedelta(minutes=89), 90, bt.FORMAT_HISTDATA) == bt.GAP_OTHER
    assert bt.classify_gap(fri, sun, 3000, bt.FORMAT_BINANCE) == bt.GAP_OTHER
    return "formato HistData (EST -> UTC) e classificazione weekend / pausa giornaliera"


TESTS = [
    test_atr_ema_seed_availability,
    test_indicators_match_reference,
    test_no_lookahead,
    test_determinism,
    test_wick_does_not_flip,
    test_wick_no_signal_in_strategy,
    test_ema_filter_rules,
    test_rules_in_full_replay,
    test_sizing_and_tp,
    test_out_of_order_rejected,
    test_execution_sl_and_tp_same_minute,
    test_execution_gap_exit_at_open,
    test_data_checks_and_resample,
    test_histdata_format_and_gaps,
]


def main() -> int:
    import logging
    logging.getLogger("backtest").addHandler(logging.NullHandler())
    failed = 0
    for test in TESTS:
        try:
            msg = test()
            print(f"[OK]   {test.__name__}: {msg}")
        except Exception:
            failed += 1
            print(f"[FAIL] {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} test superati")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
