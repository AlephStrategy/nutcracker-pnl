import streamlit as st
st.set_page_config(
    page_title="Nutcracker PnL Checker",
    page_icon="favicon.ico",   # file must be in same folder as app.py
    layout="wide"
)
import logging
import ccxt
import json
from datetime import datetime, timedelta
import pandas as pd
import importlib
import requests
from routing.router import route
import pnl
importlib.reload(pnl)
SERVER_IP = requests.get("https://api.ipify.org").text



# Disable detailed error tracebacks in the UI (privacy + UX)
#st.set_option("client.showErrorDetails", False)

with open("config/upstreams.json") as f:
    backends = json.load(f)

# ---------------------------------------------------------
# Helper: unwrap balance
# ---------------------------------------------------------
def unwrap_balance(v):
    if v is None:
        return 0.0
    if isinstance(v, dict):
        if "total" in v:
            return float(v["total"] or 0)
        free = float(v.get("free", 0) or 0)
        used = float(v.get("used", 0) or 0)
        return free + used
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
# ---------------------------------------------------------
# Session State Initialization
# ---------------------------------------------------------
if "flows_fetched_once" not in st.session_state:
    st.session_state.flows_fetched_once = False

if "fetched_flows" not in st.session_state:
    st.session_state.fetched_flows = None

if "analysis_ran" not in st.session_state:
    st.session_state.analysis_ran = False

if "manual_flows_list" not in st.session_state:
    st.session_state.manual_flows_list = []

manual_flows = None

# ---------------------------------------------------------
# Title
# ---------------------------------------------------------
st.header("Nutcracker PnL Analyzer")

# ---------------------------------------------------------
# Terms & Agreement
# ---------------------------------------------------------
with st.expander("📄 Terms & Agreement", expanded=True):
    st.write("""
This PnL Checker is an analytics tool and **not** a tax, accounting, or financial advisory product.

**Aleph Strategy** (R&D Lab) and **Nutcracker™** (algorithmic trading engine)  
never have access to your API keys, funds, or personal data.  
Keys are stored only in your browser cache and never transmitted elsewhere.

We strongly recommend using:
- **read‑only, no‑withdrawal API keys** for this tool  
- **no‑withdrawal API keys** for all Aleph Strategy products  

The analysis is based on:
- balances and trades fetched from your exchange  
- reconstructed starting balances (since most exchanges do not provide historical snapshots)  
- a matching‑based realized PnL method (unmatched tails are truncated)  
- user‑provided manual flows when needed  

Depending on your exchange setup (spot, margin, lending, sub‑accounts),  
some balances or flows may not be retrievable and will not appear in the analysis.

**Aleph Strategy, Nutcracker™, and the developers of this tool do not provide financial advice.**  
Your use of this tool is entirely at your discretion.
""")

    agree = st.checkbox("I understand and agree to the terms above.")

# ---------------------------------------------------------
# Settings (only enabled after agreement)
# ---------------------------------------------------------
if not agree:
    st.warning("Please agree to the terms above to continue.")
    st.stop()

with st.expander("⚙️ Settings", expanded=True):

    st.markdown("### Step 1 — Exchange")
    exchange_name = st.selectbox(
        "Select Exchange",
        [
            "binance",
            "bitfinex",
            "bitget",
            "bitrue",
            "bybit",
            "coinbase",
            "gateio",
            "huobi",
            "kraken",
            "kucoin",
            "mexc",
            "okx",
            "poloniex",
            "other"
        ]
    )
    if exchange_name == "other":
        exchange_name = st.text_input("Enter exchange id (must match CCXT id)")

    api_key = st.text_input("API Key", type="password", 
        help="Use a read‑only or no‑withdrawal API key. Never use a key with trading or withdrawal permissions."
    )
    api_secret = st.text_input("API Secret", type="password", 
        help="Secret key for your exchange API."
    )
    api_password = st.text_input("API Password / Passphrase (optional)", type="password", 
        help="Some exchanges will require a password or passphrase - provided with API keys"
    )

    st.caption(
        "🔒 **Security Notice:** Use *read‑only* or *no‑withdrawal* API keys. "
        "Keys must **not** have trading, withdrawal, or write permissions."
    )

    st.markdown("### Step 2 — Lookback Window")
    lookback_days = st.number_input(
        "Lookback (days)",
        min_value=2,
        max_value=365,
        value=30,
        help="Number of days of trade and balance history to include in the analysis."
    )

    st.markdown("### Step 3 — Reporting Currency")
    secondary_reporting = st.selectbox(
        "Secondary Reporting Currency",
        ["BTC", "ETH", "XRP", "SOL", "BNB"],
        index=0,
        help="PnL is always computed in USDC. This option adds an alternative reporting layer (e.g., BTC)."
    )
    st.caption(
        "Primary reporting currency is **USDC**. All PnL and alpha metrics are computed in USDC. "
        "Secondary reporting currency provides an additional perspective."
    )

    st.markdown("### Step 4 — Flows")

    # Fetch flows only once per session
    fetch_disabled = st.session_state.flows_fetched_once

    if st.button(
        "Fetch Exchange Flows",
        disabled=fetch_disabled,
        help="Fetches deposits and withdrawals from the exchange. This may take several minutes depending on API speed."
    ):
        exchange = pnl.init_exchange(exchange_name, api_key, api_secret, api_password)
        since = int((datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000)
        try:
            flows = pnl.fetch_portfolio_flows(exchange, since, [])
        except ccxt.AuthenticationError as e:
            msg = str(e).lower()
            if "ip" in msg or "whitelist" in msg or "permission" in msg:
                st.error(
                    "Your API key is restricted to specific IP addresses.\n\n"
                    "Please whitelist the following server IP to continue:\n\n"
                    f"**{SERVER_IP}**"
                )
                st.stop()
            else:
                raise


        st.session_state.fetched_flows = flows
        st.session_state.flows_fetched_once = True
        st.session_state.analysis_ran = False  # reset analysis state

        st.success("Flows fetched successfully.")

    # Reset button
    if st.session_state.flows_fetched_once:
        if st.button("Reset Flows"):
            st.session_state.fetched_flows = None
            st.session_state.flows_fetched_once = False
            st.session_state.manual_flows_list = []
            st.session_state.analysis_ran = False
            st.info("Flows reset. You may fetch again.")

    # Show flows (even if empty)
    if st.session_state.fetched_flows is not None:
        st.subheader("Fetched Exchange Flows")

        if st.session_state.fetched_flows == {}:
            st.info("No exchange flows detected. You may add manual flows below.")
        else:
            st.write("These are the flows detected from the exchange.")
            st.json(st.session_state.fetched_flows)

        # ---------------------------------------------------------
        # Manual Adjustments (Optional)
        # ---------------------------------------------------------
        st.markdown("### Step 5 — Manual Adjustments (Optional)")
        manual_mode = st.checkbox("Add manual deposits/withdrawals")

        if manual_mode:
            st.write("Add manual flows below. These will be merged with fetched exchange flows.")

            with st.form("manual_flow_form"):
                colA, colB, colC = st.columns([2, 2, 2])

                asset = colA.text_input("Asset (e.g., USDC, BTC, EUR)").upper()
                amount = colB.number_input("Amount", min_value=0.0, format="%.8f")
                flow_type = colC.selectbox("Type", ["deposit", "withdrawal"])

                submitted = st.form_submit_button("Add Flow")

                if submitted:
                    if asset and amount > 0:
                        st.session_state.manual_flows_list.append({
                            "asset": asset,
                            "amount": amount,
                            "type": flow_type
                        })
                    else:
                        st.warning("Please enter a valid asset and amount.")

            if st.session_state.manual_flows_list:
                st.markdown("#### Added Manual Flows")
                st.json(st.session_state.manual_flows_list)

            manual_flows = st.session_state.manual_flows_list
        else:
            manual_flows = None


st.markdown("### Live Log Output")
log_container = st.container()
# ---------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------
root_logger = logging.getLogger()
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)

class StreamlitHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        log_container.markdown(
            f"<pre style='color:#00e676; background-color:#000000; padding:8px; border-radius:4px;'>{msg}</pre>",
            unsafe_allow_html=True
        )

streamlit_handler = StreamlitHandler()
streamlit_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(streamlit_handler)
logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------
# Run Full PnL Analysis
# ---------------------------------------------------------
st.header("Run PnL Analysis")

run_disabled = (
    st.session_state.fetched_flows is None
    or st.session_state.analysis_ran
)

if st.button("Run Analysis", disabled=run_disabled):
    if run_disabled:
        st.error("Please fetch exchange flows first.")
    else:
        # Initialize exchange (this is the object run_pnl_analysis expects)
        # 1. Create probe exchange WITHOUT loading markets
        probe_exchange = pnl.init_exchange(
            exchange_name,
            api_key,
            api_secret,
            api_password,
            load_markets=False
        )

        # 2. Route based on region
        selected_backend = route(exchange_name, probe_exchange, backends)

        # 3. Now create the real exchange (markets load on correct backend)
        exchange = pnl.init_exchange(
            exchange_name,
            api_key,
            api_secret,
            api_password,
            load_markets=True
        )

        # Merge fetched flows + manual flows
        if manual_flows:
            merged_flows = dict(st.session_state.fetched_flows)
            merged_flows["manual"] = manual_flows
        else:
            merged_flows = st.session_state.fetched_flows

        # Run analysis with correct signature
        results = pnl.run_pnl_analysis(
            exchange,
            lookback_days,
            secondary_reporting,
            pre_fetched_flows=merged_flows
        )

        st.session_state.results = results
        st.session_state.analysis_ran = True

        st.success("Analysis complete.")

def metric_small(label, value_html, **kwargs):
    st.markdown(
        f"""
        <div style="border:1px solid rgba(49,51,63,0.2); padding:12px; border-radius:8px;">
            <div style="font-size:0.85rem; opacity:0.7;">{label}</div>
            <div style="font-size:0.85rem; font-weight:600;">{value_html}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


# ---------------------------------------------------------
# Render results if available
# ---------------------------------------------------------
if "results" in st.session_state:
    results = st.session_state.results

    # Download button (safe now)
    st.download_button(
        label="Download Full Results (JSON)",
        data=json.dumps(results, indent=2),
        file_name="nutcracker_pnl_results.json",
        mime="application/json"
    )

    # ---------------------------------------------------------
    # Dashboard Tabs
    # ---------------------------------------------------------
    tab_trades, tab_portfolio, tab_alpha, tab_equity, tab_balances = st.tabs([
        "Trades",
        "Portfolio",
        "Alpha Audit",
        "Equity Curve",
        "Balances"
    ])

    # ---------------------------------------------------------
    # Trades Tab
    # ---------------------------------------------------------
    with tab_trades:
        trades = results.get("trades", {})
        trade_analysis = trades.get("trade_analysis", {})
        portfolio = results.get("portfolio", {})

        st.subheader("Portfolio‑Trade Metrics")

        col1, col2 = st.columns(2)
        col1.metric("ROIC", f"{portfolio.get('roic_pct', 0):.2f}%",
            help="$ Return from trades (realized) on starting portfolio per annum."
        )
        col2.metric("Turnover", f"{portfolio.get('turnover', 0):.2f}x",
            help="How many times starting portfolio would trade in a year at current rate."
        )

        st.markdown("---")

        # ---------------------------------------------------------
        # Aggregated Totals
        # ---------------------------------------------------------

        st.markdown("### Aggregated Totals")

        col1, col2 = st.columns(2)
        col1.metric("Total Buy Size", f"{trades.get('total_buy_size', 0):.2f} USDT",
            help="$ Amount of all Buys"
        )
        col2.metric("Total Sell Size", f"{trades.get('total_sell_size', 0):.2f} USDT",
            help="$ Amount of all Sells"
        )

        col3, col4 = st.columns(2)
        col3.metric("Total Realized PnL", f"{trades.get('total_nominal_pnl', 0):.2f} USDT",
            help="Return from matched trades during the period."
        )
        col4.metric("Total PnL %", f"{trades.get('total_pnl_pct', 0):.2f}%",
            help="% Return from matched trades during the period."
        )

        st.metric("Annualized Trade Return", f"{trades.get('annualized_trade_return_pct', 0):.2f}%",
            help="Annualized return from realized pnl (trades)."
        )

        # ---------------------------------------------------------
        # Per‑Symbol Detailed Expanders
        # ---------------------------------------------------------

        st.markdown("### Per‑Symbol Details")

        for symbol, stats in trade_analysis.items():
            with st.expander(symbol):
                st.write(f"**Number of Trades:** {stats['num_trades']}")
                st.write(f"**Buy Size:** {stats['buy_size']:.8f} USDT")
                st.write(f"**Sell Size:** {stats['sell_size']:.8f} USDT")
                st.write(f"**Nominal PnL:** {stats['nominal_pl']:.8f} USDT")
                st.write(f"**PnL %:** {stats['pct_pl']:.2f}%")

        st.markdown("---")

    # ---------------------------------------------------------
    # Portfolio Tab
    # ---------------------------------------------------------
    with tab_portfolio:
        portfolio = results.get("portfolio", {})
        trades = results.get("trades", {})

        PRIMARY = "USDC"
        SECONDARY = portfolio.get("secondary_reporting", "BTC")  # from backend

        st.subheader("Portfolio Valuation")

        # -------------------------------
        # Start / End Values
        # -------------------------------
        col1, col2 = st.columns(2)

        col1.metric(
            label="Start Value (USDC)",
            value=f"{portfolio.get('start_value_usdc_flow_adj', 0):,.2f}",
            help="Portfolio valuation at the beginning of the lookback window - from reconstructed trades and adjusted for flows."
        )

        col2.metric(
            label="End Value (USDC)",
            value=f"{portfolio.get('end_value_usdc', 0):,.2f}",
            help="Portfolio valuation at the end of the lookback window - from exchange balances."
        )

        # -------------------------------
        # Net Flows
        # -------------------------------
        st.metric(
            label="Net Flows (USDC)",
            value=f"{portfolio.get('net_flow_value_usdc', 0):,.2f}",
            help="Deposits minus withdrawals during the lookback window."
        )

        st.markdown("---")
        # -------------------------------
        # Secondary Reporting Currency
        # -------------------------------
        st.markdown(f"### Portfolio Valuation ({SECONDARY})")

        col1, col2 = st.columns(2)

        col1.metric(
            label=f"Start Value ({SECONDARY})",
            value=f"{portfolio.get('start_value_alt', 0):,.6f}",
            help="Portfolio start value converted into the secondary reporting currency."
        )

        col2.metric(
            label=f"End Value ({SECONDARY})",
            value=f"{portfolio.get('end_value_alt', 0):,.6f}",
            help="Portfolio end value converted into the secondary reporting currency."
        )

        st.markdown("---")
        # -------------------------------
        # Nominal PnL + Breakdown
        # -------------------------------
        st.markdown("### Profit & Loss USDC")

        nominal = portfolio.get("nominal_pnl_usdc", 0)
        realized = portfolio.get("realized_pnl_usdc", 0)
        unrealized = portfolio.get("unrealized_pnl_usdc", 0)

        col1, col2, col3 = st.columns(3)

        col1.metric(
            label="Total PnL (USDC)",
            value=f"{nominal:,.2f}",
            help="Total PnL including realized and unrealized components."
        )

        col2.metric(
            label="Return %",
            value=f"{portfolio.get('pct_pnl_usdc', 0):.2f}%",
            help="Percentage return relative to starting portfolio value."
        )

        col3.metric(
            label="Annualized Return %",
            value=f"{portfolio.get('annualized_return_pct', 0):.2f}%",
            help="Annualized return if current pnl of lookback period sustains for a year."
        )

        with st.expander("PnL Breakdown"):
            st.write(f"**Realized PnL:** {realized:,.2f} USDC")
            st.write(f"**Unrealized PnL:** {unrealized:,.2f} USDC")

        st.markdown("---")

        st.markdown(f"### Profit & Loss ({SECONDARY})")

        col1, col2, col3 = st.columns(3)

        col1.metric(
            label=f"Total PnL ({SECONDARY})",
            value=f"{portfolio.get('nominal_pnl_alt', 0):,.8f}",
            help="Total PnL converted using the secondary reporting currency."
        )

        col2.metric(
            label=f"Return % ({SECONDARY})",
            value=f"{portfolio.get('pct_pnl_alt', 0):,.2f}%",
            help="Percentage return relative to starting portfolio value in the secondary reporting currency."
        )

        col3.metric(
            label=f"Annualized Return % ({SECONDARY})",
            value=f"{portfolio.get('annualized_return_pct_alt', 0):,.2f}%",
            help="Annualized return if current pnl of lookback period sustains for a year in the secondary reporting currency."
        )

        st.markdown("---")

    # ---------------------------------------------------------
    # Alpha Audit Tab
    # ---------------------------------------------------------
    with tab_alpha:
        alpha = results.get("alpha_audit", {})
        audit_usdc = alpha.get("usdc", {})
        audit_alt = alpha.get("alt", {})

        SECONDARY = audit_alt.get("secondary_reporting", "ALT")

        st.subheader("Alpha Audit Overview")

        # -------------------------------
        # USDC Section
        # -------------------------------
        st.markdown("### Strategy Alpha (USDC)")

        col1, col2, col3 = st.columns(3)
        col1.metric(
            "Buy & Hold Floor (Flow‑Adj)",
            f"{audit_usdc.get('market_floor_pnl_flow_adj', 0):,.2f} USDC",
            help="Baseline performance if the portfolio simply held its initial composition."
        )
        col2.metric(
            "Actual PnL",
            f"{audit_usdc.get('actual_pnl', 0):,.2f} USDC",
            help="Total realized + unrealized PnL in USDC terms."
        )

        col3.metric(
            "ATR Opportunity - Ceiling (Theoretical)",
            f"{audit_usdc.get('atr_opportunity', 0):,.2f} USDC",
            help="Theoretical max volatility harvesting potential based on ATR."
        )

        colA, colB = st.columns(2)
        colA.metric(
            "ATR Opportunity (Realistic)",
            f"{audit_usdc.get('atr_opportunity_realistic', 0):,.2f} USDC",
            help="Realistic (adjusted by turnover rate) volatility harvesting potential based on ATR."
        )
        colB.metric(
            "ATR Efficiency (Realistic)",
            f"{audit_usdc.get('atr_efficiency_realistic_pct', 0):.2f}%",
            help="How much of the realistic ATR opportunity the strategy captured."
        )


        # -------------------------------
        # ALT Section
        # -------------------------------
        st.markdown(f"### Strategy Alpha ({SECONDARY})")

        with st.expander(f"Detailed {SECONDARY} Audit"):
            st.write(f"**Buy & Hold Floor (Flow‑Adj):** {audit_alt.get('market_floor_pnl_flow_adj', 0):,.8f} {SECONDARY}")
            st.write(f"**Actual PnL:** {audit_alt.get('actual_pnl_alt', 0):,.8f} {SECONDARY}")
            st.write(f"**ATR Opportunity (Theoretical):** {audit_alt.get('atr_opportunity', 0):,.8f} {SECONDARY}")
            st.write(f"**ATR Opportunity (Realistic):** {audit_alt.get('atr_opportunity_realistic', 0):,.8f} {SECONDARY}")
            st.write(f"**ATR Efficiency (Realistic):** {audit_alt.get('atr_efficiency_realistic_pct', 0):.2f}%")

    # ---------------------------------------------------------
    # Equity Curve Tab
    # ---------------------------------------------------------
    with tab_equity:
        equity_curve = results.get("equity_curve", [])
        audit_usdc = results.get("alpha_audit", {}).get("usdc", {})
        portfolio = results.get("portfolio", {})

        st.subheader("Equity Curve vs Alpha Potential (USDC)")
        st.caption("This chart compares actual performance to baseline and theoretical noise harvesting potential.")

        if equity_curve:
            df = pd.DataFrame(equity_curve)
            df["date"] = pd.to_datetime(df["date"])
            df = df.rename(columns={"value": "Equity"})

            start_equity = portfolio.get("start_value_usdc_flow_adj", df["Equity"].iloc[0])


            # Buy & Hold Floor
            floor_end = start_equity + audit_usdc.get("market_floor_pnl_flow_adj", 0)
            df["Buy & Hold Floor"] = [
                start_equity + (floor_end - start_equity) * (i / (len(df) - 1))
                for i in range(len(df))
            ]

            # Alpha Potential (Noise)
            potential_end = start_equity + audit_usdc.get("alpha_potential", 0)
            df["Alpha Potential"] = [
                start_equity + (potential_end - start_equity) * (i / (len(df) - 1))
                for i in range(len(df))
            ]

            df = df.set_index("date")

            st.line_chart(df, use_container_width=True)

            st.caption(
                "Orange = Portfolio equity (USDC). "
                "Blue = Buy & Hold floor (flow‑adjusted). "
                "Cyan = Alpha Potential (Noise) — theoretical maximum extractable alpha."
            )

        else:
            st.info("No equity curve data available.")

        st.markdown("---")

        # ---------------------------------------------------------
        # Alpha Harvested vs Alpha Potential (USDC)
        # ---------------------------------------------------------
        st.markdown("### Equity Curve vs Alpha Potential (USDC)")

        harvested_usdc = audit_usdc.get("alpha_harvested_flow_adj", 0)
        potential_usdc = audit_usdc.get("alpha_potential", 0)
        eff_usdc = audit_usdc.get("efficiency_flow_adj_pct", 0)

        col1, col2 = st.columns(2)

        col1.metric(
            "Alpha Harvested (Flow‑Adj)",
            f"{harvested_usdc:,.2f} USDC ({eff_usdc:.2f}%)",
            help="Alpha extracted after adjusting for flows, with efficiency vs theoretical noise potential."
        )

        col2.metric(
            "Alpha Potential (Noise)",
            f"{potential_usdc:,.2f} USDC",
            help="Maximum extractable alpha based on volatility/noise."
        )

        # ---------------------------------------------------------
        # Alpha Harvested vs Alpha Potential (Secondary)
        # ---------------------------------------------------------
        st.markdown(f"### Equity Curve Harvested vs Available Potential ({SECONDARY})")

        harvested_alt = audit_alt.get("alpha_harvested_flow_adj", 0)
        potential_alt = audit_alt.get("alpha_potential", 0)
        eff_alt = audit_alt.get("efficiency_flow_adj_pct", 0)


        col1, col2 = st.columns(2)

        metric_small(
            "Alpha Harvested (Flow‑Adj)",
            f"{harvested_alt:,.8f} {SECONDARY} ({eff_alt:.2f}%)",
            help="Alpha extracted after adjusting for flows, with efficiency vs theoretical noise potential."
        )

        metric_small(
            "Alpha Potential (Noise)",
            f"{potential_alt:,.8f} {SECONDARY}",
            help="Maximum extractable alpha based on volatility/noise."
        )

    
    # ---------------------------------------------------------
    # Balances Tab
    # ---------------------------------------------------------
    with tab_balances:
        balances = results.get("balances", {})
        balances_start = balances.get("start", {})
        balances_end = balances.get("end", {})

        st.subheader("Portfolio Balances")

        st.caption(
            "These balances represent reconstructed holdings at the start and end of the lookback window. "
            "Most exchanges do not provide historical balance snapshots, so starting balances are derived "
            "from trade history and adjusted for net flows."
            "Any negative starting values are from flow adjustment and/or internal wallet transfers."
        )

        if balances_start:
            symbols = list(balances_start.keys())
            df = pd.DataFrame({
                "Symbol": symbols,
                "Start Balance": [f"{unwrap_balance(balances_start[sym]):.8f}" for sym in symbols],
                "End Balance": [f"{unwrap_balance(balances_end.get(sym, 0)):.8f}" for sym in symbols],
            })

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("### Start Balances")
                st.dataframe(
                    df[["Symbol", "Start Balance"]],
                    use_container_width=True
                )

            with col2:
                st.markdown("### End Balances")
                st.dataframe(
                    df[["Symbol", "End Balance"]],
                    use_container_width=True
                )

        else:
            st.info("No balance data available for the selected lookback window.")



