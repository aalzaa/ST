"""
Backtest della strategia definita in strategy.py (Supertrend + filtro RSI).
Il timeframe è definito in strategy.py (TIMEFRAME_MINUTES).

Questo file contiene tutto ciò che NON è logica di trading: caricamento e controllo dei
dati 1m, aggregazione al timeframe della strategia, replay candela per candela (come un loop live),
simulazione dell'esecuzione con costi, metriche, grafici, report e salvataggio.
Le regole di trading stanno solo in strategy.py e qui vengono usate senza duplicarle.

Formati di dati supportati (rilevati automaticamente dal contenuto del file):
- HistData.com "DAT_ASCII" M1 (es. XAUUSD): "YYYYMMDD HHMMSS;open;high;low;close;volume",
  orari in EST fisso (UTC-5, senza ora legale), convertiti in UTC;
- Binance klines 1m (12 colonne, timestamp in ms o µs).

Uso:
    python backtest.py
    python backtest.py --data-dir "C:\\Users\\Admin\\Downloads\\dati" --name XAUUSD_M1_2023_2024 --out output
"""

from __future__ import annotations

import argparse
import glob
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
# Configurazione del backtest (non sono parametri della strategia)
# ---------------------------------------------------------------------------
DEFAULT_DATA_DIR = r"C:\Users\Admin\Downloads\dati"
DEFAULT_NAME = "XAUUSD_M1_2023_2024"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "output")

INITIAL_CAPITAL = 10_000.0
COMMISSION_RATE = 0.0005      # taker 0.05% per lato
SLIPPAGE_RATE = 0.0002        # 0.02% per lato, applicato in modo avverso al prezzo
MAX_ALLOWED_TIME = pd.Timestamp("2025-12-31 23:59:59.999999", tz="UTC")
LARGE_GAP_MINUTES = 60        # buchi da riportare nel report
MIN_TRADES_SAMPLE = 200

# HistData.com: gli orari dei file ASCII sono in EST fisso (UTC-5) tutto l'anno
HISTDATA_UTC_OFFSET_HOURS = 5
HISTDATA_LINE = re.compile(r"^\s*\d{8} \d{6};")
MARKET_TZ = "America/New_York"   # usato solo per classificare le chiusure di mercato

FORMAT_HISTDATA = "histdata"
FORMAT_BINANCE = "binance"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "number_of_trades", "taker_buy_base_volume",
    "taker_buy_quote_volume", "ignore",
]

MINUTE_NS = 60 * 1_000_000_000
TF = st.TIMEFRAME_MINUTES      # timeframe della strategia, in minuti
TF_LABEL = f"{TF // 60}h" if TF % 60 == 0 else f"{TF}m"

# Sessioni di ingresso in UTC (ore intere, [inizio, fine))
SESSIONS = {
    "Asia": [(21, 24), (0, 8)],    # 21:00-07:59 UTC
    "Londra": [(8, 13)],           # 08:00-12:59 UTC
    "NY": [(13, 21)],              # 13:00-20:59 UTC
}

GAP_WEEKEND = "chiusura_weekend"
GAP_DAILY = "pausa_giornaliera"
GAP_OTHER = "altro"

log = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(out_dir: str) -> str:
    """Log sia su console sia su file output/backtest_log.txt."""
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
# Caricamento dati
# ---------------------------------------------------------------------------
def find_data_files(data_dir: str, name: str) -> List[str]:
    """
    Cerca prima un file unico `name`.(csv|txt|parquet|zip); in mancanza, i file annuali
    HistData DAT_ASCII_<SIMBOLO>_M1_YYYY.(csv|zip) oppure i file mensili Binance
    BTCUSDT-1m-YYYY-MM.(csv|zip), ordinati cronologicamente.
    """
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"Cartella dati non trovata: {data_dir}")
    single = []
    for path in sorted(glob.glob(os.path.join(data_dir, name + "*"))):
        base = os.path.basename(path)
        if os.path.isfile(path) and re.fullmatch(re.escape(name) + r"(\.[A-Za-z0-9]+)+", base):
            single.append(path)
    if single:
        # preferenza: parquet > csv > zip > txt
        order = {".parquet": 0, ".csv": 1, ".zip": 2, ".txt": 3}
        single.sort(key=lambda p: order.get(os.path.splitext(p)[1].lower(), 9))
        log.info("File unico trovato: %s (candidati: %s)", single[0], [os.path.basename(p) for p in single])
        return [single[0]]
    patterns = [
        ("annuali HistData", re.compile(r"DAT_ASCII_[A-Z0-9]+_M1_(\d{4})(\d{2})?\.(csv|zip)$", re.IGNORECASE)),
        ("mensili Binance", re.compile(r"BTCUSDT-1m-(\d{4})-(\d{2})\.(csv|zip)$", re.IGNORECASE)),
    ]
    for label, pattern in patterns:
        found: Dict[Tuple[int, int], str] = {}
        for path in glob.glob(os.path.join(data_dir, "*")):
            m = pattern.search(os.path.basename(path))
            if not m:
                continue
            key = (int(m.group(1)), int(m.group(2) or 0))
            # se esistono sia .csv sia .zip dello stesso periodo si preferisce il .csv
            if key not in found or path.lower().endswith(".csv"):
                found[key] = path
        if found:
            files = [found[k] for k in sorted(found)]
            log.info("File unico '%s' non trovato: uso %d file %s (%s ... %s)", name, len(files), label,
                     os.path.basename(files[0]), os.path.basename(files[-1]))
            return files
    raise FileNotFoundError(
        f"Nessun file '{name}.*', né file annuali HistData, né file mensili Binance in {data_dir}. "
        f"File presenti: {sorted(os.listdir(data_dir))}"
    )


def _first_lines(path: str, n: int = 5) -> List[str]:
    """Prime righe non vuote di un file di testo (anche dentro uno zip)."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            member = [x for x in z.namelist() if not x.endswith("/")][0]
            with z.open(member) as f:
                raw = f.read(4096).decode("utf-8", errors="replace")
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(4096)
    return [x for x in raw.splitlines() if x.strip()][:n]


def detect_format(path: str) -> str:
    """Rileva il formato dal contenuto del file, non dal nome."""
    if path.lower().endswith(".parquet"):
        return FORMAT_BINANCE
    lines = _first_lines(path)
    if any(HISTDATA_LINE.match(x) for x in lines):
        return FORMAT_HISTDATA
    return FORMAT_BINANCE


def detect_symbol(files: List[str]) -> str:
    """Simbolo dedotto dal nome del file (solo per etichette e report)."""
    for f in files:
        m = re.search(r"(XAUUSD|XAGUSD|BTCUSDT|BTCUSD|[A-Z]{6})", os.path.basename(f).upper())
        if m:
            return m.group(1)
    return "SCONOSCIUTO"


def read_histdata(path: str, info: Dict) -> pd.DataFrame:
    """
    Legge un file HistData ASCII M1: "YYYYMMDD HHMMSS;open;high;low;close;volume".
    Gli orari sono EST fisso (UTC-5, senza ora legale) e vengono convertiti in UTC.
    """
    raw = pd.read_csv(path, sep=";", header=None, dtype=str, compression="infer")
    ncol = raw.shape[1]
    if ncol < 5:
        raise ValueError(f"{path}: solo {ncol} colonne, attese 6 (data;open;high;low;close;volume)")
    if ncol != 6:
        log.warning("%s: %d colonne invece delle 6 attese; uso le prime 5/6", path, ncol)
    dt = raw.iloc[:, 0].astype(str).str.strip()
    ok = dt.str.fullmatch(r"\d{8} \d{6}")
    n_bad = int((~ok).sum())
    if n_bad:
        log.info("%s: scartate %d righe non conformi (intestazioni/righe spurie), es. %s",
                 os.path.basename(path), n_bad, dt[~ok].unique()[:3].tolist())
    info["header_rows"] += n_bad
    raw = raw[ok]
    dt = dt[ok]
    local = pd.to_datetime(dt, format="%Y%m%d %H%M%S")
    utc = (local + pd.Timedelta(hours=HISTDATA_UTC_OFFSET_HOURS)).dt.tz_localize("UTC").dt.as_unit("ns")
    info["files"].append({"file": os.path.basename(path), "rows": len(raw), "columns": ncol})
    return pd.DataFrame({
        "time_ns": utc.astype("int64").to_numpy(),
        "open": pd.to_numeric(raw.iloc[:, 1], errors="coerce").to_numpy(),
        "high": pd.to_numeric(raw.iloc[:, 2], errors="coerce").to_numpy(),
        "low": pd.to_numeric(raw.iloc[:, 3], errors="coerce").to_numpy(),
        "close": pd.to_numeric(raw.iloc[:, 4], errors="coerce").to_numpy(),
        "volume": (pd.to_numeric(raw.iloc[:, 5], errors="coerce").to_numpy() if ncol >= 6
                   else np.zeros(len(raw))),
        "source_file": os.path.basename(path),
    })


def read_binance_raw(path: str) -> pd.DataFrame:
    """Legge un file klines Binance (csv, zip con csv, parquet) come tabella grezza."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".parquet":
        df = pd.read_parquet(path)
        cols = [str(c).strip().lower() for c in df.columns]
        if "open_time" in cols:
            df.columns = cols
            missing = [c for c in KLINE_COLUMNS[:6] if c not in df.columns]
            if missing:
                raise ValueError(f"{path}: colonne mancanti {missing}")
            keep = [c for c in KLINE_COLUMNS if c in df.columns]
            df = df[keep]
        else:
            log.info("%s: colonne parquet senza nomi standard %s, uso l'ordine posizionale", path, cols)
            df = df.iloc[:, : min(12, df.shape[1])]
            df.columns = KLINE_COLUMNS[: df.shape[1]]
        return df.astype(str)
    return pd.read_csv(path, header=None, dtype=str, compression="infer")


def read_binance(path: str, info: Dict) -> pd.DataFrame:
    """
    Klines Binance: gestisce intestazioni assenti o ripetute, verifica le colonne e converte
    i timestamp (ms o µs, rilevati dal numero di cifre) in UTC.
    """
    raw = read_binance_raw(path)
    ncol = raw.shape[1]
    if ncol < 6:
        raise ValueError(f"{path}: solo {ncol} colonne, ne servono almeno 6 (attese 12)")
    if ncol != 12:
        log.warning("%s: %d colonne invece delle 12 attese; uso le prime 6 (OHLCV)", path, ncol)
    raw = raw.iloc[:, : min(ncol, 12)].copy()
    raw.columns = KLINE_COLUMNS[: raw.shape[1]]
    first = raw["open_time"].astype(str).str.strip()
    is_num = first.str.fullmatch(r"\d+(\.0+)?")
    n_header = int((~is_num).sum())
    if n_header:
        log.info("%s: scartate %d righe non numeriche (intestazioni/righe spurie), es. %s",
                 os.path.basename(path), n_header, first[~is_num].unique()[:3].tolist())
    info["header_rows"] += n_header
    raw = raw[is_num]
    info["files"].append({"file": os.path.basename(path), "rows": len(raw), "columns": ncol})
    ot_int = pd.to_numeric(raw["open_time"], errors="coerce").astype("float64").astype("int64").to_numpy()
    digits = np.floor(np.log10(np.maximum(ot_int.astype("float64"), 1))) + 1
    unit = np.where(digits >= 16, "us", np.where(digits >= 13, "ms", "s"))
    for u, c in pd.Series(unit).value_counts().items():
        info["unit_counts"][u] = info["unit_counts"].get(u, 0) + int(c)
    ns = np.where(unit == "us", ot_int * 1_000, np.where(unit == "ms", ot_int * 1_000_000, ot_int * 1_000_000_000))
    return pd.DataFrame({
        "time_ns": ns.astype("int64"),
        "open": pd.to_numeric(raw["open"], errors="coerce").to_numpy(),
        "high": pd.to_numeric(raw["high"], errors="coerce").to_numpy(),
        "low": pd.to_numeric(raw["low"], errors="coerce").to_numpy(),
        "close": pd.to_numeric(raw["close"], errors="coerce").to_numpy(),
        "volume": pd.to_numeric(raw["volume"], errors="coerce").to_numpy(),
        "source_file": os.path.basename(path),
    })


def load_raw_klines(files: List[str]) -> Tuple[pd.DataFrame, Dict]:
    """Carica e unisce i file (formato rilevato per ciascun file) in una tabella 1m in UTC."""
    info: Dict = {"files": [], "header_rows": 0, "unit_counts": {}, "formats": set()}
    frames = []
    for path in files:
        fmt_ = detect_format(path)
        info["formats"].add(fmt_)
        log.info("Formato di %s: %s", os.path.basename(path), fmt_)
        if fmt_ == FORMAT_HISTDATA:
            frames.append(read_histdata(path, info))
            info["unit_counts"]["EST fisso (UTC-5) -> UTC"] = info["unit_counts"].get(
                "EST fisso (UTC-5) -> UTC", 0) + len(frames[-1])
        else:
            frames.append(read_binance(path, info))
    if len(info["formats"]) > 1:
        raise ValueError(f"File con formati diversi: {info['formats']}")
    info["format"] = info["formats"].pop()
    info["symbol"] = detect_symbol(files)
    df = pd.concat(frames, ignore_index=True)
    return df, info


def classify_gap(start: pd.Timestamp, end: pd.Timestamp, n_missing: int, data_format: str) -> str:
    """
    Classifica un buco come chiusura di mercato prevista o come buco "altro".
    Solo per mercati non 24/7 (dati HistData, es. oro): orari di New York con ora legale.
    - weekend: inizia venerdì/sabato e finisce domenica/lunedì/martedì (festività incluse), >= 24 ore;
    - pausa giornaliera: inizia tra le 16:30 e le 18:00 di New York e dura al massimo 2 ore.
    """
    if data_format != FORMAT_HISTDATA:
        return GAP_OTHER
    s = start.tz_convert(MARKET_TZ)
    e = end.tz_convert(MARKET_TZ)
    if n_missing >= 24 * 60 and s.weekday() in (4, 5) and e.weekday() in (6, 0, 1):
        return GAP_WEEKEND
    minutes_of_day = s.hour * 60 + s.minute
    if n_missing <= 120 and 16 * 60 + 30 <= minutes_of_day <= 18 * 60:
        return GAP_DAILY
    return GAP_OTHER


# ---------------------------------------------------------------------------
# Controlli e pulizia
# ---------------------------------------------------------------------------
@dataclass
class DataReport:
    """Risultati dei controlli sui dati, riportati nel log e nel report."""
    rows_loaded: int = 0
    header_rows: int = 0
    unit_counts: Dict = field(default_factory=dict)
    was_sorted: bool = True
    time_decreases: int = 0
    exact_duplicates: int = 0
    time_duplicates_conflicting: int = 0
    month_overlaps: List[str] = field(default_factory=list)
    nan_rows: int = 0
    non_positive_rows: int = 0
    ohlc_inconsistent_rows: int = 0
    zero_volume_rows: int = 0
    gaps: List[Tuple[pd.Timestamp, pd.Timestamp, int, str]] = field(default_factory=list)
    missing_minutes: int = 0
    missing_minutes_by_type: Dict = field(default_factory=dict)
    gaps_by_type: Dict = field(default_factory=dict)
    data_format: str = FORMAT_BINANCE
    symbol: str = "SCONOSCIUTO"
    first_time: Optional[pd.Timestamp] = None
    last_time: Optional[pd.Timestamp] = None
    rows_clean: int = 0
    has_2025: bool = False
    candles_tf: int = 0
    candles_incomplete: int = 0
    candles_missing: int = 0


def ts(ns: int) -> pd.Timestamp:
    return pd.Timestamp(int(ns), tz="UTC")


def check_and_clean(df: pd.DataFrame, info: Dict) -> Tuple[pd.DataFrame, DataReport]:
    """Esegue tutti i controlli obbligatori, documentandoli nel log, e restituisce dati puliti."""
    rep = DataReport(rows_loaded=len(df), header_rows=info["header_rows"], unit_counts=info["unit_counts"],
                     data_format=info.get("format", FORMAT_BINANCE), symbol=info.get("symbol", "SCONOSCIUTO"))
    log.info("=== CONTROLLI SUI DATI ===")
    log.info("Formato: %s; simbolo: %s", rep.data_format, rep.symbol)
    log.info("Righe caricate: %d (righe di intestazione/spurie scartate: %d)", rep.rows_loaded, rep.header_rows)
    log.info("Unità/fuso dei timestamp (convertiti in UTC): %s", rep.unit_counts)

    # NaN
    nan_mask = df[["open", "high", "low", "close", "volume"]].isna().any(axis=1)
    rep.nan_rows = int(nan_mask.sum())
    log.info("Righe con valori non numerici/mancanti: %d (scartate)", rep.nan_rows)
    df = df[~nan_mask]

    # Ordinamento cronologico (sul file così come letto, nell'ordine dei file)
    t = df["time_ns"].to_numpy()
    dec = int((np.diff(t) < 0).sum())
    rep.time_decreases = dec
    rep.was_sorted = dec == 0
    log.info("Ordinamento cronologico: %s (punti in cui il tempo torna indietro: %d)",
             "OK" if rep.was_sorted else "NON ordinato -> riordino", dec)

    # Sovrapposizioni tra mesi/file
    if df["source_file"].nunique() > 1:
        ranges = df.groupby("source_file", sort=False)["time_ns"].agg(["min", "max"])
        prev_name, prev_max = None, None
        for name, row in ranges.iterrows():
            if prev_max is not None and row["min"] <= prev_max:
                msg = f"{prev_name} (fine {ts(prev_max)}) si sovrappone a {name} (inizio {ts(row['min'])})"
                rep.month_overlaps.append(msg)
            prev_name, prev_max = name, max(row["max"], prev_max if prev_max is not None else row["max"])
    log.info("Sovrapposizioni tra file mensili: %d %s", len(rep.month_overlaps),
             "" if not rep.month_overlaps else rep.month_overlaps[:10])

    df = df.sort_values("time_ns", kind="mergesort").reset_index(drop=True)

    # Duplicati
    value_cols = ["time_ns", "open", "high", "low", "close", "volume"]
    exact_dup = df.duplicated(subset=value_cols, keep="first")
    rep.exact_duplicates = int(exact_dup.sum())
    df = df[~exact_dup]
    time_dup = df.duplicated(subset=["time_ns"], keep="first")
    rep.time_duplicates_conflicting = int(time_dup.sum())
    if rep.time_duplicates_conflicting:
        ex = [str(ts(x)) for x in df.loc[time_dup, "time_ns"].head(5)]
        log.warning("Duplicati con stesso timestamp ma valori diversi: %d (tengo il primo), es. %s",
                    rep.time_duplicates_conflicting, ex)
    df = df[~time_dup]
    log.info("Duplicati identici rimossi: %d; duplicati di timestamp con valori diversi rimossi: %d",
             rep.exact_duplicates, rep.time_duplicates_conflicting)

    # Allineamento al minuto
    misaligned = int((df["time_ns"] % MINUTE_NS != 0).sum())
    if misaligned:
        log.warning("Righe con open_time non allineato al minuto: %d (scartate)", misaligned)
        df = df[df["time_ns"] % MINUTE_NS == 0]

    # Prezzi non positivi
    nonpos = (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    rep.non_positive_rows = int(nonpos.sum())
    if rep.non_positive_rows:
        log.warning("Righe con prezzi non positivi: %d (scartate, diventano minuti mancanti), es. %s",
                    rep.non_positive_rows, [str(ts(x)) for x in df.loc[nonpos, "time_ns"].head(5)])
    else:
        log.info("Righe con prezzi non positivi: 0")
    df = df[~nonpos]

    # Coerenza OHLC
    bad = (df["low"] > df[["open", "close"]].min(axis=1)) | (df["high"] < df[["open", "close"]].max(axis=1)) | (
        df["low"] > df["high"])
    rep.ohlc_inconsistent_rows = int(bad.sum())
    if rep.ohlc_inconsistent_rows:
        log.warning("Righe con OHLC incoerente (low<=open/close<=high violato): %d (scartate), es. %s",
                    rep.ohlc_inconsistent_rows, [str(ts(x)) for x in df.loc[bad, "time_ns"].head(5)])
    else:
        log.info("Coerenza OHLC (low <= open/close <= high): OK su tutte le righe")
    df = df[~bad]

    # Volume zero (tenute: sono minuti reali senza scambi)
    zero_vol = df["volume"] == 0
    rep.zero_volume_rows = int(zero_vol.sum())
    if rep.zero_volume_rows == len(df):
        log.info("Volume zero su TUTTE le righe: la fonte non fornisce volumi (tipico di HistData). "
                 "Il volume non è usato dalla strategia.")
    log.info("Righe con volume zero: %d (mantenute, sono minuti esistenti senza scambi)%s",
             rep.zero_volume_rows,
             "" if not rep.zero_volume_rows else f", es. {[str(ts(x)) for x in df.loc[zero_vol, 'time_ns'].head(5)]}")

    df = df.reset_index(drop=True)
    if df.empty:
        raise ValueError("Nessun dato valido dopo la pulizia")

    rep.first_time = ts(df["time_ns"].iloc[0])
    rep.last_time = ts(df["time_ns"].iloc[-1])

    # Limite temporale obbligatorio
    if rep.last_time > MAX_ALLOWED_TIME:
        raise SystemExit(
            f"ERRORE: la data massima dei dati ({rep.last_time}) supera il limite consentito 2025-12-31. "
            "Rimuovere i dati successivi e rilanciare."
        )
    log.info("Verifica data massima <= 2025-12-31: OK (ultima 1m: %s)", rep.last_time)
    rep.has_2025 = rep.last_time >= pd.Timestamp("2025-01-01", tz="UTC")

    # Minuti mancanti
    t = df["time_ns"].to_numpy()
    d = np.diff(t)
    gap_idx = np.nonzero(d > MINUTE_NS)[0]
    for i in gap_idx:
        start = ts(t[i] + MINUTE_NS)
        end = ts(t[i + 1] - MINUTE_NS)
        n_missing = int(d[i] // MINUTE_NS - 1)
        rep.gaps.append((start, end, n_missing, classify_gap(start, end, n_missing, rep.data_format)))
    rep.missing_minutes = int(sum(g[2] for g in rep.gaps))
    for g in rep.gaps:
        rep.gaps_by_type[g[3]] = rep.gaps_by_type.get(g[3], 0) + 1
        rep.missing_minutes_by_type[g[3]] = rep.missing_minutes_by_type.get(g[3], 0) + g[2]
    rep.rows_clean = len(df)
    expected = int((t[-1] - t[0]) // MINUTE_NS + 1)
    log.info("Periodo coperto: %s -> %s; minuti attesi %d, presenti %d, mancanti %d in %d buchi",
             rep.first_time, rep.last_time, expected, rep.rows_clean, rep.missing_minutes, len(rep.gaps))
    log.info("Buchi per tipo: %s; minuti mancanti per tipo: %s", rep.gaps_by_type, rep.missing_minutes_by_type)
    if rep.data_format == FORMAT_HISTDATA:
        log.info("Mercato non 24/7: chiusure del weekend e pausa giornaliera (17-18 New York) sono attese e non "
                 "vengono riempite; le candele interamente mancanti non sono alimentate alla strategia.")
    for start, end, n, kind in rep.gaps:
        log.info("  Buco: da %s a %s, %d minuti mancanti [%s]", start, end, n, kind)
    log.info("2025 presente nei dati: %s", "SÌ" if rep.has_2025 else "NO (il 2025 non è usato)")
    return df, rep


# ---------------------------------------------------------------------------
# Aggregazione al timeframe della strategia
# ---------------------------------------------------------------------------
@dataclass
class MarketData:
    """Dati 1m (array numpy) e candele del timeframe con l'indice dei loro minuti."""
    m_time: np.ndarray
    m_open: np.ndarray
    m_high: np.ndarray
    m_low: np.ndarray
    m_close: np.ndarray
    candles: List[st.Candle]
    c_start: np.ndarray       # indice della prima 1m della candela
    c_end: np.ndarray         # indice (escluso) dell'ultima 1m
    c_minutes: np.ndarray     # numero di minuti disponibili
    c_time_ns: np.ndarray


def resample_candles(df: pd.DataFrame, rep: Optional[DataReport] = None) -> MarketData:
    """
    Aggrega le 1m in candele di TF minuti allineate a multipli di TF dalla mezzanotte UTC
    (per 2h: 00:00, 02:00, ... UTC) con i soli minuti disponibili. Le candele con meno di
    TF minuti sono marcate incomplete; quelle
    senza alcun minuto non esistono e non vengono alimentate alla strategia.
    """
    t = df["time_ns"].to_numpy(dtype="int64")
    o = df["open"].to_numpy(dtype="float64")
    h = df["high"].to_numpy(dtype="float64")
    lo = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")
    v = df["volume"].to_numpy(dtype="float64")
    bucket = t - (t % (TF * MINUTE_NS))
    starts = np.concatenate(([0], np.nonzero(np.diff(bucket))[0] + 1))
    ends = np.concatenate((starts[1:], [len(t)]))
    c_open = o[starts]
    c_close = c[ends - 1]
    c_high = np.maximum.reduceat(h, starts)
    c_low = np.minimum.reduceat(lo, starts)
    c_vol = np.add.reduceat(v, starts)
    c_time = bucket[starts]
    minutes = ends - starts
    candles = [
        st.Candle(time=ts(c_time[i]).to_pydatetime(), open=float(c_open[i]), high=float(c_high[i]),
                  low=float(c_low[i]), close=float(c_close[i]), volume=float(c_vol[i]))
        for i in range(len(starts))
    ]
    if rep is not None:
        rep.candles_tf = len(candles)
        rep.candles_incomplete = int((minutes < TF).sum())
        expected = int((c_time[-1] - c_time[0]) // (TF * MINUTE_NS) + 1)
        rep.candles_missing = expected - len(candles)
        log.info("Candele %s: %d (incomplete: %d; intere candele mancanti, non alimentate: %d)",
                 TF_LABEL, rep.candles_tf, rep.candles_incomplete, rep.candles_missing)
    return MarketData(t, o, h, lo, c, candles, starts, ends, minutes, c_time)


# ---------------------------------------------------------------------------
# Replay ed esecuzione simulata
# ---------------------------------------------------------------------------
@dataclass
class ReplayResult:
    trades: List[Dict]
    signals: List[Dict]
    skipped: List[Dict]
    equity_time_ns: np.ndarray
    equity: np.ndarray
    final_cash: float
    start_index: int
    max_leverage: float
    discarded_pending: Optional[Dict]
    positions_fed: Optional[List[Optional[st.Position]]] = None
    actions_out: Optional[List[List[st.Action]]] = None


def session_of(hour: int) -> str:
    for name, ranges in SESSIONS.items():
        for a, b in ranges:
            if a <= hour < b:
                return name
    return "?"


def run_replay(md: MarketData, commission: float = COMMISSION_RATE, slippage: float = SLIPPAGE_RATE,
               initial_capital: float = INITIAL_CAPITAL, record_inputs: bool = False) -> ReplayResult:
    """
    Alimenta la strategia candela per candela come un loop live e simula l'esecuzione:
    - ingresso all'open della prima 1m disponibile della candela successiva, con slippage
      avverso e commissione taker;
    - SL/TP verificati sulle 1m a partire dalla 1m di ingresso inclusa; se la stessa 1m tocca
      entrambi si assume lo SL; se una 1m apre già oltre SL o TP (es. dopo un buco) il trade
      si chiude all'open di quella 1m;
    - a fine dati un trade aperto si chiude all'ultimo close 1m ("fine_dati").
    Nessuna regola di trading è definita qui: tutto viene da strategy.py.
    """
    strategy = st.SupertrendRsiStrategy()
    n = len(md.candles)
    warm = min(st.WARMUP_CANDLES, n)
    strategy.warmup(md.candles[:warm])

    cash = initial_capital
    position: Optional[st.Position] = None
    meta: Dict = {}
    pending: Optional[st.Action] = None
    pending_signal: Optional[Dict] = None
    trades: List[Dict] = []
    signals: List[Dict] = []
    skipped: List[Dict] = []
    eq_t = np.empty(n - warm, dtype="int64")
    eq_v = np.empty(n - warm, dtype="float64")
    max_lev = 0.0
    positions_fed: Optional[List] = [None] * n if record_inputs else None
    actions_out: Optional[List] = [[] for _ in range(n)] if record_inputs else None

    m_time, m_open, m_high, m_low, m_close = md.m_time, md.m_open, md.m_high, md.m_low, md.m_close

    def close_position(j: int, raw_price: float, reason: str, exit_time_ns: int, gap_exit: bool) -> None:
        nonlocal cash, position, meta
        pos = position
        sign = 1.0 if pos.side == st.LONG else -1.0
        fill = raw_price * (1.0 - slippage) if pos.side == st.LONG else raw_price * (1.0 + slippage)
        gross = (fill - pos.entry) * pos.qty * sign
        comm_exit = pos.qty * fill * commission
        cash += gross - comm_exit
        net = gross - meta["comm_entry"] - comm_exit
        slip_exit = pos.qty * abs(fill - raw_price)
        entry_dt = ts(meta["entry_time_ns"])
        exit_dt = ts(exit_time_ns)
        trades.append({
            "signal_time": meta["signal_time"],
            "entry_time": entry_dt,
            "exit_time": exit_dt,
            "side": pos.side,
            "entry": pos.entry,
            "sl": pos.sl,
            "tp": pos.tp,
            "exit_price": fill,
            "exit_reason": reason,
            "qty_btc": pos.qty,
            "notional_usd": pos.qty * pos.entry,
            "leverage": meta["leverage"],
            "risk_usd": meta["risk_usd"],
            "pnl_net_usd": net,
            "r_multiple": net / meta["risk_usd"],
            "commission_usd": meta["comm_entry"] + comm_exit,
            "slippage_usd": meta["slip_entry"] + slip_exit,
            "costs_usd": meta["comm_entry"] + comm_exit + meta["slip_entry"] + slip_exit,
            "duration_min": (exit_time_ns - meta["entry_time_ns"]) / MINUTE_NS,
            "rsi_signal": meta["rsi"],
            "st_line_signal": meta["st_line"],
            "equity_before": meta["equity_before"],
            "equity_after": cash,
            "session": session_of(entry_dt.hour),
            "gap_during_trade": meta["gap_during_trade"],
            "exit_at_open_beyond_level": gap_exit,
            "ignored_signals_during_trade": meta["ignored"],
        })
        position = None
        meta = {}

    for i in range(warm, n):
        s, e = int(md.c_start[i]), int(md.c_end[i])
        candle = md.candles[i]

        # 1) esecuzione dell'eventuale OPEN emessa alla chiusura della candela precedente
        if pending is not None:
            raw = m_open[s]
            side = pending.side
            entry = raw * (1.0 + slippage) if side == st.LONG else raw * (1.0 - slippage)
            qty = st.size_position(cash, entry, pending.sl, side)
            if qty <= 0.0:
                skipped.append({"signal_time": pending_signal["time"], "attempt_time": ts(m_time[s]),
                                "side": side, "entry_try": entry, "sl": pending.sl,
                                "reason": "distanza SL nulla o negativa (gap)"})
                pending_signal["execution"] = "SALTATO_DISTANZA_SL"
                log.info("Trade saltato: %s segnale %s, ingresso %.2f vs SL %.2f (distanza <= 0)",
                         side, pending_signal["time"], entry, pending.sl)
            else:
                tp = st.take_profit(side, entry, pending.sl)
                comm_entry = qty * entry * commission
                equity_before = cash
                cash -= comm_entry
                position = st.Position(side=side, entry=entry, sl=pending.sl, tp=tp, qty=qty)
                lev = qty * entry / equity_before
                max_lev = max(max_lev, lev)
                meta = {
                    "entry_time_ns": int(m_time[s]), "entry_idx": s, "comm_entry": comm_entry,
                    "slip_entry": qty * abs(entry - raw), "risk_usd": qty * abs(entry - pending.sl),
                    "equity_before": equity_before, "leverage": lev,
                    "signal_time": pending_signal["time"], "rsi": pending_signal["rsi"],
                    "st_line": pending_signal["st_line"],
                    "gap_during_trade": False, "ignored": 0,
                }
                pending_signal["execution"] = "ESEGUITO"
            pending = None
            pending_signal = None

        # 2) verifica SL/TP sulle 1m di questa candela
        if position is not None:
            pos = position
            for j in range(s, e):
                if j > meta["entry_idx"] and m_time[j] - m_time[j - 1] > MINUTE_NS:
                    meta["gap_during_trade"] = True
                o_, h_, l_ = m_open[j], m_high[j], m_low[j]
                if pos.side == st.LONG:
                    if o_ <= pos.sl:
                        close_position(j, o_, "SL", int(m_time[j]), True)
                    elif o_ >= pos.tp:
                        close_position(j, o_, "TP", int(m_time[j]), True)
                    elif l_ <= pos.sl:          # SL ha la precedenza se toccati entrambi
                        close_position(j, pos.sl, "SL", int(m_time[j]), False)
                    elif h_ >= pos.tp:
                        close_position(j, pos.tp, "TP", int(m_time[j]), False)
                else:
                    if o_ >= pos.sl:
                        close_position(j, o_, "SL", int(m_time[j]), True)
                    elif o_ <= pos.tp:
                        close_position(j, o_, "TP", int(m_time[j]), True)
                    elif h_ >= pos.sl:
                        close_position(j, pos.sl, "SL", int(m_time[j]), False)
                    elif l_ <= pos.tp:
                        close_position(j, pos.tp, "TP", int(m_time[j]), False)
                if position is None:
                    break

        # 3) equity mark-to-market alla chiusura della candela
        mtm = cash
        if position is not None:
            sign = 1.0 if position.side == st.LONG else -1.0
            mtm += (m_close[e - 1] - position.entry) * position.qty * sign
        eq_t[i - warm] = md.c_time_ns[i] + TF * MINUTE_NS
        eq_v[i - warm] = mtm

        # 4) decisione della strategia sulla candela chiusa
        if record_inputs:
            positions_fed[i] = position
        actions = strategy.on_candle_close(candle, position)
        if record_inputs:
            actions_out[i] = list(actions)
        for a in actions:
            if a.type != st.ACTION_OPEN:
                raise RuntimeError("Azione inattesa dalla strategia")
        if strategy.signal_on_last_candle:
            sig = strategy.last_signal
            rec = {"time": pd.Timestamp(sig.time), "direction": sig.direction, "st_line": sig.st_line,
                   "rsi": sig.rsi, "outcome": sig.outcome,
                   "execution": "", "candle_incomplete": bool(md.c_minutes[i] < TF)}
            signals.append(rec)
            if sig.outcome == st.OUTCOME_IGNORED_POSITION and position is not None:
                meta["ignored"] += 1
            if actions:
                pending = actions[0]
                pending_signal = rec

    discarded = None
    if pending is not None:
        pending_signal["execution"] = "NON_ESEGUITO_FINE_DATI"
        discarded = pending_signal
        log.info("Segnale %s del %s non eseguito: fine dei dati", pending.side, pending_signal["time"])
    if position is not None:
        last = len(m_time) - 1
        close_position(last, m_close[last], "fine_dati", int(m_time[last] + MINUTE_NS), False)
        eq_v[-1] = cash

    return ReplayResult(trades, signals, skipped, eq_t, eq_v, cash, warm, max_lev, discarded,
                        positions_fed, actions_out)


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


def drawdown(equity: pd.Series) -> Tuple[float, float]:
    """Max drawdown (% negativa, USD negativa) di una serie di equity."""
    if equity.empty:
        return float("nan"), float("nan")
    peak = equity.cummax()
    dd_usd = equity - peak
    dd_pct = equity / peak - 1.0
    return float(dd_pct.min()), float(dd_usd.min())


def binomial_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) per X ~ Binomiale(n, p), calcolata in modo esatto."""
    if n == 0:
        return float("nan")
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                          + i * math.log(p) + (n - i) * math.log(1 - p))
    return min(1.0, total)


def compute_metrics(res: ReplayResult, initial_capital: float = INITIAL_CAPITAL) -> Dict:
    """Metriche principali. Il trade 'fine_dati' è escluso dalle statistiche dei trade."""
    tr = pd.DataFrame(res.trades)
    eq = pd.Series(res.equity, index=pd.to_datetime(res.equity_time_ns, utc=True))
    closed = tr[tr["exit_reason"] != "fine_dati"] if not tr.empty else tr
    m: Dict = {}
    final_equity = float(res.final_cash)
    m["capitale_iniziale"] = initial_capital
    m["equity_finale"] = final_equity
    m["profitto_netto_usd"] = final_equity - initial_capital
    m["rendimento_pct"] = (final_equity / initial_capital - 1.0) * 100
    if len(eq) > 1:
        start = eq.index[0] - pd.Timedelta(minutes=TF)
        years = (eq.index[-1] - start).total_seconds() / (365.25 * 86400)
    else:
        years = float("nan")
    m["anni_di_trading"] = years
    m["cagr_pct"] = ((final_equity / initial_capital) ** (1 / years) - 1) * 100 if years and years > 0 and final_equity > 0 else float("nan")

    n = len(closed)
    m["numero_trade"] = n
    m["trade_fine_dati_esclusi"] = int((tr["exit_reason"] == "fine_dati").sum()) if not tr.empty else 0
    m["campione_insufficiente"] = n < MIN_TRADES_SAMPLE
    if n:
        pnl = closed["pnl_net_usd"]
        r = closed["r_multiple"]
        wins = closed[pnl > 0]
        losses = closed[pnl < 0]
        m["profit_factor"] = profit_factor(pnl)
        m["win_rate_pct"] = len(wins) / n * 100
        avg_cost_r = float((closed["costs_usd"] / closed["risk_usd"]).mean())
        m["costo_medio_R"] = avg_cost_r
        m["win_rate_pareggio_teorico_senza_costi_pct"] = 1.0 / (st.RR + 1.0) * 100
        m["win_rate_pareggio_con_costi_pct"] = (1.0 + avg_cost_r) / (st.RR + 1.0) * 100
        m["expectancy_usd"] = float(pnl.mean())
        m["expectancy_R"] = float(r.mean())
        m["r_std"] = float(r.std(ddof=1)) if n > 1 else float("nan")
        m["t_stat_media_R"] = float(r.mean() / (r.std(ddof=1) / math.sqrt(n))) if n > 1 and r.std(ddof=1) > 0 else float("nan")
        p_be = min(max(m["win_rate_pareggio_con_costi_pct"] / 100, 1e-9), 1 - 1e-9)
        m["p_value_binomiale_win_rate"] = binomial_sf(len(wins), n, p_be)
        m["vincita_media_usd"] = float(wins["pnl_net_usd"].mean()) if len(wins) else float("nan")
        m["perdita_media_usd"] = float(losses["pnl_net_usd"].mean()) if len(losses) else float("nan")
        m["payoff_ratio"] = (m["vincita_media_usd"] / abs(m["perdita_media_usd"])
                             if len(wins) and len(losses) else float("nan"))
        m["max_perdite_consecutive"] = max_consecutive_losses(pnl)
        m["durata_media_ore"] = float(closed["duration_min"].mean() / 60)
        m["durata_mediana_ore"] = float(closed["duration_min"].median() / 60)
        m["durata_massima_ore"] = float(closed["duration_min"].max() / 60)
        m["trade_TP"] = int((closed["exit_reason"] == "TP").sum())
        m["trade_SL"] = int((closed["exit_reason"] == "SL").sum())
        m["commissioni_totali_usd"] = float(closed["commission_usd"].sum())
        m["slippage_totale_usd"] = float(closed["slippage_usd"].sum())
    dd_pct, dd_usd = drawdown(eq)
    m["max_drawdown_pct"] = dd_pct * 100
    m["max_drawdown_usd"] = dd_usd
    daily = eq.resample("1D").last().ffill()
    rets = daily.pct_change().dropna()
    if len(rets) > 1 and rets.std(ddof=1) > 0:
        m["sharpe_annualizzato"] = float(rets.mean() / rets.std(ddof=1) * math.sqrt(365))
        downside = math.sqrt(float((np.minimum(rets, 0) ** 2).mean()))
        m["sortino_annualizzato"] = float(rets.mean() / downside * math.sqrt(365)) if downside > 0 else float("nan")
    else:
        m["sharpe_annualizzato"] = float("nan")
        m["sortino_annualizzato"] = float("nan")
    if len(eq) > 1 and not tr.empty:
        total_min = (eq.index[-1] - (eq.index[0] - pd.Timedelta(minutes=TF))).total_seconds() / 60
        m["tempo_in_posizione_pct"] = float(tr["duration_min"].sum() / total_min * 100)
    else:
        m["tempo_in_posizione_pct"] = 0.0
    m["leva_massima"] = res.max_leverage
    sig = pd.DataFrame(res.signals)
    m["cambi_trend_totali"] = len(sig)
    for key, label in [(st.OUTCOME_ENTRY, "cambi_trend_entrati"),
                       (st.OUTCOME_REJECTED_FILTER, "cambi_trend_scartati_rsi"),
                       (st.OUTCOME_IGNORED_POSITION, "cambi_trend_ignorati_in_posizione")]:
        m[label] = int((sig["outcome"] == key).sum()) if not sig.empty else 0
    m["trade_saltati_distanza_sl"] = len(res.skipped)
    return m


def group_stats(trades: pd.DataFrame, key: pd.Series, eq: pd.Series, eq_key: Optional[pd.Series]) -> pd.DataFrame:
    """Profitto, profit factor, drawdown e numero trade per gruppo."""
    rows = []
    for g, sub in trades.groupby(key, sort=True):
        row = {
            "gruppo": str(g),
            "numero_trade": len(sub),
            "profitto_usd": sub["pnl_net_usd"].sum(),
            "profit_factor": profit_factor(sub["pnl_net_usd"]),
            "win_rate_pct": (sub["pnl_net_usd"] > 0).mean() * 100,
            "expectancy_R": sub["r_multiple"].mean(),
        }
        if eq_key is not None:
            seg = eq[eq_key.astype(str) == str(g)]
            row["max_drawdown_pct"] = drawdown(seg)[0] * 100
        rows.append(row)
    return pd.DataFrame(rows)


def breakdowns(res: ReplayResult) -> Dict[str, pd.DataFrame]:
    tr = pd.DataFrame(res.trades)
    out: Dict[str, pd.DataFrame] = {}
    if tr.empty:
        return out
    closed = tr[tr["exit_reason"] != "fine_dati"].copy()
    if closed.empty:
        return out
    eq = pd.Series(res.equity, index=pd.to_datetime(res.equity_time_ns, utc=True))
    eq_year = pd.Series(eq.index.year, index=eq.index)
    eq_q = pd.Series(eq.index.tz_convert(None).to_period("Q").astype(str), index=eq.index)
    et = closed["entry_time"]
    out["anno"] = group_stats(closed, et.dt.year, eq, eq_year)
    out["trimestre"] = group_stats(closed, et.dt.tz_convert(None).dt.to_period("Q").astype(str), eq, eq_q)
    out["lato"] = group_stats(closed, closed["side"], eq, None)
    out["sessione"] = group_stats(closed, closed["session"], eq, None)
    return out


# ---------------------------------------------------------------------------
# Grafico
# ---------------------------------------------------------------------------
def plot_equity(res: ReplayResult, path: str, symbol: str = "") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    eq = pd.Series(res.equity, index=pd.to_datetime(res.equity_time_ns, utc=True).tz_convert(None))
    dd = (eq / eq.cummax() - 1.0) * 100
    ink, muted, grid, line = "#1f2328", "#59636e", "#e6e8eb", "#2a6fdb"
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.3]})
    ax1.plot(eq.index, eq.values, color=line, lw=1.6)
    ax1.axhline(INITIAL_CAPITAL, color=muted, lw=0.8, ls="--")
    ax1.set_title(f"Equity (mark-to-market a ogni chiusura {TF_LABEL}, USD)", loc="left", color=ink, fontsize=11)
    ax2.fill_between(dd.index, dd.values, 0, color="#c2410c", alpha=0.35, lw=0)
    ax2.plot(dd.index, dd.values, color="#c2410c", lw=1.0)
    ax2.set_title("Drawdown (%)", loc="left", color=ink, fontsize=11)
    for ax in (ax1, ax2):
        ax.grid(True, color=grid, lw=0.6)
        ax.tick_params(colors=muted, labelsize=9)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(grid)
    fig.suptitle(f"Supertrend + RSI {st.RSI_PERIOD} - {symbol} {TF_LABEL}", x=0.01, ha="left", color=ink, fontsize=13)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def fmt(x, nd=2) -> str:
    if isinstance(x, (bool, np.bool_)):
        return "sì" if x else "no"
    if isinstance(x, (int, np.integer)):
        return f"{x:,}".replace(",", ".")
    if isinstance(x, float):
        if math.isnan(x):
            return "n/d"
        if math.isinf(x):
            return "inf"
        return f"{x:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(x)


def df_to_md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for i in range(len(df)):
        lines.append("| " + " | ".join(fmt(df[c].iloc[i]) for c in cols) + " |")
    return "\n".join(lines)


def robustness(res: ReplayResult, m: Dict, m2: Dict, bd: Dict[str, pd.DataFrame]) -> List[str]:
    """Diagnostica automatica e prudente sulla solidità del risultato."""
    tr = pd.DataFrame(res.trades)
    notes: List[str] = []
    if tr.empty:
        return ["Nessun trade: impossibile valutare la strategia."]
    closed = tr[tr["exit_reason"] != "fine_dati"]
    n = len(closed)
    total = closed["pnl_net_usd"].sum()
    if n < MIN_TRADES_SAMPLE:
        notes.append(f"**Campione insufficiente**: {n} trade (< {MIN_TRADES_SAMPLE}). Qualunque conclusione è fragile.")
    top = closed["pnl_net_usd"].sort_values(ascending=False)
    k = max(1, int(round(n * 0.05)))
    without_top = total - top.head(k).sum()
    notes.append(f"Profitto totale {fmt(total)} USD; escludendo i migliori {k} trade (5%) diventa {fmt(without_top)} USD"
                 + (" -> il risultato dipende da pochi trade." if total > 0 and without_top <= 0 else "."))
    tstat = m.get("t_stat_media_R", float("nan"))
    pval = m.get("p_value_binomiale_win_rate", float("nan"))
    notes.append(f"Media R = {fmt(m.get('expectancy_R', float('nan')), 3)}, t-stat = {fmt(tstat)} "
                 f"(|t| < 2 => non distinguibile da zero). Win rate {fmt(m.get('win_rate_pct', float('nan')))}% contro "
                 f"pareggio con costi {fmt(m.get('win_rate_pareggio_con_costi_pct', float('nan')))}%: p-value binomiale "
                 f"(win rate > pareggio per caso) = {fmt(pval, 4)}.")
    for name in ("anno", "trimestre", "lato", "sessione"):
        if name in bd and not bd[name].empty:
            df = bd[name]
            pos = int((df["profitto_usd"] > 0).sum())
            notes.append(f"Per {name}: {pos}/{len(df)} gruppi in profitto; migliore {df.loc[df['profitto_usd'].idxmax(), 'gruppo']} "
                         f"({fmt(df['profitto_usd'].max())} USD), peggiore {df.loc[df['profitto_usd'].idxmin(), 'gruppo']} "
                         f"({fmt(df['profitto_usd'].min())} USD).")
            if total > 0 and df["profitto_usd"].max() >= total and pos < len(df):
                notes.append(f"  -> Il profitto complessivo è interamente spiegato da un solo gruppo ({name}).")
    notes.append(f"Con costi doppi: profitto netto {fmt(m2['profitto_netto_usd'])} USD, profit factor "
                 f"{fmt(m2.get('profit_factor', float('nan')))}, expectancy {fmt(m2.get('expectancy_R', float('nan')), 3)} R"
                 + (" -> il vantaggio sparisce con costi doppi." if m["profitto_netto_usd"] > 0 >= m2["profitto_netto_usd"] else "."))
    notes.append(f"Costo medio per trade = {fmt(m.get('costo_medio_R', float('nan')), 3)} R: con stop stretti i costi "
                 "pesano molto sul rischio per trade.")
    return notes


def declarations(rep: DataReport) -> List[str]:
    """Dichiarazioni obbligatorie, dipendenti dalla fonte dei dati."""
    cost = (f"Costi: commissione {COMMISSION_RATE * 100:.2f}% per lato e slippage {SLIPPAGE_RATE * 100:.2f}% per lato "
            "(applicato anche al TP).")
    both = "Se nella stessa candela 1m sono toccati sia SL sia TP si assume lo **SL** (ipotesi conservativa)."
    if rep.data_format == FORMAT_HISTDATA:
        return [
            f"1. Lo strumento è **{rep.symbol}** (oro contro dollaro USA per XAUUSD), dati M1 **HistData.com**: "
            "una sola fonte, quotazioni **BID** di un feed FX/CFD, non un mercato regolamentato centralizzato.",
            "2. Gli orari dei file HistData sono in **EST fisso (UTC-5, senza ora legale)** e sono stati convertiti in "
            "UTC aggiungendo 5 ore.",
            "3. Il mercato **non è 24/7**: chiusura nel weekend e pausa giornaliera (17-18 ora di New York). I buchi non "
            "sono stati riempiti: le candele mancanti non sono alimentate alla strategia e i trade aperti durante "
            "una chiusura vengono verificati sulla prima 1m disponibile (uscita all'open se apre oltre SL o TP).",
            "4. Su un CFD XAUUSD gli short sono possibili; **swap/finanziamento overnight non inclusi**.",
            "5. Lo spread non è modellato esplicitamente (dati BID; gli acquisti in live avvengono all'ASK): lo slippage "
            "lo approssima solo in parte. I prezzi possono differire da quelli del broker/CFD usato in live.",
            f"6. {cost} Sono i valori della specifica originale (pensati per BTC), mantenuti invariati; su un CFD oro il "
            "costo reale è tipicamente lo spread più un'eventuale commissione.",
            "7. Il volume nei dati HistData è sempre 0 (la fonte non lo fornisce); la strategia non usa il volume.",
            f"8. {both}",
        ]
    return [
        "1. Il simbolo è **BTCUSDT** (quotato in USDT, non BTCUSD), mercato **SPOT di un solo exchange** (Binance).",
        "2. Su spot non si può andare short: gli **short sono SIMULATI** come se fossero su un perpetual o CFD, "
        "**senza funding e senza costi di prestito**: i risultati degli short sono teorici.",
        "3. I prezzi possono differire leggermente da quelli del perpetual o del CFD su cui verrebbe eseguita la "
        "strategia in live.",
        f"4. Funding non incluso. {cost}",
        f"5. {both}",
    ]


def write_report(path: str, rep: DataReport, files: List[str], m: Dict, m2: Dict, res: ReplayResult,
                 bd: Dict[str, pd.DataFrame], notes: List[str]) -> None:
    tr = pd.DataFrame(res.trades)
    L: List[str] = []
    L.append(f"# Report backtest - Supertrend + filtro RSI su {rep.symbol} {TF_LABEL}\n")
    L.append("*Report generato automaticamente da `backtest.py`. Backtest eseguito una sola volta, parametri fissi, "
             "nessuna ottimizzazione.*\n")
    L.append("## Dichiarazioni obbligatorie\n")
    for line in declarations(rep):
        L.append(line)
    L.append("")

    L.append("## Strategia\n")
    L.append(f"- Supertrend ATR {st.ST_ATR_PERIOD} (RMA di Wilder), moltiplicatore {st.ST_MULTIPLIER}; filtro RSI "
             f"{st.RSI_PERIOD} (Wilder) sulla chiusura con soglia {st.RSI_THRESHOLD:g}; seed SMA per ATR e RSI; warm-up {st.WARMUP_CANDLES} candele {TF_LABEL}; timeframe {TF_LABEL} (candele allineate a multipli di {TF_LABEL} dalla mezzanotte UTC).")
    L.append(f"- LONG: Supertrend passa a rialzo e RSI > {st.RSI_THRESHOLD:g}; SHORT: passa a ribasso e RSI < {st.RSI_THRESHOLD:g}; "
             "valutato alla chiusura della candela di cambio, ingresso all'open della candela successiva.")
    L.append(f"- SL fisso sulla linea del Supertrend della candela di cambio; TP fisso a {st.RR:g}R dal prezzo di "
             f"ingresso effettivo; rischio {st.RISK_PER_TRADE * 100:.0f}% dell'equity corrente; una posizione alla volta; "
             "uscite solo per SL/TP.\n")

    L.append("## Dati\n")
    L.append(f"- File usati: {', '.join(os.path.basename(f) for f in files)}")
    L.append(f"- Periodo effettivo: **{rep.first_time} -> {rep.last_time}** (UTC)")
    L.append(f"- 2025: {'presente e usato (entro il 31/12/2025)' if rep.has_2025 else '**non presente, quindi non usato**'}")
    L.append(f"- Righe 1m valide: {fmt(rep.rows_clean)}; minuti mancanti: {fmt(rep.missing_minutes)} in {fmt(len(rep.gaps))} buchi")
    L.append(f"- Duplicati rimossi: {fmt(rep.exact_duplicates + rep.time_duplicates_conflicting)}; righe OHLC incoerenti: "
             f"{fmt(rep.ohlc_inconsistent_rows)}; prezzi non positivi: {fmt(rep.non_positive_rows)}; volume zero: "
             f"{fmt(rep.zero_volume_rows)}; sovrapposizioni tra mesi: {fmt(len(rep.month_overlaps))}")
    L.append(f"- Candele {TF_LABEL}: {fmt(rep.candles_tf)} (incomplete: {fmt(rep.candles_incomplete)}; intere candele mancanti: "
             f"{fmt(rep.candles_missing)})")
    L.append(f"- Buchi per tipo: {rep.gaps_by_type}; minuti mancanti per tipo: {rep.missing_minutes_by_type}")
    big = [g for g in rep.gaps if g[2] > LARGE_GAP_MINUTES]
    big_other = [g for g in big if g[3] == GAP_OTHER]
    big_expected = [g for g in big if g[3] != GAP_OTHER]
    L.append(f"- Buchi superiori a {LARGE_GAP_MINUTES} minuti: {len(big)} (chiusure di mercato previste: "
             f"{len(big_expected)}; altri, es. festività o dati mancanti: {len(big_other)})")
    L.append(f"- Buchi > {LARGE_GAP_MINUTES} minuti NON spiegati da weekend/pausa giornaliera:")
    for s_, e_, n_, k_ in big_other:
        L.append(f"  - {s_} -> {e_}: {n_} minuti ({n_ / 60:.1f} ore)")
    if big_expected:
        L.append(f"- Chiusure previste > {LARGE_GAP_MINUTES} minuti (elenco completo in `data_gaps.csv`):")
        for s_, e_, n_, k_ in big_expected:
            L.append(f"  - {s_} -> {e_}: {n_ / 60:.1f} ore [{k_}]")
    if not tr.empty:
        L.append(f"- Trade rimasti aperti durante un buco o una chiusura: {int(tr['gap_during_trade'].sum())}; "
                 f"chiusi all'open oltre SL/TP (gap): {int(tr['exit_at_open_beyond_level'].sum())}")
    L.append(f"- Buy & hold del sottostante nel periodo di trading: {fmt(m.get('buy_and_hold_pct', float('nan')))}%")
    L.append("")

    L.append("## Risultati principali\n")
    keys = ["profitto_netto_usd", "rendimento_pct", "cagr_pct", "profit_factor", "numero_trade", "win_rate_pct",
            "win_rate_pareggio_teorico_senza_costi_pct", "win_rate_pareggio_con_costi_pct", "expectancy_usd",
            "expectancy_R", "payoff_ratio", "max_drawdown_pct", "max_drawdown_usd", "sharpe_annualizzato",
            "sortino_annualizzato", "max_perdite_consecutive", "durata_media_ore", "durata_mediana_ore",
            "durata_massima_ore", "tempo_in_posizione_pct", "leva_massima", "costo_medio_R"]
    L.append("| metrica | costi base | costi doppi |")
    L.append("|---|---|---|")
    for k in keys:
        L.append(f"| {k} | {fmt(m.get(k, float('nan')))} | {fmt(m2.get(k, float('nan')))} |")
    L.append("")
    if m.get("campione_insufficiente"):
        L.append(f"> **CAMPIONE INSUFFICIENTE**: {m['numero_trade']} trade (< {MIN_TRADES_SAMPLE}).\n")
    if m["trade_fine_dati_esclusi"]:
        fd = tr[tr["exit_reason"] == "fine_dati"].iloc[0]
        L.append(f"Trade chiuso a fine dati (escluso dalle statistiche, incluso nell'equity finale): {fd['side']} "
                 f"ingresso {fd['entry_time']}, P&L {fmt(fd['pnl_net_usd'])} USD.\n")

    L.append("## Cambi di trend del Supertrend (dopo il warm-up)\n")
    L.append(f"- Totali: {m['cambi_trend_totali']}")
    L.append(f"- Entrati: {m['cambi_trend_entrati']} (di cui saltati per distanza SL <= 0: {m['trade_saltati_distanza_sl']})")
    L.append(f"- Scartati dal filtro RSI: {m['cambi_trend_scartati_rsi']}")
    L.append(f"- Ignorati perché già in posizione: {m['cambi_trend_ignorati_in_posizione']}\n")

    for name, title in [("anno", "Per anno"), ("trimestre", "Per trimestre"), ("lato", "Long vs short"),
                        ("sessione", "Per sessione di ingresso (Asia 21-08, Londra 08-13, NY 13-21 UTC)")]:
        if name in bd:
            L.append(f"## {title}\n")
            L.append(df_to_md(bd[name]))
            L.append("")

    L.append("## Durata dei trade\n")
    if not tr.empty:
        closed = tr[tr["exit_reason"] != "fine_dati"]
        longest = closed.sort_values("duration_min", ascending=False).head(5)
        L.append(f"- Trade oltre 24 ore: {int((closed['duration_min'] > 1440).sum())}; oltre 72 ore: "
                 f"{int((closed['duration_min'] > 4320).sum())}")
        L.append(f"- Segnali ignorati perché in posizione: {m['cambi_trend_ignorati_in_posizione']} "
                 f"(media {fmt(closed['ignored_signals_during_trade'].mean())} per trade)")
        L.append("- I 5 trade più lunghi:")
        for _, r in longest.iterrows():
            L.append(f"  - {r['side']} {r['entry_time']} -> {r['exit_time']} ({r['duration_min'] / 60:.1f} h), "
                     f"{r['exit_reason']}, segnali ignorati {r['ignored_signals_during_trade']}")
        L.append("")

    L.append("## Valutazione automatica di robustezza\n")
    for n_ in notes:
        L.append(f"- {n_}")
    L.append("")
    L.append("## Punti deboli e rischi\n")
    L.append(f"- Un solo strumento ({rep.symbol}), una sola fonte dati, un solo periodo "
             f"({rep.first_time.year}-{rep.last_time.year}); buy & hold nel periodo = "
             f"{fmt(m.get('buy_and_hold_pct', float('nan')))}%. In un mercato prevalentemente rialzista il filtro RSI "
             "(momentum) favorisce i long, quindi il risultato può riflettere il regime più che un vantaggio della regola.")
    if rep.data_format == FORMAT_HISTDATA:
        L.append("- Swap/finanziamento overnight non inclusi; i trade che restano aperti più giorni li pagherebbero in live.")
        L.append("- Gap di riapertura (lunedì, dopo festività): lo SL non protegge al suo livello; il backtest chiude "
                 "all'open, ma con dati BID e senza spread reale l'uscita in live può essere peggiore.")
    else:
        L.append("- Short teorici (spot), senza funding: in live su perpetual il funding può essere significativo.")
    L.append("- Stop sulla linea del Supertrend: distanze talvolta strette, quindi costi elevati in R e leva implicita alta.")
    L.append("- Anche senza ottimizzazione, la scelta dei parametri (10/3, 50/100, RR 2) è stata fatta a priori da "
             "conoscenze diffuse: c'è comunque un rischio di selezione implicita.")
    L.append("- Simulazione su 1m: l'ordine degli eventi dentro la stessa 1m non è noto (si assume lo SL).\n")
    L.append("## Come validarla\n")
    L.append("- Eseguire lo stesso codice, senza modifiche, su dati mai visti (es. 2025 e successivi) dopo aver congelato la versione.")
    L.append("- Paper trading sul perpetual/CFD reale per alcuni mesi, confrontando fill, slippage e funding con il backtest.")
    L.append("- Verificare la stabilità su altri exchange/strumenti come controllo (non per scegliere parametri).\n")
    L.append("## Comandi Windows\n")
    L.append("```bat\ncd supertrend_ema_btc_15m\npy -m venv .venv\n.venv\\Scripts\\activate\n"
             "pip install -r requirements.txt\npython test_strategy.py\npython backtest.py\n```")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Backtest Supertrend + filtro RSI, timeframe {TF_LABEL}")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="cartella dei dati 1m (HistData o Binance)")
    p.add_argument("--name", default=DEFAULT_NAME, help="nome del file unico (senza estensione)")
    p.add_argument("--out", default=DEFAULT_OUT, help="cartella di output")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    log_path = setup_logging(args.out)
    log.info("=== BACKTEST Supertrend + filtro RSI (%s) ===", TF_LABEL)
    log.info("Parametri strategia (fissi): ATR=%d, mult=%.1f, RSI=%d soglia %.0f, RR=%.1f, rischio=%.2f%%, warm-up=%d",
             st.ST_ATR_PERIOD, st.ST_MULTIPLIER, st.RSI_PERIOD, st.RSI_THRESHOLD, st.RR, st.RISK_PER_TRADE * 100,
             st.WARMUP_CANDLES)
    log.info("Esecuzione: capitale %.0f, commissione %.3f%%/lato, slippage %.3f%%/lato, funding/swap non inclusi",
             INITIAL_CAPITAL, COMMISSION_RATE * 100, SLIPPAGE_RATE * 100)
    log.info("Assunzioni: ingresso all'open della prima 1m disponibile della candela successiva; SL/TP su 1m dalla "
             "1m di ingresso inclusa; SL e TP toccati nella stessa 1m => SL; 1m che apre oltre SL/TP (es. dopo un buco) "
             "=> uscita all'open; slippage avverso applicato a ingresso, SL, TP e chiusura fine dati; commissione taker "
             "su entrambi i lati; sizing sull'equity realizzata al momento dell'ingresso, costi esclusi dal rischio; "
             f"candele {TF_LABEL} incomplete alimentate normalmente; candele {TF_LABEL} interamente mancanti non alimentate.")
    log.info("Sessioni (UTC, ora di ingresso): Asia 21:00-07:59, Londra 08:00-12:59, NY 13:00-20:59")

    files = find_data_files(args.data_dir, args.name)
    raw, info = load_raw_klines(files)
    for f in info["files"]:
        log.info("Letto %s: %d righe, %d colonne", f["file"], f["rows"], f["columns"])
    df, rep = check_and_clean(raw, info)
    md = resample_candles(df, rep)
    if len(md.candles) <= st.WARMUP_CANDLES:
        raise SystemExit("Dati insufficienti: servono più di WARMUP_CANDLES candele")
    log.info("Warm-up: prime %d candele %s (fino a %s) senza segnali", st.WARMUP_CANDLES, TF_LABEL,
             md.candles[st.WARMUP_CANDLES - 1].time)

    log.info("=== REPLAY (costi base) ===")
    res = run_replay(md)
    log.info("=== REPLAY STRESS TEST (costi doppi, stessi parametri) ===")
    res2 = run_replay(md, commission=2 * COMMISSION_RATE, slippage=2 * SLIPPAGE_RATE)

    m = compute_metrics(res)
    m2 = compute_metrics(res2)
    # rendimento del sottostante nello stesso periodo di trading (riferimento per il regime di mercato)
    bh = (md.m_close[-1] / md.candles[st.WARMUP_CANDLES].open - 1.0) * 100
    m["buy_and_hold_pct"] = m2["buy_and_hold_pct"] = float(bh)
    log.info("Buy & hold del sottostante nel periodo di trading: %.2f%%", bh)
    bd = breakdowns(res)

    out = args.out
    pd.DataFrame({"metrica": list(m.keys()), "costi_base": list(m.values()),
                  "costi_doppi": [m2.get(k) for k in m.keys()]}).to_csv(
        os.path.join(out, "metrics.csv"), index=False)
    pd.DataFrame(res.trades).to_csv(os.path.join(out, "trades.csv"), index=False)
    pd.DataFrame(res.signals).to_csv(os.path.join(out, "trend_changes.csv"), index=False)
    pd.DataFrame(res.skipped).to_csv(os.path.join(out, "skipped_trades.csv"), index=False)
    pd.DataFrame([{"inizio": a, "fine": b, "minuti_mancanti": c, "tipo": k} for a, b, c, k in rep.gaps]).to_csv(
        os.path.join(out, "data_gaps.csv"), index=False)
    for name, d in bd.items():
        d.to_csv(os.path.join(out, f"breakdown_{name}.csv"), index=False)
    pd.DataFrame({"time": pd.to_datetime(res.equity_time_ns, utc=True), "equity": res.equity}).to_csv(
        os.path.join(out, "equity_curve.csv"), index=False)
    plot_equity(res, os.path.join(out, "equity_drawdown.png"), rep.symbol)
    notes = robustness(res, m, m2, bd)
    write_report(os.path.join(out, "report.md"), rep, files, m, m2, res, bd, notes)

    log.info("=== RISULTATI (costi base) ===")
    for k, v in m.items():
        log.info("  %s: %s", k, fmt(v) if not isinstance(v, str) else v)
    log.info("=== RISULTATI (costi doppi) ===")
    for k in ("profitto_netto_usd", "profit_factor", "expectancy_R", "max_drawdown_pct"):
        log.info("  %s: %s", k, fmt(m2.get(k, float("nan"))))
    if m.get("campione_insufficiente"):
        log.warning("CAMPIONE INSUFFICIENTE: %d trade (< %d)", m["numero_trade"], MIN_TRADES_SAMPLE)
    for n_ in notes:
        log.info("Robustezza: %s", n_)
    log.info("Output salvati in %s (log: %s)", out, log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
