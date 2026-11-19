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

## Aggiornamento: dati XAUUSD (HistData.com)

L'utente ha comunicato che i dati non sono Binance BTCUSDT ma **XAUUSD M1 da HistData.com**,
nella cartella `C:\Users\Admin\Downloads\dati` (file `XAUUSD_M1_2023_2024.csv`, più i file annuali
`DAT_ASCII_XAUUSD_M1_2023/2024.csv` e i `.txt` di report di HistData).

Modifiche (solo `backtest.py` e test; **`strategy.py` invariato**, parametri invariati):
- default `--data-dir` = `C:\Users\Admin\Downloads\dati`, `--name` = `XAUUSD_M1_2023_2024`;
- formato rilevato dal contenuto: HistData `YYYYMMDD HHMMSS;O;H;L;C;V` con separatore `;`;
- orari HistData in EST fisso (UTC-5 tutto l'anno) → UTC con +5 ore (assunzione documentata da HistData);
- buchi classificati in `chiusura_weekend`, `pausa_giornaliera` (17-18 New York, con ora legale) e `altro`;
  nel report sono elencati in dettaglio i buchi > 60 minuti di tipo `altro` (es. festività, dati mancanti);
- dichiarazioni del report adattate: fonte unica, prezzi BID, mercato non 24/7, short possibili su CFD,
  swap non inclusi, spread non modellato esplicitamente;
- costi lasciati ai valori della specifica (0,05% + 0,02% per lato), dichiarati come tali;
- aggiunta la metrica `buy_and_hold_pct` del sottostante nel periodo di trading, per giudicare il regime;
- nuovo test `test_histdata_format_and_gaps` (14/14 test superati).

Prova tecnica su un file sintetico in formato HistData con gli orari reali dell'oro (2023-2024):
520 buchi, tutti classificati correttamente (416 pause giornaliere, 104 weekend). Anche qui i numeri
di performance sono sintetici e privi di significato.

## Aggiornamento: timeframe 2h e RR 3 (richiesta esplicita dell'utente)

- `strategy.py`: nuovo parametro `TIMEFRAME_MINUTES = 120` (prima 15) e `RR = 3.0` (prima 2.0).
  Gli altri parametri (ATR 10, moltiplicatore 3, EMA 50/100, rischio 1%, warm-up 500 candele) sono invariati.
- Il timeframe sta in `strategy.py` perché fa parte della definizione della strategia: il backtest lo legge da lì
  (`backtest.TF`) e un loop live deve usare lo stesso valore.
- Candele 2h allineate a multipli di 2 ore dalla mezzanotte UTC (00:00, 02:00, ...). Con l'oro la candela che
  contiene la pausa giornaliera risulta incompleta (alimentata comunque, marcata nel log).
- Warm-up: 500 candele 2h ≈ 2 mesi di mercato; il periodo di trading si accorcia di conseguenza.
- Con un timeframe più lento il numero di trade su 2023-2024 sarà verosimilmente inferiore a 200: in quel caso il
  report segnala "campione insufficiente".
- **Nota metodologica**: cambiare timeframe e RR è una nuova variante della strategia. Se la scelta è stata fatta
  dopo aver visto i risultati della versione 15m/RR 2 sugli stessi dati, è una forma di ottimizzazione manuale e
  i risultati della nuova versione vanno considerati in-sample; servono dati mai visti (es. 2025) per validarla.
- Test aggiornati (TP calcolato con `st.RR`, dati sintetici più lunghi per il timeframe 2h): 14/14 superati.
- Cartella non rinominata (`supertrend_ema_btc_15m`) per non rompere i percorsi già usati dall'utente.

## Aggiornamento: timeframe 30m (richiesta esplicita dell'utente)

- `TIMEFRAME_MINUTES = 30` (prima 120); `RR = 3.0` invariato; altri parametri invariati.
- Candele 30m allineate a :00 e :30 UTC. Warm-up 500 candele 30m ≈ 2 settimane di mercato.
- Corretto un test (aggregazione) che assumeva che il buco sintetico cadesse nella prima candela: ora è
  indipendente dal timeframe. 14/14 test superati.
- **Nota metodologica**: è la terza combinazione timeframe/RR (15m/RR2 → 2h/RR3 → 30m/RR3). Provare più varianti
  sugli stessi dati 2023-2024 e scegliere la migliore è ottimizzazione manuale: il risultato della variante scelta
  è in-sample e va validato su dati mai visti (es. 2025) o in paper trading prima di qualsiasi uso reale.

## Aggiornamento: timeframe 1h (richiesta esplicita dell'utente)

- `TIMEFRAME_MINUTES = 60` (prima 30); `RR = 3.0` e altri parametri invariati. Candele allineate all'ora piena UTC.
  Warm-up 500 candele 1h ≈ 1 mese di mercato. 14/14 test superati.
- **Nota metodologica**: quarta combinazione timeframe/RR sugli stessi dati (15m/RR2, 2h/RR3, 30m/RR3, 1h/RR3).
  Qualunque variante venga scelta confrontando i risultati è in-sample: validazione obbligatoria su dati mai visti.
