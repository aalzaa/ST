# Log di sviluppo — OTE Fibonacci XAUUSD (versione attuale: entry M5, trend EMA H1)

## Stato

- Codice, test e documentazione: **completi**.
- Test (`python test_strategy.py`): **22/22 superati** nell'ambiente di sviluppo (Python 3.13, pandas 3.0,
  numpy 2.5, matplotlib 3.11).
- **Backtest sui dati reali: NON ancora eseguito.** I dati (`C:\Users\Admin\Downloads\dati`) sono sul PC
  dell'utente: l'ambiente di sviluppo cloud non aveva accesso a quella cartella (nessun computer collegato)
  né a histdata.com (connessione rifiutata dalla rete dell'ambiente). Il backtest va lanciato **una volta**
  sul PC con `python backtest.py`; il report con i risultati e la valutazione è generato automaticamente in
  `output/report.md`.
- La pipeline completa è stata provata solo su **dati sintetici** (random walk 1m sugli orari reali dell'oro,
  2023-2024, formato HistData Generic): 719.040 righe, 104 weekend e 417 pause giornaliere classificati
  correttamente, 104/104 settimane con riapertura alle 22:00/23:00 UTC, run completa in ~10 secondi.
  Quei numeri di performance non hanno alcun significato e **non** sono stati salvati nel repository.

## File di dati

- Il backtest elenca nel log tutti i file presenti nella cartella e quelli usati.
- Ricerca: prima il file unico `XAUUSD_M1_2023_2024.(csv|txt|zip)` (preferenza csv > txt > zip, solo se le prime
  righe sono davvero dati HistData); in mancanza, i file annuali/mensili `DAT_ASCII_XAUUSD_M1_*`, `DAT_MT_XAUUSD_M1_*`,
  `HISTDATA_COM_(ASCII|MT)_XAUUSD_M1*` (.csv preferito allo .zip dello stesso periodo), uniti in ordine cronologico.
- Formato rilevato leggendo le righe reali: Generic ASCII (`;`, 6 colonne) o MetaTrader (`,`, 7 colonne).
  Intestazioni e righe vuote vengono scartate e contate. Negli zip si usa il membro `.csv` più grande
  (si ignora il `.txt` di report di HistData).

## Decisioni e assunzioni (prese in autonomia)

### Strategia (`strategy.py`)
- Seed espliciti: ATR = SMA dei primi 14 TR (disponibile dalla 14ª candela), EMA = SMA dei primi N close.
- Le prime 500 candele 15m (indici 0..499) sono warm-up: nessun range viene valutato e nessun ordine generato,
  sia con `warmup()` sia chiamando direttamente `on_candle_close()`. Il primo range valutabile è confermato
  alla candela 501.
- Ordine dei filtri: EMA alla conferma → swing nel buffer (SCARTATO_SWING_LUNGO) → incroci → separazione →
  BOS → ampiezza → già toccato. Lo SWING_LUNGO è verificato prima degli incroci perché senza i dati dello
  swing incroci e separazione non sono calcolabili.
- Separazione: la validità è decisa con `gap >= 0.5 * ATR` (moltiplicazione, soglia inclusa) per evitare errori
  di arrotondamento della divisione; `max_sep = max(gap/ATR)` è riportato come diagnostica. Il riferimento
  indipendente nei test usa la stessa definizione.
- Candele dello swing con EMA 100 o ATR non ancora disponibili (possibile solo per swing che iniziano nelle prime
  100 candele): nessun segno e nessuna separazione per quella candela.
- Il trend "neutro" (EMA 50 = EMA 100) o EMA non disponibili ⇒ nessun range valido; per un ordine pendente
  equivale a TREND_EMA_INVERTITO (long: EMA 50 <= EMA 100; short: EMA 50 >= EMA 100).
- Con una posizione aperta i filtri vengono comunque valutati: l'esito registrato è quello del primo filtro
  fallito oppure IGNORATO_POSIZIONE_APERTA se il range sarebbe stato valido. Nessuna Action.
- Priorità a chiusura candela con ordine pendente: un nuovo range valido (CANCEL SOSTITUITO + PLACE_LIMIT) ha la
  precedenza; altrimenti NUOVO_ESTREMO, poi TREND_EMA_INVERTITO, poi SCADUTO. Un solo CANCEL per ordine.
- Scadenza: l'ordine piazzato alla chiusura della candela N è attivo dalle candele N+1..N+48; alla chiusura della
  N+48 la strategia emette CANCEL SCADUTO (effettivo dalla N+49). Il contatore `candles_alive` è gestito dalla
  strategia (indice della candela corrente − indice del piazzamento) e scritto nel PendingOrder; se l'id non è
  noto (es. riavvio live) il contatore viene ricostruito dal valore salvato + 1 e annotato in `notes`.
- "Già toccato" usa i low/high reali delle candele 15m (anche con USE_BODIES=True), dalla candela dell'estremo
  finale alla conferma inclusa, con `low <= limite` (long) / `high >= limite` (short).
- `size_position(equity, entry, sl, side)`: aggiunto il parametro `side` (indispensabile per riconoscere la distanza
  negativa, cioè il fill in gap oltre lo SL). `position_sizing` restituisce anche il motivo dello scarto
  (SALTATO_DISTANZA_SL_NULLA_O_NEGATIVA / SALTATO_SOTTO_MIN_LOT). La distanza è calcolata sul prezzo di fill
  effettivo (limite oppure open in gap), al lordo dei costi: una perdita a SL vale quindi poco più di 1R.
- Osservazione strutturale: un CANCEL SOSTITUITO tra range dello stesso lato è quasi impossibile, perché il BOS
  richiede un nuovo estremo oltre il range precedente, che fa scattare prima NUOVO_ESTREMO. Accade quando il
  nuovo range valido viene confermato nella stessa candela in cui scatterebbe un'altra cancellazione (per esempio
  inversione del trend con range di lato opposto). È testato passando un ordine pendente esplicito.

### Backtest (`backtest.py`)
- Fuso: utc = orario del file − tz (default tz = −5 ⇒ +5 ore), senza ora legale; `America/New_York` è usato
  SOLO per verificare il calendario (riapertura domenicale, pausa giornaliera, festività), mai per convertire.
- Verifica del fuso: prima candela dopo ogni buco ≥ 24 ore che comprende un sabato; attesa la domenica alle 18:00
  di New York (23:00 UTC ora solare, 22:00 UTC ora legale), tolleranza da −10 a +30 minuti. Settimane che
  riaprono di lunedì (festività) contano come non conformi. Oltre il 20% ⇒ anomalia nel report, conversione
  invariata.
- Buchi attesi: weekend (da venerdì a domenica/lunedì di New York), pausa giornaliera (ultima candela tra le 16:00
  e le 17:15 NY, prima dopo tra le 17:30 e le 18:30 NY, ≤ 120 minuti), festività (Natale 24-26/12, Capodanno
  31/12-2/1, Venerdì Santo e — scelta documentata — le festività USA con orario ridotto del COMEX: MLK, Presidents,
  Memorial, Juneteenth, 4 luglio, Labor, Thanksgiving e venerdì successivo, con i giorni osservati). Tutto il resto
  è anomalo; nel report l'elenco dei buchi anomali > 60 minuti, nel CSV l'elenco completo.
- Righe con prezzi non positivi, fuori scala (500–10.000) o OHLC incoerente: scartate (diventano minuti mancanti,
  nessun dato inventato). Volume zero: solo riportato. Duplicati: si tiene la prima occorrenza (file precedente).
  Salti > 20 × ATR(14) 1m (|open − close precedente|): solo riportati.
- Candele 15m: solo gli intervalli con almeno una candela base; incomplete se < 15 candele base (contate nel log).
- Esecuzione: limite long eseguito solo se `low < limite` (all'open se `open < limite`), short speculare.
  SL scatta al tocco (`low <= SL` long), TP (ordine limite) solo con prezzo strettamente oltre (`high > TP` long).
  Apertura oltre SL/TP ⇒ uscita all'open (applicato a ogni candela 1m, quindi anche dopo i buchi).
  Sulla candela 1m del fill solo lo SL; SL e TP nella stessa candela ⇒ SL.
- Costi per lato: 0,15 (metà spread) + 0,05 (slippage) USD/oncia sul prezzo eseguito, 3,5 USD/lotto di commissione.
  Trigger di SL/TP sui prezzi bid del feed (lo spread è solo un costo fisso, come richiesto).
- Sizing sull'equity realizzata al momento del fill (essendo flat coincide con il mark-to-market).
- Equity mark-to-market a ogni chiusura 15m; drawdown su questa serie; Sharpe e Sortino su rendimenti giornalieri
  (ultima equity del giorno UTC), annualizzati con √252, tasso privo di rischio 0.
- CAGR e % di tempo in posizione sul periodo di trading (dopo il warm-up); % tempo in posizione = quota di
  chiusure 15m con posizione aperta.
- Win rate di pareggio effettivo = |R medio perdenti| / (R medio vincenti + |R medio perdenti|), con R netti di costi.
- Sessioni (orario del fill, UTC): Asia 22:00–06:59, Londra 07:00–12:59, NY 13:00–21:59.
- Stress test: secondo replay con spread, slippage e commissioni doppi; strategia e parametri invariati
  (i segnali sono identici, cambiano solo prezzi eseguiti ed equity).
- Trade "aperto nel weekend": posizione attraversata da un buco > 12 ore. "Uscita su gap": uscita all'open.
- Trade aperto a fine dati: chiuso all'ultimo close 1m (con costi), `fine_dati`, escluso dalle statistiche.
  Ordine pendente a fine dati: scartato (esito FINE_DATI nella lista ordini).
- La valutazione nel report è generata con regole fisse scritte prima di vedere i risultati (p-value binomiale
  del win rate contro il pareggio, t-stat dell'expectancy, costi doppi, top-5 trade, concentrazione per
  anno/trimestre/lato/sessione, regime buy & hold).

### Correzioni durante lo sviluppo (bug tecnici, nessuna modifica di strategia)
- Generatore di dati sintetici dei test: pandas 3 crea indici in microsecondi; convertiti in nanosecondi.

## Aggiornamento: swing ed entry su M5, trend EMA 50/100 su H1 (richiesta esplicita dell'utente)

- `strategy.py`: `TIMEFRAME_MINUTES = 5` (candele alimentate: M5) e nuovo `TREND_TIMEFRAME_MINUTES = 60`.
  Pivot 5/5, zigzag, dealing range, livelli, limite, SL, TP, ATR(14) per l'ampiezza: tutto su M5.
  EMA 50 / EMA 100 (e l'ATR(14) usato per la soglia di separazione) su candele H1.
- **Aggregazione H1 dentro la strategia** (stesso codice in backtest e live, interfaccia invariata: si passano solo
  le M5). Bucket orario UTC; una H1 si chiude alla chiusura della M5 delle :55, oppure — se quella manca per un
  buco — all'arrivo della prima M5 di un'ora successiva, prima di elaborare quella M5. Le EMA H1 alla chiusura di
  una M5 sono quindi quelle dell'ultima H1 chiusa: nessun dato futuro.
- **Scelte prese in autonomia** (l'utente non le ha specificate):
  - separazione EMA misurata in ATR(14) **H1** (stesso timeframe delle EMA; con l'ATR M5 il filtro sarebbe molto più
    permissivo); ampiezza del range in ATR(14) **M5** (stesso timeframe degli swing);
  - incroci e separazione contati sulle candele M5 dello swing, ognuna con il gap EMA H1 valido in quel momento
    (un incrocio H1 conta una sola volta);
  - `ORDER_EXPIRY_CANDLES = 48` resta in candele del timeframe di ingresso: **48 M5 = 4 ore** di mercato (prima 12
    ore). Come i pivot 5/5, la scadenza scala con il timeframe della struttura;
  - nuovo `WARMUP_TREND_CANDLES = 200`: nessun ordine finché non ci sono almeno 500 M5 **e** 200 H1 chiuse (EMA 100
    seminata alla 100ª H1 + 100 di assestamento). In live servono almeno ~2400 M5 di storico.
- `backtest.py`: etichette e colonne generiche (`duration_candles` al posto di `duration_candles_15m`); il periodo
  di trading (CAGR, Sharpe, equity, % tempo in posizione, buy & hold) parte dalla prima chiusura fuori dal warm-up.
- Test: riferimento indipendente del trend H1 (aggregazione oraria con la stessa regola di chiusura, EMA/ATR H1,
  numero di H1 chiuse per ogni M5) e nuovo test sulla causalità delle H1 (chiusura alle :55, :55 mancante).
  **23/23 superati.** Pipeline completa provata su dati sintetici 2023-2024 (~20 s); numeri non significativi.
- Nome della cartella invariato (`ote_fib_xauusd_15m`) per non rompere i percorsi.
- **Nota metodologica**: è una seconda variante della strategia. Se verrà scelta confrontando i risultati con la
  versione 15m sugli stessi dati 2023-2024, è ottimizzazione manuale: i risultati vanno considerati in-sample e
  validati su dati mai visti o in paper trading.
