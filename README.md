# ST — Strategia Supertrend + filtro RSI (timeframe 1h, RR 3)

| Cartella / file | Contenuto |
|---|---|
| [`supertrend_ema_btc_15m/`](supertrend_ema_btc_15m/) | strategia (testata su dati XAUUSD HistData) (`strategy.py`), backtest (`backtest.py`), test (`test_strategy.py`), README con i comandi Windows e l'uso in live |
| [`docs/LOG_SVILUPPO.md`](docs/LOG_SVILUPPO.md) | decisioni, assunzioni e stato della verifica |
| [`ote_fib_xauusd_15m/`](ote_fib_xauusd_15m/) | strategia OTE Fibonacci (limite 0.705) su XAUUSD: swing ed entry M5, trend EMA 50/100 H1: `strategy.py`, `backtest.py`, `test_strategy.py`, README con comandi Windows e uso in live, `LOG_SVILUPPO.md`, `output/` |

Avvio rapido (Windows, cmd):

```bat
cd supertrend_ema_btc_15m
py -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python test_strategy.py
python backtest.py
```

I risultati vengono salvati in `supertrend_ema_btc_15m/output/` (metriche, trade, cambi di trend,
grafico equity/drawdown, log e `report.md`).

Strategia OTE Fibonacci XAUUSD 15m (Windows, cmd):

```bat
cd ote_fib_xauusd_15m
py -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python test_strategy.py
python backtest.py
```
