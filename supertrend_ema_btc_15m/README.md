# Supertrend + EMA 50/100 — timeframe 30m, RR 3 (dati attuali: XAUUSD)

Strategia (timeframe **30 minuti**, `TIMEFRAME_MINUTES = 30` in `strategy.py`), nata per BTC 15m e ora testata su XAUUSD, con ingresso sul cambio di trend del Supertrend
(ATR 10, moltiplicatore 3) e filtro di trend EMA 50/100. Uscite solo per stop loss
(linea del Supertrend sulla candela di cambio, fisso) o take profit (**3R**, fisso).
Nota: la cartella mantiene il nome storico `supertrend_ema_btc_15m` per non rompere i percorsi.
Parametri fissi, nessuna ottimizzazione.

## Struttura dei file

```
supertrend_ema_btc_15m/
├── strategy.py        # TUTTA la strategia e nient'altro (identica in backtest e live)
├── backtest.py        # dati, controlli, resample al timeframe, replay, esecuzione simulata, metriche, report
├── test_strategy.py   # test (python test_strategy.py, senza pytest)
├── requirements.txt
├── README.md
└── output/            # creata da backtest.py
    ├── metrics.csv            # metriche (costi base e costi doppi)
    ├── trades.csv             # lista completa dei trade
    ├── trend_changes.csv      # tutti i cambi di trend con esito
    ├── skipped_trades.csv     # trade saltati (distanza SL <= 0)
    ├── data_gaps.csv          # buchi nei dati 1m
    ├── breakdown_*.csv        # per anno, trimestre, lato, sessione
    ├── equity_curve.csv
    ├── equity_drawdown.png    # equity curve e drawdown
    ├── backtest_log.txt       # decisioni, assunzioni, controlli sui dati, risultati
    └── report.md              # report finale generato automaticamente
```

### Separazione strategia / backtest

- `strategy.py` non importa nulla del backtest e non accede a file, rete, exchange, API o orologio.
  Riceve candele **chiuse** del timeframe, una alla volta, e calcola gli indicatori in modo **incrementale**
  (stato interno, nessun ricalcolo sulla storia): per costruzione non vede il futuro.
- Interfaccia: `Candle`, `Position` (o `None` se flat), `Action` (solo `OPEN`), `warmup(candles)`,
  `on_candle_close(candle, position)`, `last_signal` (diagnostica), `size_position(...)`,
  `take_profit(...)`.
- `backtest.py` importa `strategy.py` e lo alimenta candela per candela come farebbe un loop live.
  Nessuna regola di trading è definita nel backtest.

Nota: `size_position(equity, entry, sl, side)` riceve anche il lato: senza di esso un long
con ingresso in gap sotto lo SL sembrerebbe uno short valido e il controllo "distanza dallo SL
nulla o negativa ⇒ trade saltato" non sarebbe possibile.

## Dati

Default: cartella `C:\Users\Admin\Downloads\dati`, file `XAUUSD_M1_2023_2024` (.csv/.txt/.zip/.parquet).
Se non c'è un file unico, vengono uniti in ordine cronologico i file annuali HistData
`DAT_ASCII_<SIMBOLO>_M1_YYYY.csv` (i `.txt` di HistData sono report sui buchi e vengono ignorati),
oppure i file mensili Binance `BTCUSDT-1m-YYYY-MM.csv/.zip`.

Formati riconosciuti automaticamente dal contenuto:
- **HistData.com ASCII M1** (es. XAUUSD): `YYYYMMDD HHMMSS;open;high;low;close;volume`,
  orari in **EST fisso (UTC-5, senza ora legale)** → convertiti in UTC (+5 ore). Volume sempre 0.
- **Binance klines 1m** (12 colonne, timestamp in ms o µs).

Per XAUUSD il mercato non è 24/7: chiusure del weekend e pausa giornaliera (17-18 ora di New York)
sono riconosciute e classificate nel log e in `data_gaps.csv` (colonna `tipo`), non vengono
riempite e le candele mancanti non vengono passate alla strategia. Un trade aperto durante
una chiusura viene verificato sulla prima 1m disponibile (uscita all'open se apre oltre SL o TP).

I dati oltre il 31/12/2025 fanno interrompere il backtest con errore.

## Comandi Windows

### cmd

```bat
cd supertrend_ema_btc_15m
py -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python test_strategy.py
python backtest.py
```

### PowerShell

```powershell
cd supertrend_ema_btc_15m
py -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python test_strategy.py
python backtest.py
```

Opzioni:

```bat
python backtest.py --data-dir "C:\Users\Admin\Downloads\dati" --name XAUUSD_M1_2023_2024 --out output
```

Eseguire i test **prima** del backtest. Il backtest va eseguito **una sola volta**; si rilancia
solo per correggere bug tecnici, dichiarandolo.

## Uso in live

Il connettore live **non è implementato** (nessuna chiave API, nessun ordine reale). Un loop
live deve usare `strategy.py` così com'è, esattamente come fa il backtest:

```python
import strategy as st

strat = st.SupertrendEmaStrategy()

# 1) warm-up: almeno le ultime WARMUP_CANDLES (500) candele 30m CHIUSE, dalla più vecchia
history = exchange.fetch_closed_candles(timeframe="30m", limit=1000)        # Candle(time UTC, o, h, l, c, v)
strat.warmup(history)                                          # nessun segnale qui

# 2) a ogni chiusura di candela 30m (solo candele chiuse, mai quella in formazione)
while True:
    candle = wait_for_next_closed_30m_candle()                 # Candle chiusa
    position = exchange.current_position()                     # Position(...) oppure None
    actions = strat.on_candle_close(candle, position)          # aggiorna SEMPRE gli indicatori

    for action in actions:                                     # al massimo una OPEN, solo se flat
        fill = exchange.market_order(action.side, ...)         # all'apertura della candela successiva
        entry = fill.avg_price                                 # prezzo di ingresso EFFETTIVO
        qty = st.size_position(exchange.equity(), entry, action.sl, action.side)
        if qty <= 0:                                           # distanza SL nulla o negativa: salta
            continue                                           # (in pratica: calcolare qty su un
                                                               #  prezzo stimato prima dell'ordine)
        tp = st.take_profit(action.side, entry, action.sl)
        exchange.place_oco(stop=action.sl, take_profit=tp, qty=qty)   # SL e TP come ordini sull'exchange

    # nessuna uscita discrezionale: la posizione si chiude SOLO per SL o TP (ordini OCO)
```

Note per il live:
- se il loop riparte, rifare il warm-up con lo storico prima di operare;
- le candele 30m vanno allineate come nel backtest (:00 e :30 UTC);
- una candela mancante (exchange fermo) non va inventata: semplicemente non viene passata;
- la quantità in pratica va calcolata prima dell'ordine (su un prezzo stimato) e poi verificata
  sul fill; il TP va ricalcolato sul prezzo effettivo con `take_profit`.

## Dichiarazioni

Le dichiarazioni complete vengono scritte in `output/report.md` in base alla fonte dei dati. Per XAUUSD (HistData):
1. una sola fonte, quotazioni BID di un feed FX/CFD; orari EST fisso convertiti in UTC;
2. mercato non 24/7 (weekend e pausa giornaliera), buchi non riempiti;
3. short possibili su CFD; swap/finanziamento overnight non inclusi;
4. spread non modellato esplicitamente (solo slippage 0,02% per lato); commissione 0,05% per lato
   (valori della specifica originale, mantenuti invariati); i prezzi possono differire dal broker live.
