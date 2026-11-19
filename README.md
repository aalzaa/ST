# ST — Strategia Supertrend + EMA su BTC

| Cartella / file | Contenuto |
|---|---|
| [`supertrend_ema_btc_15m/`](supertrend_ema_btc_15m/) | strategia (`strategy.py`), backtest (`backtest.py`), test (`test_strategy.py`), README con i comandi Windows e l'uso in live |
| [`docs/LOG_SVILUPPO.md`](docs/LOG_SVILUPPO.md) | decisioni, assunzioni e stato della verifica |

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
