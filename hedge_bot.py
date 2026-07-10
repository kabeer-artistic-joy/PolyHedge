#!/usr/bin/env python3
"""
Polymarket Split-Hedge Scalper
================================
Strategy (locked in after extensive discussion — see accompanying design log):
  - At entry, SPLIT $SPLIT_AMOUNT_USD into an equal number of Up shares and
    Down shares. Split is a fixed on-chain conversion ($1 always mints exactly
    1 Up + 1 Down share, regardless of current market odds) — this is used
    specifically for its ATOMICITY: two separate buy orders can fill at
    different times, breaking the "both legs anchored to the same reference
    moment" premise the strategy depends on. Split has no such risk.
  - Immediately rest TWO sell limit orders: one on each leg, each priced
    SELL_TARGET_PER_SHARE above that leg's price at the moment of entry.
  - NO stop-loss. The worst case is bounded and known in advance (lose at
    most the full cost basis of whichever leg never sells, since a binary
    contract floors at $0) — a fundamentally different, KNOWN-ceiling risk
    from the original bot's unpredictable slippage problem, so removing the
    stop-loss here is a deliberate, reasoned choice, not the same mistake.
  - Three real outcomes, not two:
      A) both legs hit their target -> real win
      B) one leg hits and sells, the other rides to $0 at resolution -> real loss
      C) neither leg ever hits, window resolves normally -> a WASH (the winning
         side pays exactly what was spent, net $0) — this does NOT count
         against viability, only the ratio of A to B outcomes matters.
  - Entry is gated, not blind-at-every-coinflip:
      - Skip entirely if |delta from price-to-beat| exceeds MAX_DELTA_TO_ENTER
        (a strong persistent trend is exactly the outcome-B danger zone).
      - Within that band, require a real CHOPPINESS signal (path length vs
        net displacement over a rolling lookback) confirming genuine
        two-sided churn, not just entering because the delta happens to be small.
  - Up to MAX_ENTRIES_PER_WINDOW re-entries per window IF a prior split has
    already fully resolved and a fresh qualifying signal appears — not
    mandatory, depends on real conditions.

WHAT STILL NEEDS LIVE VERIFICATION — READ BEFORE RUNNING LIVE:
  The actual on-chain SPLIT call (_split_position) is written against the
  documented CTF (Conditional Tokens Framework) mechanics but has NOT been
  tested against a real account yet — same situation as every other brand
  new API integration in this project. Expect to adjust the exact call once
  you have real credentials to test against.

Usage:
  python hedge_bot.py --dry-run
  python hedge_bot.py --live --amount 2
"""
import time
import json
import csv
import argparse
import threading
import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
SYMBOLS = {"BTC": "BTCUSDT"}
MARKETS = {"btc-updown-5m": "BTC"}

SPLIT_AMOUNT_USD = 2.0          # $ split into equal Up+Down shares per entry
SELL_TARGET_PER_SHARE = 0.05    # sell target above each leg's entry price

MAX_DELTA_TO_ENTER = 10.0       # skip entirely if |BTC price - price-to-beat| exceeds this —
                                  # a strong persistent trend is the outcome-B danger zone

CHOPPINESS_LOOKBACK_SEC = 45    # rolling window for the churn-detection signal
CHOPPINESS_RATIO_THRESHOLD = 2.5  # path_length / net_displacement must exceed this to enter —
                                     # starting guess, meant to be refined against real dry-run data,
                                     # not assumed correct in advance

MAX_ENTRIES_PER_WINDOW = 4      # cap, not a mandate — depends on real conditions
MONITOR_INTERVAL = 1.0
POLL_INTERVAL_LEG = 0.5         # how often to check each resting sell leg for a fill

# ─── CTF (Conditional Token Framework) CONSTANTS ────────────────────────────
# Verified against Polymarket's official documentation
# (github.com/Polymarket/agent-skills/blob/main/ctf-operations.md) — split is a
# DIRECT SMART CONTRACT CALL, not a py_clob_client_v2 / CLOB API method.
POLYGON_RPC = "https://polygon-rpc.com"
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID = "0x" + "00" * 32  # always bytes32(0) for Polymarket markets
BINARY_PARTITION = [1, 2]  # Yes=1, No=2 (Up=1, Down=2 for these markets)

# Minimal ABIs — only the functions actually needed
CTF_ABI = [
    {
        "name": "splitPosition", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]
ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

# ─── UTILITIES (proven patterns, reused from breakthrough_bot.py) ───────────
_print_lock = threading.Lock()

def ts_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg, crypto=""):
    prefix = f"[{crypto}] " if crypto else ""
    with _print_lock:
        print(f"[{ts_str()}] {prefix}{msg}", flush=True)

def now_unix():
    return time.time()

def get_binance_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=2)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None

def get_window_open_price(symbol: str, window_ts: int) -> float | None:
    try:
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "startTime": window_ts * 1000, "limit": 1},
            timeout=3,
        )
        r.raise_for_status()
        candles = r.json()
        return float(candles[0][1]) if candles else None
    except Exception:
        return None

def get_window_market(slug_prefix: str, start_ts: int) -> dict | None:
    slug = f"{slug_prefix}-{start_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=3)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        event = data[0]
    except Exception:
        return None
    markets = event.get("markets", [])
    if not markets:
        return None
    market = markets[0]
    try:
        outcomes = json.loads(market.get("outcomes", "[]"))
        clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
    except Exception:
        return None
    if len(outcomes) < 2 or len(clob_token_ids) < 2:
        return None
    tokens = dict(zip(outcomes, clob_token_ids))
    if "Down" not in tokens or "Up" not in tokens:
        return None
    return {
        "slug": slug, "crypto": MARKETS[slug_prefix], "start_ts": start_ts, "close_ts": start_ts + 300,
        "down_token": tokens["Down"], "up_token": tokens["Up"],
        "condition_id": market.get("conditionId", ""), "title": event.get("title", ""),
    }

def get_order_book(token_id: str) -> dict:
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=2)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

def best_bid(book: dict):
    bids = book.get("bids", [])
    if not bids:
        return None, None
    highest = max(bids, key=lambda b: float(b["price"]))
    return float(highest["price"]), float(highest["size"])

def next_window_start(now: float) -> int:
    return int((now // 300) + 1) * 300

# ─── ROLLING PRICE HISTORY FOR THE CHOPPINESS SIGNAL ────────────────────────
class PriceHistory:
    def __init__(self, window_seconds: float):
        self.window_seconds = window_seconds
        self.samples = []
        self.lock = threading.Lock()

    def add(self, price: float):
        with self.lock:
            now = now_unix()
            self.samples.append((now, price))
            cutoff = now - self.window_seconds - 5
            self.samples = [(t, p) for t, p in self.samples if t >= cutoff]

    def choppiness_ratio(self):
        """path_length / net_displacement over the lookback window.
        High ratio = genuine back-and-forth churn. Low ratio (near 1) =
        a clean, mostly-monotonic directional move. Returns None if we
        don't yet have a FULL window_seconds of real elapsed history —
        REAL GAP FIXED HERE: previously only required 3 samples to exist,
        which could compute a misleading ratio off a few seconds of data
        early in a window, not the genuine full-window read intended."""
        with self.lock:
            if len(self.samples) < 3:
                return None
            now = now_unix()
            oldest_sample_time = self.samples[0][0]
            if now - oldest_sample_time < self.window_seconds:
                return None  # not enough REAL elapsed time yet, regardless of sample count
            in_window = [(t, p) for t, p in self.samples if t >= now - self.window_seconds]
            if len(in_window) < 3:
                return None
            path_length = sum(abs(in_window[i][1] - in_window[i-1][1]) for i in range(1, len(in_window)))
            net_displacement = abs(in_window[-1][1] - in_window[0][1])
            if net_displacement < 1e-9:
                return float('inf') if path_length > 0 else None
            return path_length / net_displacement

# ─── PERSISTENT CSV LOG ──────────────────────────────────────────────────────
CSV_FIELDS = [
    "timestamp", "mode", "crypto", "slug", "entry_num_this_window",
    "delta_at_entry", "choppiness_at_entry",
    "down_entry_price", "up_entry_price", "shares_per_leg", "total_cost",
    "down_target_price", "up_target_price",
    "down_result", "down_exit_price", "up_result", "up_exit_price",
    "outcome", "pnl_usd", "notes",
]

class TradeLogger:
    def __init__(self):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hedge_trades_log.csv")
        self.lock = threading.Lock()
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(CSV_FIELDS)

    def write(self, row: dict):
        row = {**{k: "" for k in CSV_FIELDS}, **row}
        with self.lock:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow([row[k] for k in CSV_FIELDS])

# ─── CORE BOT ────────────────────────────────────────────────────────────────
class HedgeBot:
    def __init__(self, dry_run: bool, amount: float):
        self.dry_run = dry_run
        self.amount = amount
        self.bot_name = os.getenv("BOT_NAME", "hedge_bot")
        self.mode_str = "dry_run" if dry_run else "live"
        self.stop_event = threading.Event()
        self.logger = TradeLogger()
        self.client = None
        if not dry_run:
            self._init_client()

        log("=" * 70)
        log(f"Split-Hedge Scalper | {self.mode_str.upper()} | ${amount:.2f}/entry | bot_name={self.bot_name}")
        log(f"Entry gate: |delta from price-to-beat| < ${MAX_DELTA_TO_ENTER} AND "
            f"choppiness ratio > {CHOPPINESS_RATIO_THRESHOLD} over {CHOPPINESS_LOOKBACK_SEC}s")
        log(f"Sell target: entry price + ${SELL_TARGET_PER_SHARE}/share on EACH leg | no stop-loss")
        log(f"Max {MAX_ENTRIES_PER_WINDOW} entries/window (not mandatory)")
        log(f"Trade log: {self.logger.path}")
        log("=" * 70)

    def _init_client(self):
        from py_clob_client_v2 import ClobClient, AssetType, BalanceAllowanceParams
        signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))
        self.client = ClobClient(
            host=CLOB_API, key=os.environ["POLY_PRIVATE_KEY"], chain_id=137,
            signature_type=signature_type, funder=os.environ["POLY_PROXY_WALLET"],
        )
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.client.update_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=signature_type,
        ))

        # Split is a direct smart-contract call (CTF), not a CLOB API method —
        # confirmed against Polymarket's official agent-skills documentation.
        # Needs its own web3 connection and its own USDC.e approval separate
        # from the CLOB's own allowance system.
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        self.wallet_address = Web3.to_checksum_address(os.environ["POLY_PROXY_WALLET"])
        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS), abi=CTF_ABI)
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=ERC20_ABI)
        self._ensure_ctf_approval()

    def _ensure_ctf_approval(self):
        """The CTF contract needs approval to spend USDC.e before split will
        work — confirmed as a prerequisite in Polymarket's own docs. Checks
        the real on-chain allowance first; only sends an approval transaction
        if actually needed, rather than approving on every startup."""
        try:
            current_allowance = self.usdc_contract.functions.allowance(
                self.wallet_address, Web3.to_checksum_address(CTF_CONTRACT_ADDRESS)
            ).call()
            if current_allowance > 10**12:  # already generously approved
                log("CTF contract already approved to spend USDC.e — skipping approval tx")
                return
            log("⚠️ CTF contract not yet approved for USDC.e — sending approval transaction "
                "(one-time, small gas cost on Polygon)")
            max_uint = 2**256 - 1
            approve_tx = self.usdc_contract.functions.approve(
                Web3.to_checksum_address(CTF_CONTRACT_ADDRESS), max_uint
            ).build_transaction({
                "from": self.wallet_address,
                "nonce": self.w3.eth.get_transaction_count(self.wallet_address),
                "gas": 100000,
                "gasPrice": self.w3.eth.gas_price,
            })
            signed = self.w3.eth.account.sign_transaction(approve_tx, private_key=os.environ["POLY_PRIVATE_KEY"])
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            log(f"Approval tx sent: {tx_hash.hex()} — waiting for confirmation...")
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            log("CTF approval confirmed")
        except Exception as e:
            log(f"⚠️ Could not verify/send CTF approval ({e}) — split will likely fail until this is resolved")

    # ── ENTRY SIGNAL ─────────────────────────────────────────────────────────
    def _should_enter(self, delta_from_beat: float, choppiness) -> bool:
        if abs(delta_from_beat) >= MAX_DELTA_TO_ENTER:
            return False  # strong persistent trend — the outcome-B danger zone
        if choppiness is None or choppiness < CHOPPINESS_RATIO_THRESHOLD:
            return False  # not enough evidence of genuine two-sided churn yet
        return True

    # ── SPLIT (entry) ────────────────────────────────────────────────────────
    def _split_position(self, condition_id: str, amount: float) -> dict:
        """Mints `amount` shares of BOTH Up and Down via the CTF split
        operation. Fixed conversion: $1 always -> 1 Up share + 1 Down share,
        regardless of current market odds. Verified against Polymarket's
        official CTF documentation — this is a direct call to splitPosition
        on the CTF contract, not a CLOB order."""
        if self.dry_run:
            # Split always succeeds at the fixed rate in reality — no order-book
            # risk to simulate here, unlike a normal buy.
            return {"result": "split", "shares": amount}
        try:
            from web3 import Web3
            amount_units = int(round(amount * 1_000_000))  # USDC.e uses 6 decimals
            condition_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            tx = self.ctf_contract.functions.splitPosition(
                Web3.to_checksum_address(USDC_E_ADDRESS),
                bytes.fromhex(PARENT_COLLECTION_ID.replace("0x", "")),
                condition_id_bytes,
                BINARY_PARTITION,
                amount_units,
            ).build_transaction({
                "from": self.wallet_address,
                "nonce": self.w3.eth.get_transaction_count(self.wallet_address),
                "gas": 300000,
                "gasPrice": self.w3.eth.gas_price,
            })
            signed = self.w3.eth.account.sign_transaction(tx, private_key=os.environ["POLY_PRIVATE_KEY"])
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status != 1:
                log(f"⚠️ Split transaction reverted: {tx_hash.hex()}")
                return {"result": "error", "shares": 0}
            log(f"Split confirmed: {tx_hash.hex()}")
            return {"result": "split", "shares": amount, "tx": tx_hash.hex()}
        except Exception as e:
            log(f"⚠️ Split failed: {e}")
            return {"result": "error", "shares": 0}

    # ── HEDGE MONITORING ─────────────────────────────────────────────────────
    def _place_and_monitor_hedge(self, market: dict, shares: float,
                                   down_entry: float, up_entry: float, close_ts: float) -> dict:
        down_target = round(down_entry + SELL_TARGET_PER_SHARE, 4)
        up_target = round(up_entry + SELL_TARGET_PER_SHARE, 4)
        log(f"Hedge entered: {shares} Down @ ${down_entry} (target ${down_target}) | "
            f"{shares} Up @ ${up_entry} (target ${up_target})", market["crypto"])

        down_sold = up_sold = False
        down_exit = up_exit = None

        if not self.dry_run:
            from py_clob_client_v2 import OrderArgsV2, Side, OrderType
            try:
                down_resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=market["down_token"], price=down_target, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC)
                down_order_id = down_resp.get("orderID", "")
            except Exception as e:
                log(f"⚠️ Could not place Down sell: {e}", market["crypto"])
                down_order_id = None
            try:
                up_resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=market["up_token"], price=up_target, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC)
                up_order_id = up_resp.get("orderID", "")
            except Exception as e:
                log(f"⚠️ Could not place Up sell: {e}", market["crypto"])
                up_order_id = None

        while now_unix() < close_ts and not (down_sold and up_sold):
            if self.dry_run:
                if not down_sold:
                    book = get_order_book(market["down_token"])
                    bid, size = best_bid(book)
                    if bid is not None and bid >= down_target and size >= shares:
                        down_sold, down_exit = True, down_target
                        log(f"Down leg hit target ${down_target}", market["crypto"])
                if not up_sold:
                    book = get_order_book(market["up_token"])
                    bid, size = best_bid(book)
                    if bid is not None and bid >= up_target and size >= shares:
                        up_sold, up_exit = True, up_target
                        log(f"Up leg hit target ${up_target}", market["crypto"])
            else:
                if not down_sold and down_order_id:
                    try:
                        detail = self.client.get_order(down_order_id)
                        if float(detail.get("size_matched", 0)) >= shares:
                            down_sold, down_exit = True, down_target
                            log(f"Down leg filled at ${down_target}", market["crypto"])
                    except Exception:
                        pass
                if not up_sold and up_order_id:
                    try:
                        detail = self.client.get_order(up_order_id)
                        if float(detail.get("size_matched", 0)) >= shares:
                            up_sold, up_exit = True, up_target
                            log(f"Up leg filled at ${up_target}", market["crypto"])
                    except Exception:
                        pass
            time.sleep(POLL_INTERVAL_LEG)

        # Window closing — cancel whichever resting order(s) never filled,
        # since unsold shares just resolve naturally at settlement.
        if not self.dry_run:
            from py_clob_client_v2 import OrderPayload
            if not down_sold and down_order_id:
                try:
                    self.client.cancel_order(OrderPayload(orderID=down_order_id))
                except Exception:
                    pass
            if not up_sold and up_order_id:
                try:
                    self.client.cancel_order(OrderPayload(orderID=up_order_id))
                except Exception:
                    pass

        return self._resolve_outcome(market, shares, down_entry, up_entry,
                                       down_sold, down_exit, up_sold, up_exit)

    def _resolve_outcome(self, market, shares, down_entry, up_entry,
                          down_sold, down_exit, up_sold, up_exit):
        total_cost = shares * down_entry + shares * up_entry  # for a real split this simplifies to
                                                                 # $amount total, kept explicit for clarity
        if down_sold and up_sold:
            proceeds = shares * down_exit + shares * up_exit
            pnl = round(proceeds - total_cost, 4)
            return {"outcome": "both_hit", "pnl_usd": pnl, "down_result": "sold", "up_result": "sold",
                    "down_exit_price": down_exit, "up_exit_price": up_exit,
                    "notes": "both legs hit their target"}

        # Exactly one (or neither) leg sold — need to know which side actually
        # WON the window to know if the unsold leg resolves to $0 or $1.
        symbol = SYMBOLS.get(market["crypto"])
        window_open = get_window_open_price(symbol, market["start_ts"])
        final_price = get_binance_price(symbol)
        up_won = (final_price is not None and window_open is not None and final_price > window_open)

        if not down_sold and not up_sold:
            # Outcome C: neither ever hit target — clean resolve, a wash.
            proceeds = shares * (1.0 if up_won else 0.0) + shares * (0.0 if up_won else 1.0)
            pnl = round(proceeds - total_cost, 4)
            return {"outcome": "neutral_resolve", "pnl_usd": pnl,
                    "down_result": "resolved_up_won" if up_won else "resolved_down_won",
                    "up_result": "resolved_up_won" if up_won else "resolved_down_won",
                    "down_exit_price": 1.0 if not up_won else 0.0, "up_exit_price": 1.0 if up_won else 0.0,
                    "notes": "neither leg hit target — resolved normally, a wash"}

        # Outcome B: exactly one leg sold, the other rides to resolution
        if down_sold:
            # Holding the remaining UP shares -- they resolve based on
            # whether Up actually won, not its inverse.
            other_resolve = 1.0 if up_won else 0.0
            proceeds = shares * down_exit + shares * other_resolve
            pnl = round(proceeds - total_cost, 4)
            return {"outcome": "one_hit_other_resolved", "pnl_usd": pnl,
                    "down_result": "sold", "up_result": "resolved",
                    "down_exit_price": down_exit, "up_exit_price": other_resolve,
                    "notes": f"Down sold at target, Up resolved to ${other_resolve} at settlement"}
        else:
            # Holding the remaining DOWN shares -- they resolve based on
            # whether Down actually won (the inverse of up_won).
            other_resolve = 0.0 if up_won else 1.0
            proceeds = shares * up_exit + shares * other_resolve
            pnl = round(proceeds - total_cost, 4)
            return {"outcome": "one_hit_other_resolved", "pnl_usd": pnl,
                    "down_result": "resolved", "up_result": "sold",
                    "down_exit_price": other_resolve, "up_exit_price": up_exit,
                    "notes": f"Up sold at target, Down resolved to ${other_resolve} at settlement"}

    # ── WINDOW LOOP ──────────────────────────────────────────────────────────
    def _monitor_window(self, slug_prefix: str, start_ts: int):
        crypto = MARKETS[slug_prefix]
        close_ts = start_ts + 300
        symbol = SYMBOLS.get(crypto)

        market = None
        find_deadline = now_unix() + 5
        while now_unix() < find_deadline:
            market = get_window_market(slug_prefix, start_ts)
            if market:
                break
            time.sleep(0.5)
        if not market:
            log(f"Could not find market for window starting {start_ts} — skipping", crypto)
            return

        window_open_price = get_window_open_price(symbol, start_ts) if symbol else None
        if window_open_price:
            log(f"Price to beat this window: ${window_open_price:,.2f}", crypto)
        else:
            log("Could not fetch price-to-beat — skipping entire window", crypto)
            return

        price_history = PriceHistory(CHOPPINESS_LOOKBACK_SEC)
        entries_this_window = 0

        while now_unix() < close_ts and entries_this_window < MAX_ENTRIES_PER_WINDOW:
            if self.stop_event.is_set():
                return
            current_price = get_binance_price(symbol) if symbol else None
            if current_price is None:
                time.sleep(MONITOR_INTERVAL)
                continue
            price_history.add(current_price)

            delta = current_price - window_open_price
            choppiness = price_history.choppiness_ratio()

            if not self._should_enter(delta, choppiness):
                time.sleep(MONITOR_INTERVAL)
                continue

            book_down = get_order_book(market["down_token"])
            book_up = get_order_book(market["up_token"])
            down_bid, _ = best_bid(book_down)
            up_bid, _ = best_bid(book_up)
            if down_bid is None or up_bid is None:
                time.sleep(MONITOR_INTERVAL)
                continue

            entries_this_window += 1
            choppy_str = f"{choppiness:.2f}" if choppiness not in (None, float('inf')) else "inf"
            log(f"Entry signal ({entries_this_window}/{MAX_ENTRIES_PER_WINDOW}): delta={delta:+.2f} "
                f"choppiness={choppy_str} -> splitting ${self.amount}", crypto)

            split_result = self._split_position(market["condition_id"], self.amount)
            if split_result["result"] != "split":
                time.sleep(MONITOR_INTERVAL)
                continue

            shares = split_result["shares"]
            outcome = self._place_and_monitor_hedge(market, shares, down_bid, up_bid, close_ts)

            row = {
                "timestamp": ts_str(), "mode": self.mode_str, "crypto": crypto, "slug": market["slug"],
                "entry_num_this_window": entries_this_window,
                "delta_at_entry": round(delta, 4), "choppiness_at_entry": choppy_str,
                "down_entry_price": down_bid, "up_entry_price": up_bid,
                "shares_per_leg": shares, "total_cost": round(shares * (down_bid + up_bid), 4),
                "down_target_price": round(down_bid + SELL_TARGET_PER_SHARE, 4),
                "up_target_price": round(up_bid + SELL_TARGET_PER_SHARE, 4),
                **outcome,
            }
            self.logger.write(row)
            sign = "+" if outcome["pnl_usd"] >= 0 else ""
            log(f"RECORDED: {outcome['outcome']} | pnl={sign}${outcome['pnl_usd']}", crypto)
            time.sleep(MONITOR_INTERVAL)

    def _asset_loop(self, slug_prefix: str):
        crypto = MARKETS[slug_prefix]
        next_start_ts = None
        while not self.stop_event.is_set():
            if next_start_ts is None:
                start_ts = next_window_start(now_unix())
            else:
                start_ts = next_start_ts
                if now_unix() > start_ts + 30:
                    log(f"⚠️ Running behind schedule — re-syncing to the current window", crypto)
                    start_ts = next_window_start(now_unix())
            while now_unix() < start_ts and not self.stop_event.is_set():
                time.sleep(1)
            if self.stop_event.is_set():
                break
            log(f"Monitoring window starting {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC", crypto)
            try:
                self._monitor_window(slug_prefix, start_ts)
            except Exception as e:
                log(f"⚠️ Unhandled error this window: {e}", crypto)
            next_start_ts = start_ts + 300

    def run(self):
        threads = [threading.Thread(target=self._asset_loop, args=(prefix,), daemon=True) for prefix in MARKETS]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("Stopping...")
            self.stop_event.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Split-Hedge Scalper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--amount", type=float, default=SPLIT_AMOUNT_USD)
    args = parser.parse_args()

    bot = HedgeBot(dry_run=args.dry_run, amount=args.amount)
    bot.run()
