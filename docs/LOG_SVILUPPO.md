# Log di sviluppo — decisioni e assunzioni

## Stato

- Codice, test e documentazione: **completi**.
- Test (`python test_strategy.py`): **13/13 superati** nell'ambiente di sviluppo (Python 3.13,
  pandas 3.0, numpy 2.5, matplotlib 3.11).
- **Backtest sui dati reali: NON ancora eseguito.** I dati (`C:\Users\Admin\Downloads\dati backtest`)
  si trovano sul PC dell'utente e l'ambiente di sviluppo cloud non aveva accesso né a quella cartella
  né a data.binance.vision. Il backtest va lanciato una sola volta sul PC con `python backtest.py`;
  il report finale con i risultati è generato automaticamente in `output/report.md`.
- La pipeline completa è stata provata solo su **dati sintetici** (random walk in formato Binance:
  file mensili csv, zip e con intestazione, file unico con intestazioni ripetute, parquet, un buco di
  90 minuti, una riga con volume zero) per verificare che funzioni. Quei numeri non hanno alcun
  significato e non sono stati salvati nel repository.

## Decisioni e assunzioni (prese in autonomia)

### Strategia (`strategy.py`)
- Formule del Supertrend implementate alla lettera; "prev superTrend == prev upperBand" implementato
  con la direzione della candela precedente (isDownTrend), come da specifica.
- Prima dell'ATR la direzione è isDownTrend; sulla prima candela con ATR bande = bande base e
  direzione isDownTrend: quindi nessun "cambio" artificiale all'avvio.
- Seed: ATR = SMA dei primi 10 TR (disponibile dalla 10ª candela); EMA = SMA dei primi N close.
- Segnali solo dalla candela 501 in poi (le prime 500 candele 15m sono warm-up), sia con `warmup()`
  sia chiamando direttamente `on_candle_close()`.
- `size_position(equity, entry, sl, side)`: aggiunto il parametro `side`, indispensabile per
  riconoscere un ingresso in gap oltre lo SL (distanza negativa ⇒ quantità 0 ⇒ trade saltato).
- Rischio 1% calcolato sulla distanza ingresso effettivo–SL; i costi non sono inclusi nel rischio
  (una perdita a SL vale quindi poco più di 1R).
- La strategia rifiuta candele non in ordine cronologico (errore). Le candele 15m mancanti
  semplicemente non vengono passate.
- `last_signal` resta sull'ultimo cambio valutato; `signal_on_last_candle` indica se l'ultima candela
  ne ha prodotto uno (così il backtest registra i segnali senza duplicare logica).

### Backtest (`backtest.py`)
- Ingresso all'open della prima 1m disponibile della candela 15m successiva, slippage avverso 0,02%,
  commissione taker 0,05%.
- SL/TP verificati sulle 1m a partire dalla 1m di ingresso inclusa. SL e TP nella stessa 1m ⇒ SL.
- Una 1m che apre già oltre SL o TP ⇒ uscita all'open di quella 1m. Applicato a ogni 1m (non solo
  dopo i buchi): per minuti consecutivi è raro e corrisponde al comportamento reale di uno stop.
- Slippage applicato anche all'uscita a TP (ipotesi conservativa) e commissione taker su entrambi i lati.
- Sizing sull'equity realizzata al momento dell'ingresso (essendo flat coincide con il mark-to-market).
- Drawdown, Sharpe e Sortino calcolati sull'equity mark-to-market a ogni chiusura 15m; Sharpe/Sortino
  su rendimenti giornalieri, annualizzati con √365, tasso privo di rischio 0.
- CAGR e % di tempo in posizione calcolati sul periodo di trading (dopo il warm-up).
- Win rate di pareggio con costi = (1 + costo medio in R) / (RR + 1).
- Righe con prezzi non positivi o OHLC incoerente: scartate (diventano minuti mancanti, nessun dato
  inventato). Righe con volume zero: mantenute. Duplicati: si tiene la prima occorrenza.
- Timestamp: unità dedotta dal numero di cifre (13 = ms, 16 = µs) riga per riga.
- Sessioni (ora di ingresso UTC): Asia 21:00–07:59, Londra 08:00–12:59, NY 13:00–20:59.
- Stress test: secondo replay con commissione e slippage doppi, parametri della strategia invariati.
- Trade aperto a fine dati: chiuso all'ultimo close 1m, `fine_dati`, escluso dalle statistiche.

### Correzioni durante lo sviluppo (bug tecnici, nessuna modifica di strategia)
- Test "posizione aperta + cambio opposto con EMA concordi": i dati sintetici non contenevano il caso;
  il test ora costruisce lo scenario in modo deterministico.
- Report: formattazione dei gruppi (anno) e dei conteggi nelle tabelle markdown.
