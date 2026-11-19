"""
Backtest della strategia OTE Fibonacci su XAUUSD 15m definita in strategy.py.

Questo file contiene tutto ciò che NON è logica di trading: caricamento e controllo dei dati 1m
HistData, aggregazione in candele 15m, replay candela per candela (come un loop live), simulazione
dell'esecuzione degli ordini limite e di SL/TP sulle candele base 1m, costi, metriche, grafici,
report e salvataggio. Le regole di trading stanno SOLO in strategy.py e qui vengono usate senza
duplicarle.

Formati HistData supportati (rilevati leggendo le prime righe reali di ogni file):
- Generic ASCII: "YYYYMMDD HHMMSS;open;high;low;close;volume" (separatore ;)
- MetaTrader:    "YYYY.MM.DD,HH:MM,open,high,low,close,volume" (separatore ,)
Orari HistData in EST fisso (UTC-5, senza ora legale) -> UTC aggiungendo 5 ore (opzione --tz).

Uso:
    python backtest.py
    python backtest.py --data-dir "C:\\Users\\Admin\\Downloads\\dati" --name XAUUSD_M1_2023_2024 --tz -5 --out output
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import io
import logging
import math
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import strategy as st

# ---------------------------------------------------------------------------
# Configurazione del backtest (NON sono parametri della strategia)
# ---------------------------------------------------------------------------
DEFAULT_DATA_DIR = r"C:\Users\Admin\Downloads\dati"
DEFAULT_NAME = "XAUUSD_M1_2023_2024"
DEFAULT_TZ = -5.0
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "output")
SYMBOL = "XAUUSD"

INITIAL_CAPITAL = 10_000.0
SPREAD_PER_OZ = 0.30              # spread fisso USD/oncia (metà per lato)
SLIPPAGE_PER_OZ = 0.05            # USD/oncia per lato
COMMISSION_PER_LOT_SIDE = 3.5     # USD per lotto per lato (7 andata e ritorno)
MAX_ALLOWED_DATE = pd.Timestamp("2025-12-31 23:59:59", tz="UTC")
PRICE_MIN, PRICE_MAX = 500.0, 10_000.0
JUMP_ATR_MULT = 20.0
LARGE_GAP_MINUTES = 60
MIN_TRADES_SAMPLE = 200
WEEK_FAIL_THRESHOLD = 0.20
TRADING_DAYS_PER_YEAR = 252

MINUTE_NS = 60 * 1_000_000_000
TF_NS = st.TIMEFRAME_MINUTES * MINUTE_NS
MARKET_TZ = "America/New_York"    # usato SOLO per verificare il calendario di mercato, mai per convertire i dati

FMT_GENERIC = "HistData Generic ASCII"
FMT_MT = "HistData MetaTrader"
RE_GENERIC = re.compile(r"^\s*\d{8} \d{6};")
RE_MT = re.compile(r"^\s*\d{4}\.\d{2}\.\d{2},\d{2}:\d{2},")

GAP_WEEKEND = "atteso_weekend"
GAP_DAILY = "atteso_pausa_giornaliera"
GAP_HOLIDAY = "atteso_festivita"
GAP_ANOMALOUS = "anomalo"
EXPECTED_GAPS = (GAP_WEEKEND, GAP_DAILY, GAP_HOLIDAY)

# Sessioni (orario del fill, UTC): [inizio, fine)
SESSIONS = {"Asia": [(22, 24), (0, 7)], "Londra": [(7, 13)], "NY": [(13, 22)]}

ORDER_FILLED = "ESEGUITO"
ORDER_END_OF_DATA = "FINE_DATI"
EXIT_SL, EXIT_TP, EXIT_END = "SL", "TP", "fine_dati"

log = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(out_dir: str) -> str:
    """Log su console e su output/backtest_log.txt."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "backtest_log.txt")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(ch)
    return path


# ---------------------------------------------------------------------------
# Ricerca e lettura dei file
# ---------------------------------------------------------------------------
def _read_text(path: str) -> str:
    """Testo di un file .csv/.txt oppure del file dati dentro uno zip HistData."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            members = [m for m in z.namelist() if not m.endswith("/")]
            csvs = [m for m in members if m.lower().endswith(".csv")]
            pick = csvs or [m for m in members if not m.lower().endswith(".txt")] or members
            member = max(pick, key=lambda m: z.getinfo(m).file_size)
            log.info("  %s -> membro dello zip usato: %s (membri: %s)", os.path.basename(path), member, members)
            return z.read(member).decode("utf-8", errors="replace")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _first_lines(path: str, n: int = 10) -> List[str]:
    if path.lower().endswith(".zip"):
        text = _read_text(path)[:8192]
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(8192)
    return [x for x in text.splitlines() if x.strip()][:n]


def detect_format(lines: List[str]) -> Optional[str]:
    """Formato dedotto dalle righe reali (eventuali intestazioni iniziali sono ignorate)."""
    n_gen = sum(bool(RE_GENERIC.match(x)) for x in lines)
    n_mt = sum(bool(RE_MT.match(x)) for x in lines)
    if n_gen == 0 and n_mt == 0:
        return None
    return FMT_GENERIC if n_gen >= n_mt else FMT_MT


def find_data_files(data_dir: str, name: str) -> List[str]:
    """
    Cerca prima un file unico `name`.(csv|txt|zip) con contenuto valido; in mancanza i file annuali o
    mensili HistData di XAUUSD M1 (DAT_ASCII_/DAT_MT_/HISTDATA_COM_..._XAUUSD_M1...), in ordine cronologico.
    """
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"Cartella dati non trovata: {data_dir}")
    all_files = sorted(os.listdir(data_dir))
    log.info("File presenti in %s (%d): %s", data_dir, len(all_files), all_files)
    order = {".csv": 0, ".txt": 1, ".zip": 2}
    single = [os.path.join(data_dir, f) for f in all_files
              if re.fullmatch(re.escape(name) + r"\.(csv|txt|zip)", f, re.IGNORECASE)]
    single.sort(key=lambda p: order.get(os.path.splitext(p)[1].lower(), 9))
    for path in single:
        fmt_ = detect_format(_first_lines(path))
        if fmt_:
            log.info("File unico trovato e usato: %s (%s)", os.path.basename(path), fmt_)
            return [path]
        log.info("File %s ignorato: le prime righe non sono dati HistData", os.path.basename(path))
    pattern = re.compile(r"(?:DAT_(?:ASCII|MT)_|HISTDATA_COM_(?:ASCII|MT)_)XAUUSD_M1_?(\d{4})(\d{2})?\.(csv|zip)$",
                         re.IGNORECASE)
    found: Dict[Tuple[int, int, str], str] = {}
    for f in all_files:
        m = pattern.search(f)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2) or 0), "MT" if "_MT_" in f.upper() else "ASCII")
        path = os.path.join(data_dir, f)
        if key not in found or path.lower().endswith(".csv"):   # .csv preferito allo .zip
            found[key] = path
    if not found:
        raise FileNotFoundError(f"Né '{name}.*' né file HistData XAUUSD M1 in {data_dir}. File: {all_files}")
    files = [found[k] for k in sorted(found)]
    log.info("File unico '%s' non trovato: uso %d file HistData: %s", name, len(files),
             [os.path.basename(p) for p in files])
    return files


def read_histdata_file(path: str, tz_hours: float, info: Dict) -> pd.DataFrame:
    """Legge un file HistData (Generic ASCII o MetaTrader) e converte gli orari in UTC (utc = locale - tz)."""
    text = _read_text(path)
    lines = [x for x in text.splitlines() if x.strip()]
    fmt_ = detect_format(lines[:50])
    if fmt_ is None:
        raise ValueError(f"{path}: formato non riconosciuto. Prime righe: {lines[:3]}")
    log.info("File %s: formato %s; prime righe reali: %s", os.path.basename(path), fmt_, lines[:2])
    sep = ";" if fmt_ == FMT_GENERIC else ","
    raw = pd.read_csv(io.StringIO(text), sep=sep, header=None, dtype=str, skip_blank_lines=True,
                      on_bad_lines="skip", engine="c")
    if fmt_ == FMT_GENERIC:
        key = raw.iloc[:, 0].astype(str).str.strip()
        ok = key.str.fullmatch(r"\d{8} \d{6}")
        local = pd.to_datetime(key[ok], format="%Y%m%d %H%M%S")
        cols = [1, 2, 3, 4, 5]
    else:
        key = raw.iloc[:, 0].astype(str).str.strip() + " " + raw.iloc[:, 1].astype(str).str.strip()
        ok = key.str.fullmatch(r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}")
        local = pd.to_datetime(key[ok], format="%Y.%m.%d %H:%M")
        cols = [2, 3, 4, 5, 6]
    n_bad = int((~ok).sum())
    info["spurious_rows"] += n_bad
    if n_bad:
        log.info("  %s: scartate %d righe non di dati (intestazioni/righe spurie), es. %s",
                 os.path.basename(path), n_bad, key[~ok].unique()[:3].tolist())
    raw = raw[ok]
    utc = (local - pd.Timedelta(hours=tz_hours)).dt.tz_localize("UTC")
    df = pd.DataFrame({"time_ns": utc.astype("datetime64[ns, UTC]").astype("int64").to_numpy()})
    for name, c in zip(["open", "high", "low", "close", "volume"], cols):
        df[name] = pd.to_numeric(raw.iloc[:, c], errors="coerce").to_numpy() if c < raw.shape[1] else 0.0
    df["source_file"] = os.path.basename(path)
    info["files"].append({"file": os.path.basename(path), "format": fmt_, "rows": len(df),
                          "first": str(pd.Timestamp(df["time_ns"].min(), tz="UTC")) if len(df) else "",
                          "last": str(pd.Timestamp(df["time_ns"].max(), tz="UTC")) if len(df) else ""})
    info["formats"].add(fmt_)
    return df


# ---------------------------------------------------------------------------
# Calendario di mercato (solo per classificare i buchi e verificare il fuso)
# ---------------------------------------------------------------------------
def easter_date(year: int) -> dt.date:
    """Data della Pasqua (algoritmo gregoriano anonimo)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def holiday_dates(years: List[int]) -> Dict[dt.date, str]:
    """
    Festività note (date di New York): Natale (24-26/12), Capodanno (31/12-2/1), Venerdì Santo e,
    come scelta documentata, le festività USA in cui il COMEX ha orario ridotto (MLK, Presidents,
    Memorial, Juneteenth, 4 luglio, Labor, Thanksgiving + venerdì successivo), con i giorni "osservati".
    """
    out: Dict[dt.date, str] = {}
    for y in years:
        for d, n in [(dt.date(y, 12, 24), "Vigilia di Natale"), (dt.date(y, 12, 25), "Natale"),
                     (dt.date(y, 12, 26), "Santo Stefano"), (dt.date(y, 12, 31), "San Silvestro"),
                     (dt.date(y, 1, 1), "Capodanno"), (dt.date(y, 1, 2), "Capodanno (giorno dopo)"),
                     (easter_date(y) - dt.timedelta(days=2), "Venerdì Santo"),
                     (_nth_weekday(y, 1, 0, 3), "Martin Luther King Day (USA)"),
                     (_nth_weekday(y, 2, 0, 3), "Presidents Day (USA)"),
                     (_last_weekday(y, 5, 0), "Memorial Day (USA)"),
                     (dt.date(y, 6, 19), "Juneteenth (USA)"), (dt.date(y, 7, 4), "Independence Day (USA)"),
                     (_nth_weekday(y, 9, 0, 1), "Labor Day (USA)"),
                     (_nth_weekday(y, 11, 3, 4), "Thanksgiving (USA)"),
                     (_nth_weekday(y, 11, 3, 4) + dt.timedelta(days=1), "Venerdì dopo Thanksgiving (USA)")]:
            out.setdefault(d, n)
            if d.weekday() == 5:
                out.setdefault(d - dt.timedelta(days=1), n + " (osservato)")
            elif d.weekday() == 6:
                out.setdefault(d + dt.timedelta(days=1), n + " (osservato)")
    return out


def classify_gap(prev_ns: int, next_ns: int, missing: int, holidays: Dict[dt.date, str]) -> Tuple[str, str]:
    """
    Classifica un buco tra la candela base prev e la successiva next (orari UTC).
    Atteso: weekend (da venerdì a domenica/lunedì di New York), pausa giornaliera (circa 17-18 di New York,
    al massimo 120 minuti), festività note. Altrimenti: anomalo. Restituisce (categoria, nota).
    """
    a = pd.Timestamp(prev_ns, tz="UTC").tz_convert(MARKET_TZ)
    b = pd.Timestamp(next_ns, tz="UTC").tz_convert(MARKET_TZ)
    dates = [a.date() + dt.timedelta(days=k) for k in range((b.date() - a.date()).days + 1)]
    hol = [holidays[d] for d in dates if d in holidays]
    if a.weekday() == 4 and b.weekday() in (6, 0) and missing >= 24 * 60:
        return GAP_WEEKEND, ("; ".join(hol) if hol else "")
    if a.weekday() >= 4 and b.weekday() in (6, 0, 1) and missing >= 24 * 60 and hol:
        return GAP_HOLIDAY, "; ".join(hol)
    a_min = a.hour * 60 + a.minute
    b_min = b.hour * 60 + b.minute
    if missing <= 120 and 16 * 60 <= a_min <= 17 * 60 + 15 and 17 * 60 + 30 <= b_min <= 18 * 60 + 30 \
            and a.date() == b.date():
        return GAP_DAILY, ""
    if hol:
        return GAP_HOLIDAY, "; ".join(hol)
    return GAP_ANOMALOUS, ""


# ---------------------------------------------------------------------------
# Controlli e pulizia
# ---------------------------------------------------------------------------
@dataclass
class DataReport:
    files_used: List[Dict] = field(default_factory=list)
    formats: List[str] = field(default_factory=list)
    tz_hours: float = DEFAULT_TZ
    rows_loaded: int = 0
    spurious_rows: int = 0
    was_sorted: bool = True
    time_decreases: int = 0
    file_overlaps: List[str] = field(default_factory=list)
    overlap_rows: int = 0
    exact_duplicates: int = 0
    conflicting_duplicates: int = 0
    nan_rows: int = 0
    non_positive_rows: int = 0
    out_of_scale_rows: int = 0
    ohlc_inconsistent_rows: int = 0
    zero_volume_rows: int = 0
    jumps: List[Dict] = field(default_factory=list)
    median_step_min: float = float("nan")
    rows_clean: int = 0
    first_time: Optional[pd.Timestamp] = None
    last_time: Optional[pd.Timestamp] = None
    gaps: Optional[pd.DataFrame] = None
    weeks: Optional[pd.DataFrame] = None
    week_fail_ratio: float = 0.0
    tz_anomaly: bool = False
    candles_15m: int = 0
    candles_15m_incomplete: int = 0
    candles_15m_missing_in_market: int = 0


def wilder_atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """ATR di Wilder vettoriale (seed SMA) usato SOLO per il controllo dei salti sui dati 1m."""
    n = len(close)
    out = np.full(n, np.nan)
    if n < period:
        return out
    pc = np.r_[np.nan, close[:-1]]
    tr = np.nanmax(np.vstack([high - low, np.abs(high - pc), np.abs(low - pc)]), axis=0)
    tr[0] = high[0] - low[0]
    out[period - 1] = tr[:period].mean()
    a = out[period - 1]
    for i in range(period, n):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a
    return out


def check_and_clean(df: pd.DataFrame, info: Dict, tz_hours: float) -> Tuple[pd.DataFrame, DataReport]:
    """Esegue i controlli obbligatori, li documenta nel log e restituisce i dati 1m puliti."""
    rep = DataReport(files_used=info["files"], formats=sorted(info["formats"]), tz_hours=tz_hours,
                     rows_loaded=len(df), spurious_rows=info["spurious_rows"])
    log.info("=== CONTROLLI SUI DATI ===")
    log.info("Righe caricate: %d; righe spurie scartate (intestazioni/vuote): %d", rep.rows_loaded, rep.spurious_rows)
    log.info("Fuso dei dati: UTC%+g (utc = orario file %+g ore)", tz_hours, -tz_hours)

    nan = df[["open", "high", "low", "close"]].isna().any(axis=1)
    rep.nan_rows = int(nan.sum())
    df = df[~nan].copy()
    df["volume"] = df["volume"].fillna(0.0)
    log.info("Righe con prezzi mancanti/non numerici: %d (scartate)", rep.nan_rows)

    t = df["time_ns"].to_numpy()
    rep.time_decreases = int((np.diff(t) < 0).sum()) if len(t) > 1 else 0
    rep.was_sorted = rep.time_decreases == 0
    log.info("Ordinamento cronologico (nell'ordine dei file): %s (ritorni indietro: %d)",
             "OK" if rep.was_sorted else "NON ordinato -> riordino", rep.time_decreases)

    # sovrapposizioni tra file
    if df["source_file"].nunique() > 1:
        rng = df.groupby("source_file", sort=False)["time_ns"].agg(["min", "max"]).sort_values("min")
        prev_name, prev_max = None, None
        for name, row in rng.iterrows():
            if prev_max is not None and row["min"] <= prev_max:
                n_ov = int(((df["source_file"] == name) & (df["time_ns"] <= prev_max)).sum())
                rep.overlap_rows += n_ov
                rep.file_overlaps.append(f"{prev_name} (fine {pd.Timestamp(prev_max, tz='UTC')}) si sovrappone a "
                                         f"{name} (inizio {pd.Timestamp(row['min'], tz='UTC')}): {n_ov} righe")
            prev_name = name
            prev_max = row["max"] if prev_max is None else max(prev_max, row["max"])
    log.info("Sovrapposizioni tra file: %d %s (le righe sovrapposte sono gestite come duplicati)",
             len(rep.file_overlaps), rep.file_overlaps)

    df = df.sort_values("time_ns", kind="mergesort").reset_index(drop=True)
    vals = ["time_ns", "open", "high", "low", "close", "volume"]
    ex = df.duplicated(subset=vals, keep="first")
    rep.exact_duplicates = int(ex.sum())
    df = df[~ex]
    cf = df.duplicated(subset=["time_ns"], keep="first")
    rep.conflicting_duplicates = int(cf.sum())
    if rep.conflicting_duplicates:
        log.warning("Timestamp duplicati con valori diversi: %d (tengo il primo, cioè il file precedente), es. %s",
                    rep.conflicting_duplicates,
                    [str(pd.Timestamp(x, tz="UTC")) for x in df.loc[cf, "time_ns"].head(5)])
    df = df[~cf].reset_index(drop=True)
    log.info("Duplicati identici rimossi: %d; duplicati di timestamp con valori diversi rimossi: %d",
             rep.exact_duplicates, rep.conflicting_duplicates)

    p = df[["open", "high", "low", "close"]]
    nonpos = (p <= 0).any(axis=1)
    rep.non_positive_rows = int(nonpos.sum())
    scale = ((p < PRICE_MIN) | (p > PRICE_MAX)).any(axis=1) & ~nonpos
    rep.out_of_scale_rows = int(scale.sum())
    incoh = ~((df["low"] <= df[["open", "close"]].min(axis=1)) & (df[["open", "close"]].max(axis=1) <= df["high"]))
    incoh &= ~nonpos & ~scale
    rep.ohlc_inconsistent_rows = int(incoh.sum())
    for label, mask in [("prezzi non positivi", nonpos), (f"prezzi fuori scala ({PRICE_MIN:.0f}-{PRICE_MAX:.0f})", scale),
                        ("OHLC incoerente (low <= open/close <= high violato)", incoh)]:
        n = int(mask.sum())
        if n:
            log.warning("Righe con %s: %d (scartate: diventano minuti mancanti, nessun dato inventato), es. %s",
                        label, n, [str(pd.Timestamp(x, tz="UTC")) for x in df.loc[mask, "time_ns"].head(5)])
        else:
            log.info("Righe con %s: 0", label)
    df = df[~(nonpos | scale | incoh)].reset_index(drop=True)

    rep.zero_volume_rows = int((df["volume"] == 0).sum())
    log.info("Righe con volume zero: %d su %d (%.1f%%). Normale per HistData: solo riportato, nessuna riga scartata.",
             rep.zero_volume_rows, len(df), 100.0 * rep.zero_volume_rows / max(len(df), 1))

    t = df["time_ns"].to_numpy()
    if len(t) < 2:
        raise ValueError("Dati insufficienti dopo la pulizia")
    rep.median_step_min = float(np.median(np.diff(t)) / MINUTE_NS)
    log.info("Differenza mediana tra timestamp consecutivi: %.3f minuti", rep.median_step_min)
    if abs(rep.median_step_min - 1.0) > 1e-9:
        raise ValueError(f"Il timeframe dei dati non è 1 minuto (mediana {rep.median_step_min} min): interrompo")
    if (t % MINUTE_NS != 0).any():
        raise ValueError("Timestamp non allineati al minuto")

    rep.first_time = pd.Timestamp(t[0], tz="UTC")
    rep.last_time = pd.Timestamp(t[-1], tz="UTC")
    if rep.last_time > MAX_ALLOWED_DATE:
        raise ValueError(f"Data massima {rep.last_time} oltre il 2025-12-31: interrompo (vincolo sui dati)")
    log.info("Periodo coperto (UTC): %s -> %s; data massima <= 2025-12-31: OK", rep.first_time, rep.last_time)

    # salti di prezzo anomali (|open - close precedente| > 20 x ATR(14) 1m precedente)
    o, h, l, c = (df[x].to_numpy() for x in ["open", "high", "low", "close"])
    atr1 = wilder_atr_np(h, l, c, 14)
    jump = np.abs(o[1:] - c[:-1])
    thr = JUMP_ATR_MULT * atr1[:-1]
    idx = np.where(np.isfinite(thr) & (jump > thr))[0] + 1
    for i in idx:
        rep.jumps.append({"time": str(pd.Timestamp(t[i], tz="UTC")), "prev_close": c[i - 1], "open": o[i],
                          "jump": o[i] - c[i - 1], "atr_1m": atr1[i - 1],
                          "minuti_dalla_candela_precedente": int((t[i] - t[i - 1]) // MINUTE_NS)})
    after_gap = sum(1 for j in rep.jumps if j["minuti_dalla_candela_precedente"] > 1)
    log.info("Salti di prezzo > %.0f x ATR(14) 1m tra candele consecutive: %d (di cui %d dopo un buco). "
             "Non scartati (sono prezzi reali); elenco in output/salti_prezzo.csv",
             JUMP_ATR_MULT, len(rep.jumps), after_gap)
    for j in rep.jumps[:20]:
        log.info("  salto %s", j)

    rep.rows_clean = len(df)
    log.info("Righe 1m pulite: %d", rep.rows_clean)
    rep.gaps = analyse_gaps(t)
    rep.weeks, rep.week_fail_ratio = check_week_opens(rep.gaps)
    rep.tz_anomaly = rep.week_fail_ratio > WEEK_FAIL_THRESHOLD
    return df, rep


def analyse_gaps(t: np.ndarray) -> pd.DataFrame:
    """Elenco di tutti i buchi tra candele base consecutive, classificati."""
    years = list(range(pd.Timestamp(t[0], tz="UTC").year - 1, pd.Timestamp(t[-1], tz="UTC").year + 2))
    hol = holiday_dates(years)
    d = np.diff(t)
    gi = np.where(d > MINUTE_NS)[0]
    rows = []
    for i in gi:
        missing = int(d[i] // MINUTE_NS) - 1
        cat, note = classify_gap(int(t[i]), int(t[i + 1]), missing, hol)
        rows.append({"ultima_candela_prima_utc": pd.Timestamp(t[i], tz="UTC"),
                     "prima_candela_dopo_utc": pd.Timestamp(t[i + 1], tz="UTC"),
                     "minuti_mancanti": missing, "categoria": cat, "nota": note})
    gaps = pd.DataFrame(rows, columns=["ultima_candela_prima_utc", "prima_candela_dopo_utc", "minuti_mancanti",
                                       "categoria", "nota"])
    log.info("=== BUCHI NEI DATI 1m ===")
    for cat in EXPECTED_GAPS + (GAP_ANOMALOUS,):
        sub = gaps[gaps["categoria"] == cat]
        log.info("Buchi %s: %d, minuti mancanti totali %d", cat, len(sub), int(sub["minuti_mancanti"].sum()))
    an = gaps[gaps["categoria"] == GAP_ANOMALOUS]
    if len(an):
        buckets = pd.cut(an["minuti_mancanti"], [0, 1, 5, 15, 60, 10 ** 9], labels=["1", "2-5", "6-15", "16-60", ">60"])
        log.info("Buchi anomali per durata (minuti): %s", buckets.value_counts().sort_index().to_dict())
    big = an[an["minuti_mancanti"] > LARGE_GAP_MINUTES]
    log.info("Buchi anomali > %d minuti: %d", LARGE_GAP_MINUTES, len(big))
    for _, r in big.iterrows():
        log.info("  ANOMALO %s -> %s (%d minuti)", r["ultima_candela_prima_utc"], r["prima_candela_dopo_utc"],
                 r["minuti_mancanti"])
    for _, r in gaps[gaps["categoria"].isin([GAP_WEEKEND, GAP_HOLIDAY])].iterrows():
        log.info("  %s %s -> %s (%d minuti) %s", r["categoria"], r["ultima_candela_prima_utc"],
                 r["prima_candela_dopo_utc"], r["minuti_mancanti"], r["nota"])
    return gaps


def check_week_opens(gaps: pd.DataFrame) -> Tuple[pd.DataFrame, float]:
    """
    Verifica del fuso con il calendario di mercato: la prima candela di ogni settimana (prima candela dopo un
    buco di almeno 24 ore che comprende un sabato) deve cadere alla riapertura della domenica alle 18:00 di
    New York = 23:00 UTC con l'ora solare, 22:00 UTC con l'ora legale. Tolleranza: da -10 a +30 minuti.
    """
    rows = []
    for _, g in gaps.iterrows():
        if g["minuti_mancanti"] < 24 * 60:
            continue
        a, b = g["ultima_candela_prima_utc"], g["prima_candela_dopo_utc"]
        days = pd.date_range(a.normalize(), b.normalize(), freq="D")
        if not any(d.weekday() == 5 for d in days):
            continue
        b_ny = b.tz_convert(MARKET_TZ)
        expected_ny = pd.Timestamp(dt.datetime.combine(b_ny.date(), dt.time(18, 0))).tz_localize(MARKET_TZ)
        expected_utc = expected_ny.tz_convert("UTC")
        diff = (b - expected_utc).total_seconds() / 60.0
        sunday = b_ny.weekday() == 6
        ok = sunday and -10 <= diff <= 30
        rows.append({"settimana": b_ny.strftime("%G-W%V"), "prima_candela_utc": b,
                     "riapertura_attesa_utc": expected_utc if sunday else pd.NaT,
                     "ora_attesa_utc": f"{expected_utc.hour:02d}:00" if sunday else "",
                     "scostamento_minuti": diff if sunday else np.nan,
                     "giorno_ny": b_ny.day_name(), "esito": "OK" if ok else
                     ("NON_DOMENICA (festività?)" if not sunday else "FUORI_ORARIO")})
    weeks = pd.DataFrame(rows)
    ratio = float((weeks["esito"] != "OK").mean()) if len(weeks) else 0.0
    log.info("=== VERIFICA DEL FUSO (riapertura settimanale) ===")
    for _, r in weeks.iterrows():
        log.info("  %s prima candela %s, attesa %s UTC, scostamento %s min -> %s", r["settimana"],
                 r["prima_candela_utc"], r["ora_attesa_utc"] or "-",
                 "-" if pd.isna(r["scostamento_minuti"]) else f"{r['scostamento_minuti']:.0f}", r["esito"])
    log.info("Settimane verificate: %d; non conformi: %.1f%% (soglia anomalia %.0f%%)", len(weeks), 100 * ratio,
             100 * WEEK_FAIL_THRESHOLD)
    if ratio > WEEK_FAIL_THRESHOLD:
        log.warning("ANOMALIA DEL FUSO: più del %.0f%% delle settimane non riapre all'orario atteso. "
                    "La conversione NON viene cambiata automaticamente (usare --tz se necessario).",
                    100 * WEEK_FAIL_THRESHOLD)
    return weeks, ratio


# ---------------------------------------------------------------------------
# Candele 15m
# ---------------------------------------------------------------------------
@dataclass
class MarketData:
    """Candele base 1m e candele 15m (solo quelle con almeno una candela base)."""
    t1: np.ndarray
    o1: np.ndarray
    h1: np.ndarray
    l1: np.ndarray
    c1: np.ndarray
    t15: np.ndarray
    o15: np.ndarray
    h15: np.ndarray
    l15: np.ndarray
    c15: np.ndarray
    v15: np.ndarray
    n_base: np.ndarray
    start: np.ndarray      # indice della prima candela base di ogni candela 15m
    end: np.ndarray        # indice successivo all'ultima candela base

    def candle(self, k: int) -> st.Candle:
        return st.Candle(time=pd.Timestamp(int(self.t15[k]), tz="UTC"), open=float(self.o15[k]),
                         high=float(self.h15[k]), low=float(self.l15[k]), close=float(self.c15[k]),
                         volume=float(self.v15[k]))


def build_market_data(df: pd.DataFrame, rep: Optional[DataReport] = None) -> MarketData:
    """Aggrega le candele base in 15m allineate a :00/:15/:30/:45 UTC. Nessun dato inventato."""
    t = df["time_ns"].to_numpy().astype(np.int64)
    o, h, l, c, v = (df[x].to_numpy().astype(float) for x in ["open", "high", "low", "close", "volume"])
    bucket = t - (t % TF_NS)
    starts = np.r_[0, np.where(np.diff(bucket) != 0)[0] + 1]
    ends = np.r_[starts[1:], len(t)]
    md = MarketData(t1=t, o1=o, h1=h, l1=l, c1=c, t15=bucket[starts], o15=o[starts],
                    h15=np.maximum.reduceat(h, starts), l15=np.minimum.reduceat(l, starts), c15=c[ends - 1],
                    v15=np.add.reduceat(v, starts), n_base=ends - starts, start=starts, end=ends)
    if rep is not None:
        rep.candles_15m = len(starts)
        rep.candles_15m_incomplete = int((md.n_base < st.TIMEFRAME_MINUTES).sum())
        d15 = np.diff(md.t15) // TF_NS - 1
        rep.candles_15m_missing_in_market = 0
        if rep.gaps is not None and len(rep.gaps):
            an = rep.gaps[rep.gaps["categoria"] == GAP_ANOMALOUS]
            rep.candles_15m_missing_in_market = int((an["minuti_mancanti"] // st.TIMEFRAME_MINUTES).sum())
        log.info("=== CANDELE 15m ===")
        log.info("Candele 15m costruite: %d; incomplete (meno di 15 candele base, marcate): %d; "
                 "intervalli 15m senza candele (non alimentati alla strategia): %d",
                 rep.candles_15m, rep.candles_15m_incomplete, int(d15[d15 > 0].sum()))
    return md


def load_market_data(data_dir: str, name: str, tz_hours: float) -> Tuple[MarketData, DataReport, List[str]]:
    files = find_data_files(data_dir, name)
    info: Dict = {"files": [], "formats": set(), "spurious_rows": 0}
    frames = [read_histdata_file(p, tz_hours, info) for p in files]
    df = pd.concat(frames, ignore_index=True)
    df, rep = check_and_clean(df, info, tz_hours)
    md = build_market_data(df, rep)
    return md, rep, files


# ---------------------------------------------------------------------------
# Replay ed esecuzione
# ---------------------------------------------------------------------------
@dataclass
class Costs:
    spread_per_oz: float = SPREAD_PER_OZ
    slippage_per_oz: float = SLIPPAGE_PER_OZ
    commission_per_lot_side: float = COMMISSION_PER_LOT_SIDE

    @property
    def per_side_oz(self) -> float:
        return self.spread_per_oz / 2.0 + self.slippage_per_oz

    def scaled(self, k: float) -> "Costs":
        return Costs(self.spread_per_oz * k, self.slippage_per_oz * k, self.commission_per_lot_side * k)


@dataclass
class ReplayResult:
    trades: pd.DataFrame
    orders: pd.DataFrame
    ranges: pd.DataFrame
    equity: pd.Series            # equity mark-to-market alla chiusura di ogni candela 15m di trading
    in_position: pd.Series       # True se a fine candela c'è una posizione aperta
    actions_log: List[Tuple[int, List[st.Action]]]
    inputs_log: List[Tuple[int, Optional[st.Position], Optional[st.PendingOrder]]]
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    skipped: List[Dict]
    costs: Costs
    first_trading_close: float = float("nan")
    last_close: float = float("nan")


def session_of(ts_: pd.Timestamp) -> str:
    h = ts_.hour
    for name, spans in SESSIONS.items():
        if any(a <= h < b for a, b in spans):
            return name
    return "?"


def run_replay(md: MarketData, costs: Optional[Costs] = None, initial_capital: float = INITIAL_CAPITAL,
               record_actions: bool = False, verbose: bool = True) -> ReplayResult:
    """
    Replay come un loop live: a ogni chiusura 15m la strategia riceve la candela, la posizione e l'ordine
    pendente correnti. Le Action emesse alla chiusura della candela N si applicano dalla prima candela base
    della candela N+1. Fill, SL e TP sono simulati sulle candele base 1m.
    """
    costs = costs or Costs()
    cps = costs.per_side_oz
    strat = st.OteFibStrategy()
    n15 = len(md.t15)
    warm = min(st.WARMUP_CANDLES, n15)
    strat.warmup(md.candle(k) for k in range(warm))

    equity = initial_capital
    pos: Optional[st.Position] = None
    pos_info: Dict = {}
    pend: Optional[st.PendingOrder] = None
    pend_info: Dict = {}
    queued: List[st.Action] = []
    trades: List[Dict] = []
    orders: Dict[str, Dict] = {}
    ranges: List[Dict] = []
    skipped: List[Dict] = []
    eq_t, eq_v, inpos = [], [], []
    actions_log: List[Tuple[int, List[st.Action]]] = []
    t1, o1, h1, l1, c1 = md.t1, md.o1, md.h1, md.l1, md.c1

    def close_trade(j: int, exit_raw: float, reason: str, at_open: bool) -> None:
        nonlocal equity, pos, pos_info
        side, qty, fill, sl = pos.side, pos.qty, pos.entry, pos.sl
        d = 1.0 if side == st.LONG else -1.0
        entry_exec = fill + d * cps
        exit_exec = exit_raw - d * cps
        commission = 2 * costs.commission_per_lot_side * qty
        gross = (exit_raw - fill) * d * qty * st.CONTRACT_SIZE_OZ
        pnl = (exit_exec - entry_exec) * d * qty * st.CONTRACT_SIZE_OZ - commission
        risk_usd = abs(fill - sl) * qty * st.CONTRACT_SIZE_OZ
        gap_min = int((t1[j] - t1[j - 1]) // MINUTE_NS) if j > 0 else 0
        exit_time = pd.Timestamp(int(t1[j]), tz="UTC")
        rec = dict(pos_info)
        rec.update({
            "exit_time": exit_time, "exit_reason": reason, "exit_price": exit_raw, "exit_exec_price": exit_exec,
            "entry_exec_price": entry_exec, "gross_pnl": gross, "costs": gross - pnl, "commission": commission,
            "pnl": pnl, "r": pnl / risk_usd if risk_usd > 0 else np.nan, "r_gross": gross / risk_usd if risk_usd > 0 else np.nan,
            "equity_before": equity, "equity_after": equity + pnl,
            "exit_at_open": at_open, "exit_after_gap_min": gap_min if at_open else 0,
            "duration_hours": (exit_time - rec["fill_time"]).total_seconds() / 3600.0,
        })
        rec["duration_candles_15m"] = None
        equity += pnl
        trades.append(rec)
        pos = None
        pos_info = {}

    def check_exit(j: int, fill_bar: bool) -> None:
        """SL/TP sulla candela base j. Sulla candela del fill solo lo SL. SL e TP insieme -> SL."""
        side, sl, tp = pos.side, pos.sl, pos.tp
        if not fill_bar and j > 0 and (t1[j] - t1[j - 1]) > 12 * 60 * MINUTE_NS:
            pos_info["over_weekend"] = True
        o, h, l = o1[j], h1[j], l1[j]
        if side == st.LONG:
            if not fill_bar and o <= sl:
                close_trade(j, o, EXIT_SL, True)
            elif not fill_bar and o > tp:
                close_trade(j, o, EXIT_TP, True)
            elif l <= sl:
                close_trade(j, sl, EXIT_SL, False)
            elif not fill_bar and h > tp:
                close_trade(j, tp, EXIT_TP, False)
        else:
            if not fill_bar and o >= sl:
                close_trade(j, o, EXIT_SL, True)
            elif not fill_bar and o < tp:
                close_trade(j, o, EXIT_TP, True)
            elif h >= sl:
                close_trade(j, sl, EXIT_SL, False)
            elif not fill_bar and l < tp:
                close_trade(j, tp, EXIT_TP, False)

    def try_fill(j: int, k: int) -> None:
        """Fill del limite: long solo se low < limite (il tocco non basta), all'open se l'open è già sotto."""
        nonlocal pend, pend_info, pos, pos_info
        side, lim = pend.side, pend.limit_price
        o, h, l = o1[j], h1[j], l1[j]
        fill = None
        at_open = False
        if side == st.LONG:
            if o < lim:
                fill, at_open = o, True
            elif l < lim:
                fill = lim
        else:
            if o > lim:
                fill, at_open = o, True
            elif h > lim:
                fill = lim
        if fill is None:
            return
        fill_time = pd.Timestamp(int(t1[j]), tz="UTC")
        qty, reason = st.position_sizing(equity, fill, pend.sl, side)
        orec = orders[pend.id]
        if qty <= 0:
            orec.update({"outcome": reason, "outcome_time": fill_time})
            skipped.append({"order_id": pend.id, "time": fill_time, "side": side, "limit_price": lim,
                            "fill_price": fill, "sl": pend.sl, "reason": reason, "equity": equity})
            if verbose:
                log.info("Trade saltato (%s): ordine %s, fill %.2f, SL %.2f", reason, pend.id, fill, pend.sl)
            pend, pend_info = None, {}
            return
        orec.update({"outcome": ORDER_FILLED, "outcome_time": fill_time})
        pos = st.Position(side=side, entry=fill, sl=pend.sl, tp=pend.tp, qty=qty)
        notional = qty * st.CONTRACT_SIZE_OZ * fill
        pos_info = dict(pend_info)
        pos_info.update({
            "fill_time": fill_time, "fill_price": fill, "fill_at_open": at_open, "qty_lots": qty,
            "risk_pct_effective": 100.0 * abs(fill - pend.sl) * qty * st.CONTRACT_SIZE_OZ / equity,
            "leverage": notional / equity, "session": session_of(fill_time), "over_weekend": False,
            "candles_to_fill": k - pend_info["placed_k"],
            "hours_to_fill": (fill_time - pend_info["active_from"]).total_seconds() / 3600.0,
            "r_theoretical": abs(pend.tp - fill) / abs(fill - pend.sl),
        })
        pend, pend_info = None, {}
        check_exit(j, fill_bar=True)

    queued_meta: Dict[str, Dict] = {}
    inputs_log: List[Tuple[int, Optional[st.Position], Optional[st.PendingOrder]]] = []
    for k in range(warm, n15):
        active_from = pd.Timestamp(int(t1[md.start[k]]), tz="UTC")
        # 1) applica le Action emesse alla chiusura della candela precedente
        for a in queued:
            if a.type == st.CANCEL:
                if pend is not None and pend.id == a.id:
                    orders[a.id].update({"outcome": a.reason, "outcome_time": active_from})
                    pend, pend_info = None, {}
            elif a.type == st.PLACE_LIMIT:
                if pos is not None or pend is not None:
                    raise RuntimeError("PLACE_LIMIT con posizione o ordine già presenti: violazione dell'interfaccia")
                pend = st.PendingOrder(id=a.id, side=a.side, limit_price=a.limit_price, sl=a.sl, tp=a.tp,
                                       created_time=active_from, candles_alive=0,
                                       range_high=a.range_high, range_low=a.range_low)
                pend_info = dict(queued_meta[a.id])
                pend_info["active_from"] = active_from
                orders[a.id]["active_from"] = active_from
        queued = []
        # 2) candele base della candela 15m k
        if pos is not None or pend is not None:
            for j in range(md.start[k], md.end[k]):
                if pos is not None:
                    check_exit(j, fill_bar=False)
                elif pend is not None:
                    try_fill(j, k)
                else:
                    break
        # 3) equity mark-to-market e chiusura della candela per la strategia
        close_k = c1[md.end[k] - 1]
        mtm = equity
        if pos is not None:
            d = 1.0 if pos.side == st.LONG else -1.0
            mtm += (close_k - pos.entry) * d * pos.qty * st.CONTRACT_SIZE_OZ
        close_time = pd.Timestamp(int(md.t15[k] + TF_NS), tz="UTC")
        eq_t.append(close_time)
        eq_v.append(mtm)
        inpos.append(pos is not None)

        candle = md.candle(k)
        if record_actions:
            inputs_log.append((k, None if pos is None else st.Position(**vars(pos)),
                               None if pend is None else st.PendingOrder(**vars(pend))))
        actions = strat.on_candle_close(candle, pos, pend)
        if pos is not None and actions:
            raise RuntimeError("La strategia ha emesso Action con una posizione aperta")
        if record_actions:
            actions_log.append((k, list(actions)))
        for leg in strat.legs_this_candle:
            r = dict(leg)
            r["confirm_time"] = close_time
            r["confirm_candle_open_utc"] = leg["confirm_time"]
            ranges.append(r)
        queued_meta = {}
        for a in actions:
            if a.type == st.PLACE_LIMIT:
                leg = next(x for x in strat.legs_this_candle if x.get("order_id") == a.id)
                meta = {"order_id": a.id, "placed_k": k, "placed_time": close_time, "side": a.side,
                        "limit_price": a.limit_price, "sl": a.sl, "tp": a.tp,
                        "range_high": a.range_high, "range_high_time": a.range_high_time,
                        "range_low": a.range_low, "range_low_time": a.range_low_time,
                        "level_618": leg["level_618"], "level_705": leg["level_705"], "level_786": leg["level_786"],
                        "atr_at_placement": leg["atr"]}
                queued_meta[a.id] = meta
                exp_k = k + st.ORDER_EXPIRY_CANDLES
                orders[a.id] = {"order_id": a.id, "side": a.side, "placed_time": close_time,
                                "active_from": pd.NaT,
                                "expiry_time": (pd.Timestamp(int(md.t15[exp_k] + TF_NS), tz="UTC")
                                                if exp_k < n15 else pd.NaT),
                                "limit_price": a.limit_price, "sl": a.sl, "tp": a.tp,
                                "range_high": a.range_high, "range_low": a.range_low,
                                "outcome": None, "outcome_time": pd.NaT}
        queued = list(actions)

    # fine dati
    last_j = len(t1) - 1
    if pos is not None:
        close_trade(last_j, c1[last_j], EXIT_END, False)
        if eq_v:
            eq_v[-1] = equity
    if pend is not None:
        orders[pend.id].update({"outcome": ORDER_END_OF_DATA, "outcome_time": pd.Timestamp(int(t1[last_j]), tz="UTC")})
        pend = None
    for oid, a in [(a.id, a) for a in queued if a.type == st.PLACE_LIMIT]:
        orders[oid].update({"outcome": ORDER_END_OF_DATA, "outcome_time": pd.Timestamp(int(t1[last_j]), tz="UTC")})

    trades_df = pd.DataFrame(trades)
    if len(trades_df):
        f15 = np.searchsorted(md.t15, trades_df["fill_time"].map(lambda x: x.value).to_numpy(), side="right")
        e15 = np.searchsorted(md.t15, trades_df["exit_time"].map(lambda x: x.value).to_numpy(), side="right")
        trades_df["duration_candles_15m"] = e15 - f15 + 1
    equity_s = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t), name="equity")
    res = ReplayResult(
        trades=trades_df, orders=pd.DataFrame(list(orders.values())), ranges=pd.DataFrame(ranges),
        equity=equity_s, in_position=pd.Series(inpos, index=equity_s.index), actions_log=actions_log,
        inputs_log=inputs_log,
        start_time=pd.Timestamp(int(md.t15[warm]), tz="UTC") if warm < n15 else pd.Timestamp(int(md.t15[-1]), tz="UTC"),
        end_time=pd.Timestamp(int(t1[-1]), tz="UTC"), skipped=skipped, costs=costs,
        first_trading_close=float(md.c15[warm - 1]) if warm > 0 else float(md.c15[0]),
        last_close=float(c1[-1]),
    )
    return res


# ---------------------------------------------------------------------------
# Metriche
# ---------------------------------------------------------------------------
def profit_factor(pnl: pd.Series) -> float:
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


def max_consecutive_losses(pnl: pd.Series) -> int:
    best = cur = 0
    for x in pnl:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def drawdown_stats(equity: pd.Series) -> Tuple[float, float]:
    """Max drawdown in % e in USD."""
    if len(equity) == 0:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = equity - peak
    return float((dd / peak).min() * 100.0), float(dd.min())


def binomial_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) con X ~ Binomiale(n, p)."""
    if n == 0 or not (0 < p < 1):
        return float("nan")
    return float(sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def compute_metrics(res: ReplayResult, initial_capital: float = INITIAL_CAPITAL) -> Dict:
    tr = res.trades
    closed = tr[tr["exit_reason"] != EXIT_END] if len(tr) else tr
    end_rows = tr[tr["exit_reason"] == EXIT_END] if len(tr) else tr
    m: Dict = {}
    n = len(closed)
    pnl = closed["pnl"] if n else pd.Series(dtype=float)
    net = float(pnl.sum()) if n else 0.0
    years = max((res.end_time - res.start_time).total_seconds() / (365.25 * 86400), 1e-9)
    m["periodo_trading_inizio"] = str(res.start_time)
    m["periodo_trading_fine"] = str(res.end_time)
    m["anni_trading"] = years
    m["capitale_iniziale"] = initial_capital
    m["profitto_netto_usd"] = net
    m["rendimento_pct"] = 100.0 * net / initial_capital
    m["cagr_pct"] = 100.0 * ((1 + net / initial_capital) ** (1 / years) - 1) if net > -initial_capital else -100.0
    m["equity_finale_inclusi_fine_dati"] = float(res.equity.iloc[-1]) if len(res.equity) else initial_capital
    m["trade_fine_dati"] = len(end_rows)
    m["pnl_trade_fine_dati_usd"] = float(end_rows["pnl"].sum()) if len(end_rows) else 0.0
    m["numero_trade"] = n
    m["trade_long"] = int((closed["side"] == st.LONG).sum()) if n else 0
    m["trade_short"] = int((closed["side"] == st.SHORT).sum()) if n else 0
    wins = closed[closed["pnl"] > 0] if n else closed
    losses = closed[closed["pnl"] <= 0] if n else closed
    m["profit_factor"] = profit_factor(pnl) if n else float("nan")
    m["win_rate_pct"] = 100.0 * len(wins) / n if n else float("nan")
    avg_win_r = float(wins["r"].mean()) if len(wins) else float("nan")
    avg_loss_r = float(-losses["r"].mean()) if len(losses) else float("nan")
    m["r_medio_vincenti"] = avg_win_r
    m["r_medio_perdenti"] = -avg_loss_r if not math.isnan(avg_loss_r) else float("nan")
    m["rr_teorico"] = st.ENTRY_LEVEL / (st.SL_LEVEL - st.ENTRY_LEVEL)
    m["r_teorico_medio_sul_fill"] = float(closed["r_theoretical"].mean()) if n else float("nan")
    m["win_rate_pareggio_teorico_lordo_pct"] = 100.0 * (st.SL_LEVEL - st.ENTRY_LEVEL) / st.SL_LEVEL
    if len(wins) and len(losses):
        m["win_rate_pareggio_effettivo_pct"] = 100.0 * avg_loss_r / (avg_win_r + avg_loss_r)
    else:
        rt = m["r_teorico_medio_sul_fill"]
        m["win_rate_pareggio_effettivo_pct"] = 100.0 / (1 + rt) if n else float("nan")
    m["expectancy_usd"] = float(pnl.mean()) if n else float("nan")
    m["expectancy_r"] = float(closed["r"].mean()) if n else float("nan")
    m["payoff_ratio"] = float(wins["pnl"].mean() / -losses["pnl"].mean()) if len(wins) and len(losses) and losses["pnl"].mean() < 0 else float("nan")
    dd_pct, dd_usd = drawdown_stats(res.equity)
    m["max_drawdown_pct"] = dd_pct
    m["max_drawdown_usd"] = dd_usd
    daily = res.equity.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        m["sharpe"] = float(rets.mean() / rets.std() * math.sqrt(TRADING_DAYS_PER_YEAR))
        down = rets[rets < 0]
        dstd = math.sqrt((np.minimum(rets, 0) ** 2).mean())
        m["sortino"] = float(rets.mean() / dstd * math.sqrt(TRADING_DAYS_PER_YEAR)) if dstd > 0 and len(down) else float("nan")
    else:
        m["sharpe"] = m["sortino"] = float("nan")
    m["max_perdite_consecutive"] = max_consecutive_losses(pnl) if n else 0
    m["durata_media_ore"] = float(closed["duration_hours"].mean()) if n else float("nan")
    m["durata_mediana_ore"] = float(closed["duration_hours"].median()) if n else float("nan")
    m["durata_massima_ore"] = float(closed["duration_hours"].max()) if n else float("nan")
    m["durata_media_candele_15m"] = float(closed["duration_candles_15m"].mean()) if n else float("nan")
    m["durata_massima_candele_15m"] = float(closed["duration_candles_15m"].max()) if n else float("nan")
    m["tempo_in_posizione_pct"] = 100.0 * float(res.in_position.mean()) if len(res.in_position) else 0.0
    m["costi_totali_usd"] = float(closed["costs"].sum()) if n else 0.0
    m["costo_medio_r"] = float((closed["r_gross"] - closed["r"]).mean()) if n else float("nan")
    m["rischio_effettivo_medio_pct"] = float(closed["risk_pct_effective"].mean()) if n else float("nan")
    m["rischio_effettivo_min_pct"] = float(closed["risk_pct_effective"].min()) if n else float("nan")
    m["rischio_effettivo_max_pct"] = float(closed["risk_pct_effective"].max()) if n else float("nan")
    m["leva_massima"] = float(tr["leverage"].max()) if len(tr) else float("nan")
    m["trade_aperti_nel_weekend"] = int(tr["over_weekend"].sum()) if len(tr) else 0
    m["uscite_su_gap_all_open"] = int(tr["exit_at_open"].sum()) if len(tr) else 0
    m["uscite_su_gap_dopo_buco_oltre_12h"] = int((tr["exit_after_gap_min"] > 12 * 60).sum()) if len(tr) else 0
    m["fill_all_open_su_gap"] = int(tr["fill_at_open"].sum()) if len(tr) else 0
    m["uscite_tp"] = int((closed["exit_reason"] == EXIT_TP).sum()) if n else 0
    m["uscite_sl"] = int((closed["exit_reason"] == EXIT_SL).sum()) if n else 0
    # imbuto dei range
    rg = res.ranges
    m["range_totali"] = len(rg)
    for o in st.RANGE_OUTCOMES:
        m[f"range_{o}"] = int((rg["outcome"] == o).sum()) if len(rg) else 0
    for side in (st.LONG, st.SHORT):
        m[f"range_{side}"] = int((rg["side"] == side).sum()) if len(rg) else 0
        m[f"range_{side}_SCARTATO_EMA"] = int(((rg["side"] == side) & (rg["outcome"] == st.SCARTATO_EMA)).sum()) if len(rg) else 0
    # imbuto degli ordini
    od = res.orders
    placed = len(od)
    filled = int((od["outcome"] == ORDER_FILLED).sum()) if placed else 0
    m["ordini_piazzati"] = placed
    m["ordini_eseguiti"] = filled
    m["fill_rate_pct"] = 100.0 * filled / placed if placed else float("nan")
    for r in st.CANCEL_REASONS:
        m[f"ordini_cancellati_{r}"] = int((od["outcome"] == r).sum()) if placed else 0
    m["ordini_saltati_distanza_sl"] = int((od["outcome"] == st.SKIP_SL_DISTANCE).sum()) if placed else 0
    m["ordini_saltati_sotto_min_lot"] = int((od["outcome"] == st.SKIP_MIN_LOT).sum()) if placed else 0
    m["ordini_pendenti_a_fine_dati"] = int((od["outcome"] == ORDER_END_OF_DATA).sum()) if placed else 0
    m["tempo_medio_piazzamento_fill_ore"] = float(tr["hours_to_fill"].mean()) if len(tr) else float("nan")
    m["candele_medie_piazzamento_fill"] = float(tr["candles_to_fill"].mean()) if len(tr) else float("nan")
    # contesto e robustezza
    if res.first_trading_close and not math.isnan(res.first_trading_close):
        m["buy_and_hold_xauusd_pct"] = 100.0 * (res.last_close / res.first_trading_close - 1)
    if n:
        k = len(wins)
        p_be = m["win_rate_pareggio_effettivo_pct"] / 100.0
        m["p_value_win_rate_vs_pareggio"] = binomial_sf(k, n, p_be) if 0 < p_be < 1 else float("nan")
        top = closed["pnl"].sort_values(ascending=False)
        m["profitto_netto_senza_top5_usd"] = float(net - top.head(5).sum())
        r = closed["r"].to_numpy()
        m["t_stat_expectancy_r"] = float(r.mean() / (r.std(ddof=1) / math.sqrt(n))) if n > 1 and r.std(ddof=1) > 0 else float("nan")
    m["campione_insufficiente"] = n < MIN_TRADES_SAMPLE
    return m


def group_stats(closed: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    rows = []
    for g, sub in closed.groupby(key, sort=True):
        cum = sub["pnl"].cumsum()
        dd = float((cum - cum.cummax().clip(lower=0)).min()) if len(cum) else 0.0
        rows.append({"gruppo": str(g), "trade": len(sub), "profitto_usd": float(sub["pnl"].sum()),
                     "profit_factor": profit_factor(sub["pnl"]),
                     "win_rate_pct": 100.0 * float((sub["pnl"] > 0).mean()),
                     "expectancy_r": float(sub["r"].mean()), "max_drawdown_usd": min(dd, 0.0)})
    return pd.DataFrame(rows)


def breakdowns(res: ReplayResult) -> Dict[str, pd.DataFrame]:
    tr = res.trades
    if not len(tr):
        return {}
    closed = tr[tr["exit_reason"] != EXIT_END].sort_values("exit_time")
    if not len(closed):
        return {}
    ft = closed["fill_time"]
    return {
        "anno": group_stats(closed, ft.map(lambda x: x.year)),
        "trimestre": group_stats(closed, ft.map(lambda x: f"{x.year}-Q{(x.month - 1) // 3 + 1}")),
        "lato": group_stats(closed, closed["side"]),
        "sessione": group_stats(closed, closed["session"]),
    }


# ---------------------------------------------------------------------------
# Grafici
# ---------------------------------------------------------------------------
def plot_equity(res: ReplayResult, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    eq = res.equity
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(eq.index, eq.values, color="#1f5fa8", lw=1.2)
    ax1.axhline(INITIAL_CAPITAL, color="grey", lw=0.8, ls="--")
    ax1.set_title(f"{SYMBOL} 15m - OTE Fibonacci (0.705) con filtro EMA 50/100 - equity (USD, mark-to-market)")
    ax1.set_ylabel("Equity USD")
    ax1.grid(alpha=0.3)
    dd = (eq / eq.cummax() - 1) * 100
    ax2.fill_between(dd.index, dd.values, 0, color="#c0392b", alpha=0.5)
    ax2.set_ylabel("Drawdown %")
    ax2.set_xlabel("Tempo (UTC)")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_trade(md: MarketData, trade: pd.Series, path: str, title: str) -> None:
    """Candele 15m (UTC) con dealing range, box OTE giallo, midline 0.5, limite, SL, TP e marker fill/uscita."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    tv = lambda x: pd.Timestamp(x).value
    t_lo, t_hi = tv(trade["range_low_time"]), tv(trade["range_high_time"])
    k0 = int(np.searchsorted(md.t15, min(t_lo, t_hi))) - 15
    k_fill = int(np.searchsorted(md.t15, tv(trade["fill_time"]), side="right")) - 1
    k_exit = int(np.searchsorted(md.t15, tv(trade["exit_time"]), side="right")) - 1
    k1 = k_exit + 10
    k0 = max(k0, 0, k1 - 400)
    k1 = min(k1, len(md.t15) - 1)
    ks = np.arange(k0, k1 + 1)
    fig, ax = plt.subplots(figsize=(13, 6.5))
    for x, k in enumerate(ks):
        o, h, l, c = md.o15[k], md.h15[k], md.l15[k], md.c15[k]
        col = "#26a69a" if c >= o else "#ef5350"
        ax.vlines(x, l, h, color=col, lw=0.8)
        ax.add_patch(Rectangle((x - 0.3, min(o, c)), 0.6, max(abs(c - o), 1e-6), color=col))
    xpos = lambda t: float(np.searchsorted(md.t15[ks], tv(t), side="right") - 1)
    side, rh, rl = trade["side"], trade["range_high"], trade["range_low"]
    x_h, x_l = xpos(trade["range_high_time"]), xpos(trade["range_low_time"])
    x_start = max(x_h, x_l)
    x_end = xpos(trade["exit_time"]) + 1
    ax.plot([x_l, x_h], [rl, rh], color="#555555", lw=1.2, ls="--", label="dealing range")
    l618, l786 = trade["level_618"], trade["level_786"]
    ax.add_patch(Rectangle((x_start, min(l618, l786)), x_end - x_start, abs(l786 - l618), color="#f1c40f",
                           alpha=0.35, label="zona OTE 0.618-0.786"))
    mid = st.fib_level(side, rh, rl, st.MID_LEVEL)
    for y, c_, ls_, lab in [(mid, "#7f8c8d", ":", "midline 0.5"), (trade["limit_price"], "#2c3e50", "-", "limite 0.705"),
                            (trade["sl"], "#c0392b", "-", "SL (1.0)"), (trade["tp"], "#27ae60", "-", "TP (0.0)")]:
        ax.hlines(y, x_start, x_end, color=c_, ls=ls_, lw=1.2, label=lab)
    ax.scatter([xpos(trade["fill_time"])], [trade["fill_price"]], marker="^" if side == st.LONG else "v",
               s=120, color="#2980b9", zorder=5, label="fill")
    ax.scatter([xpos(trade["exit_time"])], [trade["exit_price"]], marker="X", s=120,
               color="#27ae60" if trade["pnl"] > 0 else "#c0392b", zorder=5, label=f"uscita {trade['exit_reason']}")
    step = max(len(ks) // 10, 1)
    ax.set_xticks(range(0, len(ks), step))
    ax.set_xticklabels([pd.Timestamp(int(md.t15[k]), tz="UTC").strftime("%m-%d %H:%M") for k in ks[::step]],
                       rotation=30, fontsize=8)
    ax.set_xlabel("UTC")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def fmt(x, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, (bool, np.bool_)):
        return "sì" if x else "no"
    if isinstance(x, (int, np.integer)):
        return f"{int(x):,}".replace(",", ".")
    if isinstance(x, (float, np.floating)):
        if math.isnan(x):
            return "n.d."
        if math.isinf(x):
            return "inf"
        return f"{x:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(x)


def df_to_md(df: pd.DataFrame) -> str:
    if df is None or not len(df):
        return "_nessun dato_\n"
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(fmt(r[c]) for c in cols) + " |")
    return "\n".join(out) + "\n"


def evaluation(m: Dict, m2: Dict, bd: Dict[str, pd.DataFrame], res: ReplayResult) -> List[str]:
    """Valutazione automatica e onesta, basata su regole esplicite (nessuna scelta a posteriori)."""
    out = []
    n = m["numero_trade"]
    if n < MIN_TRADES_SAMPLE:
        out.append(f"- **Campione insufficiente**: {n} trade (< {MIN_TRADES_SAMPLE}). Qualunque conclusione è fragile.")
    if n == 0:
        out.append("- Nessun trade chiuso: impossibile valutare la strategia.")
        return out
    pf, pf2 = m["profit_factor"], m2.get("profit_factor", float("nan"))
    wr, be = m["win_rate_pct"], m["win_rate_pareggio_effettivo_pct"]
    out.append(f"- Win rate reale {fmt(wr)}% contro win rate di pareggio {fmt(be)}% (con l'R medio effettivo e i costi "
               f"pagati): margine {fmt(wr - be)} punti.")
    pv = m.get("p_value_win_rate_vs_pareggio", float("nan"))
    out.append(f"- Probabilità di ottenere almeno questo win rate per puro caso se il vero win rate fosse quello di "
               f"pareggio (binomiale): p = {fmt(pv, 3)}. " + ("Sotto 0,05: indizio di vantaggio, ma su un solo periodo."
                                                            if pv < 0.05 else "Non significativo: il risultato è compatibile con il caso."))
    out.append(f"- t-stat dell'expectancy in R: {fmt(m.get('t_stat_expectancy_r', float('nan')))} "
               "(|t| < 2 = non distinguibile da zero).")
    out.append(f"- Profit factor {fmt(pf)}; con costi doppi {fmt(pf2)} "
               + ("(il vantaggio **sparisce** con costi doppi)." if not (pf2 > 1) else "(regge ai costi doppi)."))
    no_top = m.get("profitto_netto_senza_top5_usd", float("nan"))
    out.append(f"- Profitto netto senza i 5 trade migliori: {fmt(no_top)} USD"
               + (" → il risultato **dipende da pochi trade**." if m["profitto_netto_usd"] > 0 and no_top <= 0 else "."))
    for key, label in [("anno", "anno"), ("lato", "lato"), ("sessione", "sessione"), ("trimestre", "trimestre")]:
        d = bd.get(key)
        if d is None or not len(d):
            continue
        pos = d[d["profitto_usd"] > 0]
        tot = d["profitto_usd"].clip(lower=0).sum()
        if m["profitto_netto_usd"] > 0 and len(d) > 1:
            share = d["profitto_usd"].max() / m["profitto_netto_usd"]
            best = d.loc[d["profitto_usd"].idxmax(), "gruppo"]
            out.append(f"- Per {label}: {len(pos)}/{len(d)} gruppi in utile; il migliore ({best}) vale "
                       f"{fmt(100 * share, 0)}% del profitto netto" + (" → **concentrazione**." if share > 1.0 or
                                                                    len(pos) <= len(d) / 2 else "."))
        elif len(d) > 1:
            out.append(f"- Per {label}: {len(pos)}/{len(d)} gruppi in utile.")
    lo, sh = m["trade_long"], m["trade_short"]
    bh = m.get("buy_and_hold_xauusd_pct", float("nan"))
    out.append(f"- Sbilanciamento del filtro EMA: {lo} trade long contro {sh} short "
               f"({fmt(100 * lo / max(lo + sh, 1), 0)}% long). Buy & hold XAUUSD nel periodo di trading: "
               f"{fmt(bh)}%. " + ("In un periodo rialzista il filtro EMA favorisce i long per costruzione: il risultato dei "
                                  "long dipende in buona parte dal regime e non è trasferibile a un mercato laterale o ribassista."
                                  if bh > 0 else "Il periodo non è rialzista: il filtro EMA ha favorito gli short o un equilibrio tra i lati."))
    out.append(f"- Ipotesi di esecuzione: {m['fill_all_open_su_gap']} fill all'open su gap, "
               f"{m['uscite_su_gap_all_open']} uscite all'open su gap ({m['uscite_su_gap_dopo_buco_oltre_12h']} dopo un buco "
               f"> 12 ore, tipicamente il weekend), {m['trade_aperti_nel_weekend']} trade aperti durante un weekend/festività. "
               "Il fill solo con prezzo oltre il limite e lo SL prioritario sulla candela del fill sono ipotesi prudenti: "
               "in live il fill rate potrebbe essere leggermente più alto (anche sui soli tocchi), ma i trade in più "
               "sarebbero tendenzialmente quelli con ritracciamento più debole.")
    return out


def declarations(rep: DataReport) -> List[str]:
    return [
        f"1. Il simbolo è **{SYMBOL}**; i dati sono di **HistData.com** (M1, prezzi **BID**, fuso **EST fisso** "
        f"UTC{rep.tz_hours:+g} convertito in UTC). Lo spread non è nei dati ed è modellato come costo fisso "
        f"({SPREAD_PER_OZ:.2f} USD/oncia, metà per lato): su prezzi bid gli short escono sull'ask, quindi lo spread "
        "fisso si applica a entrambi i lati.",
        "2. I prezzi XAUUSD differiscono tra broker e fornitori di dati (feed, spread, orari): i risultati valgono per "
        "questo feed e possono differire da quelli del broker su cui verrebbe eseguita la strategia in live.",
        "3. Il fuso orario è l'EST fisso dichiarato da HistData e verificato con il calendario di mercato (riapertura della "
        "domenica alle 18:00 di New York): se fosse sbagliato, le candele 15m resterebbero corrette (scostamento di ore "
        "intere) ma il breakdown per sessione no." + (" **ATTENZIONE: la verifica ha segnalato un'anomalia.**"
                                                     if rep.tz_anomaly else ""),
        "4. Swap e finanziamento overnight **non** sono inclusi.",
        "5. L'esecuzione degli ordini limite è simulata: in live un limite toccato può non essere eseguito. Il backtest "
        "usa una regola conservativa (fill solo se il prezzo va STRETTAMENTE oltre il limite; il solo tocco non basta), "
        "ma resta un'approssimazione.",
        f"6. Costi applicati a TUTTI i fill (limite d'ingresso, TP e SL) peggiorando il prezzo: spread {SPREAD_PER_OZ:.2f} "
        f"USD/oncia (metà per lato), slippage {SLIPPAGE_PER_OZ:.2f} USD/oncia per lato, commissione "
        f"{2 * COMMISSION_PER_LOT_SIDE:.0f} USD per lotto andata e ritorno. Applicare spread e slippage anche ai limiti è "
        "un'ipotesi conservativa.",
        "7. SL e TP sono verificati sulle candele 1m dalla candela del fill inclusa; sulla candela del fill si verifica solo "
        "lo SL; se una candela tocca sia SL sia TP si assume lo SL (ipotesi conservativa). Lo SL scatta al tocco, il TP "
        "(ordine limite) solo con prezzo strettamente oltre.",
        "8. Mercato non 24/7: con ordine pendente o trade aperto durante un buco (anche il weekend) fill, SL e TP si "
        "verificano sulla prima candela 1m dopo il buco; se apre già oltre il livello l'evento avviene all'open "
        "(il limite all'open se migliore, SL e TP all'open). I gap del lunedì sono quindi gestiti in modo realistico.",
        "9. Dati usati solo fino al 31/12/2025 (verificato via codice). Il file copre il periodo indicato sotto: "
        "il 2025 non è nei dati e non è usato.",
    ]


def write_report(path: str, rep: DataReport, files: List[str], m: Dict, m2: Dict, res: ReplayResult,
                 bd: Dict[str, pd.DataFrame], bd2: Dict[str, pd.DataFrame]) -> None:
    L: List[str] = []
    add = L.append
    add(f"# Report backtest — OTE Fibonacci {SYMBOL} 15m con filtro EMA 50/100\n")
    add("_Report generato automaticamente da `backtest.py` (backtest unico, nessuna ottimizzazione)._\n")
    add("## Dichiarazioni obbligatorie\n")
    L.extend(declarations(rep))
    add("")
    add("## Strategia\n")
    add("- **Dall'indicatore alla strategia causale.** L'indicatore \"OTE visible chart\" (@twingall) usa massimo e minimo "
        "del grafico VISIBILE, calcolati sull'ultima barra: è un look-ahead puro. Qui il grafico visibile diventa "
        "l'**ultimo dealing range causale**: due punti consecutivi di uno zigzag costruito solo con swing pivot "
        f"confermati ({st.PIVOT_LEFT} candele a sinistra, {st.PIVOT_RIGHT} a destra). Un pivot sulla candela i è noto "
        f"solo alla chiusura della candela i+{st.PIVOT_RIGHT}: **ritardo di {st.PIVOT_RIGHT} candele = "
        f"{st.PIVOT_RIGHT * st.TIMEFRAME_MINUTES} minuti**. La formula dei livelli è identica a quella dell'indicatore.")
    add(f"- Filtro di trend: EMA {st.EMA_FAST} > EMA {st.EMA_SLOW} → solo OTE long; < → solo short; uguali → nessun "
        f"ordine. Le EMA devono essere separate di almeno {st.EMA_SEP_MIN_ATR} ATR nella direzione del range in almeno "
        f"una candela dello swing e incrociarsi al massimo {st.MAX_EMA_CROSSES} volta.")
    add(f"- Filtri del range: BOS (estremo oltre lo swing precedente), ampiezza ≥ {st.MIN_RANGE_ATR} ATR(14), livello "
        "d'ingresso non già toccato prima della conferma.")
    add(f"- Ingresso solo con limite al livello {st.ENTRY_LEVEL} (zona OTE {st.OTE_LOW}–{st.OTE_HIGH}); SL all'origine "
        f"dell'impulso (1.0), TP all'estremo (0.0): R teorico {fmt(m['rr_teorico'])}. Rischio {st.RISK_PER_TRADE:.0%} "
        f"dell'equity, lotti arrotondati per difetto a {st.LOT_STEP}. Scadenza {st.ORDER_EXPIRY_CANDLES} candele 15m "
        "reali. Uscita solo a SL o TP.\n")
    add("## Dati\n")
    add(f"- File trovati/usati: {', '.join(os.path.basename(f) for f in files)} (formati: {', '.join(rep.formats)}).")
    add(f"- Periodo effettivo (UTC): **{rep.first_time} → {rep.last_time}**; periodo di trading (dopo il warm-up di "
        f"{st.WARMUP_CANDLES} candele 15m): {res.start_time} → {res.end_time}. Il 2025 non è nei dati.")
    add(f"- Righe 1m: caricate {fmt(rep.rows_loaded)}, pulite {fmt(rep.rows_clean)}; duplicati identici "
        f"{fmt(rep.exact_duplicates)}, duplicati conflittuali {fmt(rep.conflicting_duplicates)}, sovrapposizioni tra file "
        f"{len(rep.file_overlaps)}; OHLC incoerenti {fmt(rep.ohlc_inconsistent_rows)}; prezzi non positivi "
        f"{fmt(rep.non_positive_rows)}; fuori scala {fmt(rep.out_of_scale_rows)}; salti > {JUMP_ATR_MULT:.0f} ATR "
        f"{len(rep.jumps)} (non scartati); righe con volume zero {fmt(rep.zero_volume_rows)} (normale per HistData).")
    g = rep.gaps
    if g is not None and len(g):
        cnt = g.groupby("categoria")["minuti_mancanti"].agg(["count", "sum"])
        add("- Buchi nei dati 1m: " + "; ".join(f"{c}: {fmt(int(r['count']))} ({fmt(int(r['sum']))} minuti)"
                                               for c, r in cnt.iterrows()) + ".")
    add(f"- Candele 15m: {fmt(rep.candles_15m)}, di cui incomplete {fmt(rep.candles_15m_incomplete)} (costruite con le "
        "candele base disponibili). Le candele 15m del tutto mancanti non vengono alimentate alla strategia: pivot, ATR, "
        "EMA e scadenze si contano sulla sequenza di candele reali.")
    add(f"- Verifica del fuso (riapertura domenicale): {len(rep.weeks) if rep.weeks is not None else 0} settimane, "
        f"non conformi {fmt(100 * rep.week_fail_ratio, 1)}%"
        + (" → **ANOMALIA** (oltre il 20%): conversione NON cambiata automaticamente." if rep.tz_anomaly else " → OK."))
    if g is not None and len(g):
        big = g[(g["categoria"] == GAP_ANOMALOUS) & (g["minuti_mancanti"] > LARGE_GAP_MINUTES)]
        add(f"\n**Buchi anomali > {LARGE_GAP_MINUTES} minuti: {len(big)}**\n")
        if len(big):
            add(df_to_md(big[["ultima_candela_prima_utc", "prima_candela_dopo_utc", "minuti_mancanti"]]))
    add("\n## Risultati principali\n")
    keys = ["profitto_netto_usd", "rendimento_pct", "cagr_pct", "profit_factor", "win_rate_pct",
            "win_rate_pareggio_effettivo_pct", "win_rate_pareggio_teorico_lordo_pct", "numero_trade", "trade_long",
            "trade_short", "expectancy_usd", "expectancy_r", "payoff_ratio", "r_medio_vincenti", "r_medio_perdenti",
            "r_teorico_medio_sul_fill", "max_drawdown_pct", "max_drawdown_usd", "sharpe", "sortino",
            "max_perdite_consecutive", "durata_media_ore", "durata_mediana_ore", "durata_massima_ore",
            "tempo_in_posizione_pct", "costi_totali_usd", "costo_medio_r", "rischio_effettivo_medio_pct",
            "rischio_effettivo_min_pct", "leva_massima", "trade_aperti_nel_weekend", "uscite_su_gap_all_open",
            "fill_all_open_su_gap", "buy_and_hold_xauusd_pct", "trade_fine_dati", "pnl_trade_fine_dati_usd"]
    add("| metrica | valore | con costi doppi |\n|---|---|---|")
    for k in keys:
        if k in m:
            add(f"| {k} | {fmt(m[k])} | {fmt(m2.get(k)) if k in m2 else '-'} |")
    if m["campione_insufficiente"]:
        add(f"\n**Campione insufficiente** ({m['numero_trade']} trade < {MIN_TRADES_SAMPLE}).")
    add("\n## Imbuto range → ordini → fill → trade\n")
    add(f"- Dealing range rilevati (dopo il warm-up): **{m['range_totali']}** (long {m['range_long']}, short {m['range_short']}).")
    add("| esito | numero |\n|---|---|")
    for o in st.RANGE_OUTCOMES:
        add(f"| {o} | {m['range_' + o]} |")
    ema_total = m["range_SCARTATO_EMA"] + m["range_SCARTATO_EMA_INCROCI"] + m["range_SCARTATO_EMA_VICINE"]
    add(f"\n- Il filtro EMA scarta in totale **{ema_total}** range ({fmt(100 * ema_total / max(m['range_totali'], 1), 1)}%): "
        f"direzione {m['range_SCARTATO_EMA']} (long scartati {m['range_long_SCARTATO_EMA']}, short scartati "
        f"{m['range_short_SCARTATO_EMA']}), incroci {m['range_SCARTATO_EMA_INCROCI']}, separazione insufficiente "
        f"{m['range_SCARTATO_EMA_VICINE']}.")
    add(f"- Ordini piazzati {m['ordini_piazzati']}, eseguiti {m['ordini_eseguiti']} (fill rate {fmt(m['fill_rate_pct'], 1)}%). "
        f"Cancellati: NUOVO_ESTREMO {m['ordini_cancellati_NUOVO_ESTREMO']}, SCADUTO {m['ordini_cancellati_SCADUTO']}, "
        f"SOSTITUITO {m['ordini_cancellati_SOSTITUITO']}, TREND_EMA_INVERTITO {m['ordini_cancellati_TREND_EMA_INVERTITO']}. "
        f"Saltati per distanza SL nulla/negativa {m['ordini_saltati_distanza_sl']}, sotto il lotto minimo "
        f"{m['ordini_saltati_sotto_min_lot']}; pendenti a fine dati {m['ordini_pendenti_a_fine_dati']}.")
    add(f"- Tempo medio tra piazzamento e fill: {fmt(m['tempo_medio_piazzamento_fill_ore'])} ore "
        f"({fmt(m['candele_medie_piazzamento_fill'], 1)} candele 15m).")
    add(f"- Trade chiusi: {m['numero_trade']} (TP {m['uscite_tp']}, SL {m['uscite_sl']}), più {m['trade_fine_dati']} "
        "chiusi a fine dati (esclusi dalle statistiche).")
    add("\n## Breakdown\n")
    for key, title in [("anno", "Per anno"), ("trimestre", "Per trimestre"), ("lato", "Long vs short"),
                       ("sessione", "Per sessione di ingresso (orario del fill, UTC: Asia 22-07, Londra 07-13, NY 13-22)")]:
        add(f"### {title}\n")
        add(df_to_md(bd.get(key)))
    add("### Stress test con costi doppi (stessa strategia, stessi parametri)\n")
    add(f"Profitto netto {fmt(m2.get('profitto_netto_usd'))} USD, PF {fmt(m2.get('profit_factor'))}, win rate "
        f"{fmt(m2.get('win_rate_pct'))}%, expectancy {fmt(m2.get('expectancy_r'))} R, max DD {fmt(m2.get('max_drawdown_pct'))}%."
        + (" **Campione insufficiente.**" if m2.get("campione_insufficiente") else ""))
    add(df_to_md(bd2.get("anno")))
    add("\n## Valutazione (regole automatiche)\n")
    L.extend(evaluation(m, m2, bd, res))
    add("\n## Durata dei trade\n")
    add(f"Durata media {fmt(m['durata_media_ore'])} h, mediana {fmt(m['durata_mediana_ore'])} h, massima "
        f"{fmt(m['durata_massima_ore'])} h ({fmt(m['durata_massima_candele_15m'], 0)} candele 15m). Tempo in posizione "
        f"{fmt(m['tempo_in_posizione_pct'], 1)}%. Range ignorati perché c'era già una posizione aperta: "
        f"{m['range_IGNORATO_POSIZIONE_APERTA']} (su {m['range_ORDINE_PIAZZATO'] + m['range_IGNORATO_POSIZIONE_APERTA']} "
        "range validi): è il costo della regola \"nessuna uscita anticipata, una posizione alla volta\".")
    add("\n## Punti deboli, overfitting e validazione\n")
    add("- Parametri fissati a priori (nessuna ottimizzazione), ma la scelta stessa della strategia e dei filtri è stata "
        "fatta conoscendo il comportamento storico dell'oro: il risultato resta in-sample su 2023-2024.")
    add("- Un solo strumento, un solo feed, un solo periodo (probabilmente rialzista per l'oro): il filtro EMA "
        "concentra i trade sul lato del trend dominante.")
    add("- Esecuzione dei limiti simulata su barre 1m bid; spread fisso; swap esclusi.")
    add("- Per validarla servono dati mai visti (per esempio 2025 e oltre, o anni precedenti al 2023) senza cambiare nulla, "
        "e un periodo di paper trading con ordini limite reali sul broker scelto per misurare fill rate, slippage e spread "
        "effettivi.")
    add("\n## Comandi (Windows)\n")
    add("```bat\ncd ote_fib_xauusd_15m\npy -m venv .venv\n.venv\\Scripts\\activate.bat\npip install -r requirements.txt\n"
        "python test_strategy.py\npython backtest.py\n```")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------------------
# Salvataggio
# ---------------------------------------------------------------------------
TRADE_COLUMNS = ["order_id", "placed_time", "fill_time", "exit_time", "side", "limit_price", "fill_price",
                 "fill_at_open", "entry_exec_price", "sl", "tp", "range_high", "range_high_time", "range_low",
                 "range_low_time", "level_618", "level_705", "level_786", "exit_reason", "exit_price", "exit_exec_price",
                 "exit_at_open", "qty_lots", "risk_pct_effective", "leverage", "gross_pnl", "costs", "commission", "pnl",
                 "r", "r_gross", "r_theoretical", "duration_hours", "duration_candles_15m", "candles_to_fill",
                 "hours_to_fill", "session", "over_weekend", "equity_before", "equity_after"]
RANGE_COLUMNS = ["confirm_time", "side", "range_high", "range_high_time", "range_low", "range_low_time", "atr",
                 "amplitude_atr", "ema_fast", "ema_slow", "max_sep", "max_sep_time", "crosses", "swing_candles",
                 "prev_extreme", "level_618", "level_705", "level_786", "touched", "outcome", "order_id"]


def save_outputs(out: str, md: MarketData, rep: DataReport, files: List[str], res: ReplayResult, res2: ReplayResult) -> Dict:
    m = compute_metrics(res)
    m2 = compute_metrics(res2)
    bd = breakdowns(res)
    bd2 = breakdowns(res2)
    rows = [{"metrica": k, "valore": v, "valore_costi_doppi": m2.get(k)} for k, v in m.items()]
    pd.DataFrame(rows).to_csv(os.path.join(out, "metriche.csv"), index=False)
    tr = res.trades
    if len(tr):
        tr[[c for c in TRADE_COLUMNS if c in tr.columns]].to_csv(os.path.join(out, "trade.csv"), index=False)
    else:
        pd.DataFrame(columns=TRADE_COLUMNS).to_csv(os.path.join(out, "trade.csv"), index=False)
    rg = res.ranges
    (rg[[c for c in RANGE_COLUMNS if c in rg.columns]] if len(rg) else pd.DataFrame(columns=RANGE_COLUMNS)).to_csv(
        os.path.join(out, "dealing_range.csv"), index=False)
    res.orders.to_csv(os.path.join(out, "ordini.csv"), index=False)
    pd.DataFrame(res.skipped).to_csv(os.path.join(out, "trade_saltati.csv"), index=False)
    for k, d in bd.items():
        d.to_csv(os.path.join(out, f"breakdown_{k}.csv"), index=False)
    for k, d in bd2.items():
        d.to_csv(os.path.join(out, f"breakdown_{k}_costi_doppi.csv"), index=False)
    if rep.gaps is not None:
        rep.gaps.to_csv(os.path.join(out, "buchi_dati.csv"), index=False)
    if rep.weeks is not None:
        rep.weeks.to_csv(os.path.join(out, "verifica_fuso_settimane.csv"), index=False)
    pd.DataFrame(rep.jumps).to_csv(os.path.join(out, "salti_prezzo.csv"), index=False)
    res.equity.rename("equity_usd").to_csv(os.path.join(out, "equity.csv"), index_label="time_utc")
    plot_equity(res, os.path.join(out, "equity_drawdown.png"))
    if len(tr):
        closed = tr[tr["exit_reason"] != EXIT_END].sort_values("fill_time")
        winners = closed[closed["pnl"] > 0].head(3)
        losers = closed[closed["pnl"] <= 0].head(3)
        for label, sub in [("vincente", winners), ("perdente", losers)]:
            for n_, (_, t) in enumerate(sub.iterrows(), 1):
                title = (f"{SYMBOL} 15m - trade {label} {n_}: {t['side']} {t['order_id']} | fill {t['fill_time']:%Y-%m-%d %H:%M} "
                         f"@ {t['fill_price']:.2f} | uscita {t['exit_reason']} {t['exit_time']:%Y-%m-%d %H:%M} | R {t['r']:.2f}")
                plot_trade(md, t, os.path.join(out, f"esempio_{label}_{n_}.png"), title)
    write_report(os.path.join(out, "report.md"), rep, files, m, m2, res, bd, bd2)
    return m


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest OTE Fibonacci XAUUSD 15m (HistData M1)")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="cartella dei dati (default: %(default)s)")
    p.add_argument("--name", default=DEFAULT_NAME, help="nome del file unico senza estensione (default: %(default)s)")
    p.add_argument("--tz", type=float, default=DEFAULT_TZ,
                   help="scostamento dei dati rispetto a UTC in ore (default %(default)s = EST fisso HistData)")
    p.add_argument("--out", default=DEFAULT_OUT, help="cartella di output (default: output accanto agli script)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    log_path = setup_logging(args.out)
    log.info("Backtest OTE Fibonacci %s 15m — parametri della strategia fissi (strategy.py), nessuna ottimizzazione", SYMBOL)
    log.info("Parametri: %s", {k: getattr(st, k) for k in [
        "PIVOT_LEFT", "PIVOT_RIGHT", "ATR_PERIOD", "MIN_RANGE_ATR", "OTE_LOW", "OTE_HIGH", "ENTRY_LEVEL", "TP_LEVEL",
        "EMA_FAST", "EMA_SLOW", "EMA_SEP_MIN_ATR", "MAX_EMA_CROSSES", "SWING_BUFFER", "ORDER_EXPIRY_CANDLES",
        "USE_BODIES", "RISK_PER_TRADE", "WARMUP_CANDLES", "CONTRACT_SIZE_OZ", "MIN_LOT", "LOT_STEP"]})
    log.info("Costi: spread %.2f USD/oz (metà per lato), slippage %.2f USD/oz per lato, commissione %.1f USD/lotto per lato",
             SPREAD_PER_OZ, SLIPPAGE_PER_OZ, COMMISSION_PER_LOT_SIDE)
    md, rep, files = load_market_data(args.data_dir, args.name, args.tz)
    log.info("=== REPLAY (costi base) ===")
    res = run_replay(md, Costs())
    log.info("=== REPLAY stress test (costi doppi, stessa strategia) ===")
    res2 = run_replay(md, Costs().scaled(2.0), verbose=False)
    lungo = int((res.ranges["outcome"] == st.SCARTATO_SWING_LUNGO).sum()) if len(res.ranges) else 0
    log.info("Range SCARTATO_SWING_LUNGO (swing oltre %d candele): %d", st.SWING_BUFFER, lungo)
    m = save_outputs(args.out, md, rep, files, res, res2)
    log.info("=== RISULTATI ===")
    for k in ["numero_trade", "profitto_netto_usd", "rendimento_pct", "profit_factor", "win_rate_pct",
              "win_rate_pareggio_effettivo_pct", "expectancy_r", "max_drawdown_pct", "sharpe", "range_totali",
              "ordini_piazzati", "ordini_eseguiti", "fill_rate_pct"]:
        log.info("  %s = %s", k, fmt(m.get(k)))
    log.info("Output salvati in %s (log: %s)", os.path.abspath(args.out), log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
