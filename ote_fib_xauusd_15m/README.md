# OTE Fibonacci XAUUSD — entry M5, trend EMA 50/100 H1

> Il nome della cartella (`ote_fib_xauusd_15m`) è rimasto quello originale per non rompere i percorsi:
> la strategia attuale lavora su **M5** (swing ed entry) con trend dalle **EMA 50/100 H1**.

Strategia **OTE (Optimal Trade Entry)** sull'oro (XAUUSD), multi-timeframe: ordine limite al
livello di Fibonacci **0.705** (dentro la zona OTE 0.618–0.786) dell'ultimo **dealing range causale**
(swing pivot confermati + zigzag) su candele **M5**, con filtro di trend **EMA 50 / EMA 100 su H1** che
decide se cercare solo OTE long o solo OTE short. Le candele H1 le costruisce la strategia stessa
aggregando le M5 chiuse (nessun dato futuro). Stop loss all'origine dell'impulso (1.0), take profit all'estremo (0.0):
R teorico 0.705 / 0.295 ≈ 2,39. Rischio 1% per trade. Parametri fissi, nessuna ottimizzazione.

È la versione causale dell'indicatore TradingView "OTE visible chart" (@twingall), che usa massimo e
minimo del grafico visibile (look-ahead): qui i livelli si calcolano solo con pivot già confermati
(ritardo di 5 candele M5 = 25 minuti).

## Struttura

| File | Contenuto |
|---|---|
| `strategy.py` | **tutta** la strategia e nient'altro: parametri, ATR M5, aggregazione H1 con EMA 50/100 e ATR H1, pivot, zigzag, dealing range e filtri, livelli, limite/SL/TP, scadenza e cancellazioni, sizing. Nessun accesso a file, rete, broker od orologio. Stesso codice in backtest e in live. |
| `backtest.py` | caricamento e controlli dei dati HistData M1, verifica del fuso, buchi, aggregazione M5, replay come un loop live, esecuzione dei limiti e di SL/TP sulle candele 1m, costi, metriche, grafici, report |
| `test_strategy.py` | test (senza pytest): riferimento indipendente per indicatori/pivot/zigzag/range, look-ahead, determinismo, sizing, casi limite, formati HistData |
| `LOG_SVILUPPO.md` | decisioni, assunzioni e stato |
| `requirements.txt` | dipendenze |
| `output/` | risultati del backtest (generati da `python backtest.py`) |

## Comandi (Windows)

Dati attesi in `C:\Users\Admin\Downloads\dati` (file `XAUUSD_M1_2023_2024.csv/.txt/.zip`, oppure i file
annuali/mensili HistData `DAT_ASCII_XAUUSD_M1_*.csv` / `HISTDATA_COM_ASCII_XAUUSD_M1*.zip`).

### cmd

```bat
cd ote_fib_xauusd_15m
py -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python test_strategy.py
python backtest.py
```

### PowerShell

```powershell
cd ote_fib_xauusd_15m
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python test_strategy.py
python backtest.py
```

Se PowerShell blocca l'attivazione dell'ambiente: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Opzioni del backtest:

```bat
python backtest.py --data-dir "C:\Users\Admin\Downloads\dati" --name XAUUSD_M1_2023_2024 --tz -5 --out output
```

- `--tz`: scostamento dei dati rispetto a UTC in ore (default `-5`: HistData usa EST fisso, senza ora legale).
- `--out`: cartella di output (default `output` accanto agli script).

Il backtest va eseguito **una sola volta** per ottenere i risultati (rilanci solo per bug tecnici, da
annotare nel log). Output in `output/`: `report.md`, `metriche.csv`, `trade.csv`, `dealing_range.csv`,
`ordini.csv`, `trade_saltati.csv`, `breakdown_*.csv`, `buchi_dati.csv`, `verifica_fuso_settimane.csv`,
`salti_prezzo.csv`, `equity.csv`, `equity_drawdown.png`, `esempio_vincente_1..3.png`,
`esempio_perdente_1..3.png`, `backtest_log.txt`.

## Regole principali (dettaglio in `strategy.py`)

- **Timeframe**: candele M5 allineate a multipli di 5 minuti UTC; trend su candele H1 allineate all'ora UTC.
  Una H1 è chiusa alla chiusura della M5 delle :55 (o, se manca per un buco, all'arrivo della prima M5 dell'ora dopo).
- **Pivot** 5/5 su M5: swing high se `high[i] > max(5 a sinistra)` e `>= max(5 a destra)` (speculare per i low);
  noto solo alla chiusura di `i+5`. Pivot high e low sulla stessa candela: low→high se close ≥ open, altrimenti high→low.
- **Zigzag** con alternanza obbligatoria (un pivot dello stesso tipo sostituisce l'ultimo solo se più estremo).
- **Dealing range**: ultimi due punti dello zigzag (L→H rialzista, H→L ribassista). Filtri nell'ordine:
  trend EMA H1 alla conferma → swing nel buffer → incroci EMA H1 ≤ 1 → EMA H1 separate ≥ 0,5 ATR(14) H1 nella
  direzione del range in almeno una candela dello swing → BOS → ampiezza ≥ 3 ATR(14) M5 → livello 0.705 non già toccato.
- **Ingresso**: solo limite a 0.705; nuovo range valido con ordine pendente → `CANCEL SOSTITUITO` + `PLACE_LIMIT`;
  con posizione aperta nessuna Action.
- **Cancellazione** (decisa a chiusura M5, effettiva dalla candela successiva): `NUOVO_ESTREMO`,
  `TREND_EMA_INVERTITO`, `SCADUTO` (48 candele M5 reali = 4 ore di mercato).
- **Warm-up**: nessun ordine finché non ci sono almeno 500 candele M5 e 200 candele H1 chiuse.
- **Uscita** solo a SL o TP. La strategia non emette mai chiusure.

## Uso in live

`strategy.py` si usa **così com'è**. Il connettore live NON è implementato (nessuna chiave API, nessun
ordine reale). Pseudocodice di un loop live (per esempio con MetaTrader 5):

```python
import strategy as st

s = st.OteFibStrategy()
storico = carica_ultime_candele_m5_chiuse(n=3000)        # >= 200 H1 chiuse (2400 M5) e >= 500 M5, in UTC
s.warmup(storico)                                       # nessun ordine durante il warm-up

pending = None   # st.PendingOrder creato dall'ultimo PLACE_LIMIT ancora attivo sul broker
position = None  # st.Position se il broker ha una posizione aperta di questa strategia

while True:
    candle = attendi_chiusura_candela_m5()              # costruita dai tick/barre del broker CONVERTITI IN UTC
    position = leggi_posizione_dal_broker()             # None se flat (SL/TP gestiti dal broker)
    pending = pending if ordine_ancora_attivo(pending) else None
    for a in s.on_candle_close(candle, position, pending):
        if a.type == st.CANCEL:
            broker_cancella_ordine(a.id)
            pending = None
        elif a.type == st.PLACE_LIMIT:
            # ordine limite GTC: la scadenza la decide la strategia con CANCEL SCADUTO
            broker_ordine_limite_gtc(a.id, a.side, a.limit_price)
            pending = st.PendingOrder(a.id, a.side, a.limit_price, a.sl, a.tp, candle.time,
                                      0, a.range_high, a.range_low)

# Quantità: sul broker l'ordine limite richiede la quantità al piazzamento, quindi si usa
#   qty = st.size_position(equity, a.limit_price, a.sl, a.side)   (0 -> non piazzare e annotare)
# (il backtest la calcola sul prezzo di fill effettivo: coincidono salvo fill all'open su gap, dove il
# rischio reale risulta minore). Al fill impostare SUBITO SL e TP sulla posizione, lato broker (o inviare
# il limite con SL/TP già agganciati). Nessuna uscita discrezionale: la posizione si chiude solo a SL o TP.
```

Note per il live:
- Converti sempre gli orari del broker in UTC prima di costruire le candele M5 (allineate a multipli di 5 minuti UTC);
  le H1 le costruisce la strategia, non vanno passate a parte;
  le candele mancanti (weekend, pausa) semplicemente non si passano alla strategia.
- Passa a `on_candle_close` solo candele **chiuse**, in ordine cronologico (la strategia rifiuta candele fuori ordine).
- Il contatore `candles_alive` dell'ordine lo gestisce la strategia: dopo un riavvio va ricostruito il warm-up
  (e la strategia ricostruisce il contatore dal valore salvato nel `PendingOrder`).
- I prezzi del broker differiscono da HistData (feed, spread, orari): i risultati del backtest non sono garantiti in live.
