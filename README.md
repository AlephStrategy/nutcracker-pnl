# Nutcracker PnL Checker

A free, open-source portfolio analysis tool built on top of ccxt.  
Designed to provide a general representation of exchange account performance using ATR metrics, per-symbol analysis, and realized/unrealized profit breakdown.

## ⚠️ Disclaimer

This tool is **not financial advice** and **not suitable for accounting or tax reporting**.  
It provides an approximate analytical view of your portfolio based on exchange API data.

- API keys are **never stored** outside your browser session.
- **Withdrawal-enabled API keys must NOT be used**.
- Regional restrictions or exchange permissions may limit functionality.
- The tool is provided **free of charge**, **as-is**, without warranty.

## Features

- Per-symbol ATR-based performance metrics  
- Realized & unrealized PnL  
- Secondary reporting currency (BTC default; ETH/BNB/XRP optional)  
- Manual deposit/withdrawal adjustments  
- Works with most ccxt-enabled exchanges  

## Structure
nutcracker_pnl/
app.py          # Streamlit UI
pnl.py          # Core analytics engine
routing/        # Exchange identity + region fallback
favicon.


## License

MIT License


