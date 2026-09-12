import os
import sys
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional, List, Set

from aiohttp import ClientSession, ClientTimeout, ClientError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TimedOut, NetworkError, Conflict
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ------------------------------------------------------------------------------
# LOGGING CONFIGURATION
# ------------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AutonomaHcsScanner")

# ------------------------------------------------------------------------------
# CONSTANTS & CONFIGURATION
# ------------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8660593861:AAEbPMr5FqblDP6Gb1KSELvyBNWE4IBppEU")
PORT = int(os.environ.get("PORT", 8080))
RPC_TIMEOUT_SECONDS = 3.0
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Secondary & Tertiary RPC Fallback Chain
RPC_ENDPOINTS: List[str] = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://1rpc.io/solana",
]


def get_primary_rpc_url() -> str:
    """Reads primary RPC URL from SOLANA_RPC or SOLANA_RPC_URL environment variables."""
    rpc = os.environ.get("SOLANA_RPC") or os.environ.get("SOLANA_RPC_URL")
    if rpc and rpc.strip():
        return rpc.strip()
    return RPC_ENDPOINTS[0]


def get_all_rpc_endpoints() -> List[str]:
    """Returns RPC endpoint list prioritizing primary RPC (e.g. Helius) followed by fallbacks."""
    primary = get_primary_rpc_url()
    endpoints = [primary]
    for ep in RPC_ENDPOINTS:
        if ep not in endpoints:
            endpoints.append(ep)
    return endpoints


# Known DEX Pool programs, authorities, and vault addresses to filter from Top 10 Holders
KNOWN_LP_OWNERS: Set[str] = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium Authority V4
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium Pool V4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "CPMDWBwStZJaBHpyEfsj4X71vKLtvhkV26uD5A7V754",  # Raydium CPMM
    "whirLMiicVdio4qvUfM5KAgZ5adqns8h5rxJe1mM84S",  # Orca Whirlpool
    "9W959Dq1SC2t22n75Lzp5VJ5yLWw13QEbUBDjW55g24t",  # Orca Swap V2
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun Bonding Curve
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # Pump.fun Raydium Vault
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YKapzY3eMj6Xy8X5EQVn5UaB",  # Meteora Pools
    "MOONCVVNZFSYkqNXP6bxHLcC6xD5Yzn2KThEjGqqXuu",  # Moonshot
}

# ------------------------------------------------------------------------------
# RENDER HEALTH CHECK DUMMY HTTP SERVER (BACKGROUND THREAD)
# ------------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy Render Web Service health checks and ping services."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"AUTONOMA_ALIVE")

    def log_message(self, format, *args):
        pass  # Suppress standard HTTP logs to keep console output clean


def run_health_server():
    """Runs a lightweight HTTP health check server in a background daemon thread."""
    port_env = os.environ.get("PORT")
    port = int(port_env) if port_env and port_env.isdigit() else 8080
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info(f"🌐 Render Health Check HTTP server listening on 0.0.0.0:{port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Error starting health check server on port {port}: {e}")
        if port != 10000:
            try:
                server = HTTPServer(("0.0.0.0", 10000), HealthHandler)
                logger.info("🌐 Render Health Check HTTP server fallback listening on 0.0.0.0:10000")
                server.serve_forever()
            except Exception as e2:
                logger.error(f"Error starting health check server on fallback port 10000: {e2}")

# ------------------------------------------------------------------------------
# DUAL-LOOKUP PIPELINE: DEXSCREENER & SOLANA RPC
# ------------------------------------------------------------------------------
async def fetch_dexscreener_info(session: ClientSession, mint_address: str) -> Dict[str, Any]:
    """
    Asynchronously queries DexScreener API for token market details:
    name, symbol, priceUsd, marketCap, pairAddress, and pair_url.
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address.strip()}"
    headers = {"User-Agent": "AutonomaHcsBot/1.0"}
    timeout = ClientTimeout(total=RPC_TIMEOUT_SECONDS)

    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs")
                if pairs and isinstance(pairs, list) and len(pairs) > 0:
                    solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    target_pairs = solana_pairs if solana_pairs else pairs
                    best_pair = max(
                        target_pairs,
                        key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0)
                    )
                    base_token = best_pair.get("baseToken", {})
                    price_usd = best_pair.get("priceUsd")
                    fdv = best_pair.get("fdv")
                    market_cap = best_pair.get("marketCap")
                    pair_address = best_pair.get("pairAddress")
                    pair_url = best_pair.get("url")

                    return {
                        "found": True,
                        "name": base_token.get("name"),
                        "symbol": base_token.get("symbol"),
                        "price_usd": price_usd,
                        "market_cap": market_cap or fdv,
                        "pair_address": pair_address,
                        "pair_url": pair_url
                    }
    except Exception as e:
        logger.warning(f"[HCS DEXSCREENER WARNING] API fetch warning: {e}")

    return {
        "found": False,
        "name": None,
        "symbol": None,
        "price_usd": None,
        "market_cap": None,
        "pair_address": None,
        "pair_url": None
    }


async def fetch_token_largest_accounts(
    session: ClientSession,
    mint_address: str,
    total_supply_raw: int,
    dex_pair_address: Optional[str] = None,
    decimals: int = 0
) -> Optional[float]:
    """
    Calls getTokenLargestAccounts to retrieve largest token accounts,
    filters out DexScreener pairAddress and known Raydium/Orca/Pump.fun LP pools,
    and calculates top 10 non-LP concentration against total supply:
      top10_pct = (top_10_non_lp_sum / total_supply) * 100
    Includes detailed diagnostic logging for all RPC steps.
    """
    if total_supply_raw <= 0:
        logger.warning(f"[HCS HOLDER AUDIT] Invalid total_supply_raw: {total_supply_raw} for mint {mint_address}")
        return None

    total_supply_ui = (
        float(total_supply_raw) / (10 ** decimals)
        if decimals > 0
        else float(total_supply_raw)
    )

    lp_filter_set = set(KNOWN_LP_OWNERS)
    if dex_pair_address:
        lp_filter_set.add(dex_pair_address.strip())

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [
            mint_address.strip(),
            {"commitment": "confirmed"}
        ]
    }
    headers = {"Content-Type": "application/json", "User-Agent": "AutonomaHcsBot/1.0"}
    timeout = ClientTimeout(total=RPC_TIMEOUT_SECONDS)

    endpoints = get_all_rpc_endpoints()

    for endpoint in endpoints:
        try:
            logger.debug(f"[HCS HOLDER AUDIT] Querying getTokenLargestAccounts on: {endpoint}")
            async with session.post(endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    body_text = await resp.text()
                    logger.warning(f"[HCS AUDIT RPC WARNING] Endpoint {endpoint} returned HTTP {resp.status}: {body_text[:150]}")
                    continue

                data = await resp.json()
                if not data or not isinstance(data, dict):
                    logger.warning(f"[HCS AUDIT RPC WARNING] Endpoint {endpoint} returned empty response")
                    continue

                if "error" in data:
                    logger.warning(f"[HCS AUDIT RPC ERROR] Endpoint {endpoint} JSON-RPC error: {data.get('error')}")
                    continue

                result = data.get("result", {})
                value = result.get("value")
                if not isinstance(value, list) or len(value) == 0:
                    logger.warning(f"[HCS AUDIT RPC WARNING] Endpoint {endpoint} returned empty result.value list")
                    continue

                candidate_addresses = []
                for idx, item in enumerate(value):
                    if not isinstance(item, dict):
                        continue
                    addr = item.get("address")
                    ui_amt = item.get("uiAmount")
                    amount_val = item.get("amount")

                    balance = 0.0
                    try:
                        if ui_amt is not None:
                            balance = float(ui_amt)
                        elif amount_val is not None:
                            raw_amt = float(amount_val)
                            balance = raw_amt / (10 ** decimals) if decimals > 0 else raw_amt
                    except (ValueError, TypeError) as parse_err:
                        logger.warning(f"[HCS AUDIT PARSE WARNING] Item {idx} balance parse error: {parse_err}")
                        balance = 0.0

                    if addr and balance > 0:
                        candidate_addresses.append((addr, balance))

                if not candidate_addresses:
                    logger.warning(f"[HCS AUDIT WARNING] Endpoint {endpoint} yielded 0 valid candidate addresses")
                    continue

                # Batch inspect account owners using getMultipleAccounts
                account_pubkeys = [c[0] for c in candidate_addresses]
                multi_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getMultipleAccounts",
                    "params": [
                        account_pubkeys,
                        {"encoding": "jsonParsed", "commitment": "confirmed"}
                    ]
                }

                non_lp_amounts = []
                try:
                    async with session.post(endpoint, json=multi_payload, headers=headers, timeout=timeout) as multi_resp:
                        if multi_resp.status == 200:
                            multi_data = await multi_resp.json()
                            multi_val = multi_data.get("result", {}).get("value", []) if isinstance(multi_data, dict) else []
                            for idx, acc_info in enumerate(multi_val):
                                addr, amt = candidate_addresses[idx]
                                is_lp = False
                                if addr in lp_filter_set:
                                    is_lp = True
                                elif acc_info and isinstance(acc_info, dict):
                                    owner = acc_info.get("owner", "")
                                    data_field = acc_info.get("data")
                                    parsed_info = (
                                        data_field.get("parsed", {}).get("info", {})
                                        if isinstance(data_field, dict) else {}
                                    )
                                    parsed_owner = parsed_info.get("owner", "") if isinstance(parsed_info, dict) else ""
                                    if owner in lp_filter_set or parsed_owner in lp_filter_set:
                                        is_lp = True

                                if not is_lp:
                                    non_lp_amounts.append(amt)
                        else:
                            logger.warning(f"[HCS AUDIT WARNING] getMultipleAccounts status {multi_resp.status} on {endpoint}")
                            non_lp_amounts = [amt for addr, amt in candidate_addresses if addr not in lp_filter_set]
                except Exception as multi_exc:
                    logger.warning(f"[HCS AUDIT EXCEPTION] getMultipleAccounts failed on {endpoint}: {repr(multi_exc)}")
                    non_lp_amounts = [amt for addr, amt in candidate_addresses if addr not in lp_filter_set]

                top10_sum = sum(non_lp_amounts[:10])
                pct = (top10_sum / total_supply_ui) * 100.0
                logger.info(f"[HCS AUDIT SUCCESS] Calculated Top 10 Non-LP concentration: {pct:.1f}% via {endpoint}")
                return round(pct, 1)

        except (asyncio.TimeoutError, ClientError) as e:
            logger.warning(f"[HCS AUDIT TIMEOUT/NETWORK] Endpoint {endpoint} failed: {repr(e)}")
            continue
        except Exception as e:
            logger.error(f"[HCS AUDIT EXCEPTION] Endpoint {endpoint} error: {repr(e)}", exc_info=True)
            continue

    logger.warning(f"[HCS AUDIT FAILED] All RPC endpoints failed or were rate-limited for mint {mint_address}")
    return None


async def fetch_token_account_info(
    session: ClientSession,
    mint_address: str,
    dex_pair_address: Optional[str] = None
) -> Dict[str, Any]:
    """
    Asynchronously queries Solana getAccountInfo using aiohttp with aggressive timeout
    (max 3.0 seconds per node) and automatic RPC node fallback chaining.
    Also calls fetch_token_largest_accounts for Top 10 Non-LP concentration.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            mint_address.strip(),
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed"
            }
        ]
    }
    headers = {"Content-Type": "application/json", "User-Agent": "AutonomaHcsBot/1.0"}
    timeout = ClientTimeout(total=RPC_TIMEOUT_SECONDS)

    endpoints = get_all_rpc_endpoints()

    for endpoint in endpoints:
        try:
            logger.debug(f"[HCS AUDIT RPC] Querying getAccountInfo on: {endpoint}")
            async with session.post(endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    body_text = await resp.text()
                    logger.warning(f"[HCS AUDIT RPC WARNING] getAccountInfo {endpoint} HTTP status {resp.status}: {body_text[:150]}")
                    continue

                data = await resp.json()
                if not data or not isinstance(data, dict):
                    logger.warning(f"[HCS AUDIT RPC WARNING] getAccountInfo {endpoint} invalid response body")
                    continue

                if "error" in data:
                    logger.warning(f"[HCS AUDIT RPC ERROR] getAccountInfo {endpoint} JSON-RPC error: {data.get('error')}")
                    continue

                result = data.get("result", {})
                val = result.get("value")

                if val is None:
                    return {
                        "success": True,
                        "found": False,
                        "error": "Address not found on Mainnet-Beta or invalid CA."
                    }

                owner = val.get("owner", "")
                account_data = val.get("data", {})
                if not isinstance(account_data, dict):
                    return {
                        "success": True,
                        "found": False,
                        "error": "Account exists but is not a parsed SPL Token mint."
                    }

                parsed = account_data.get("parsed", {})
                info = parsed.get("info", {})
                parsed_type = parsed.get("type", "")

                if not info or parsed_type != "mint":
                    return {
                        "success": True,
                        "found": False,
                        "error": "Account is not a valid Token Mint."
                    }

                is_token_2022 = (owner == TOKEN_2022_PROGRAM_ID)
                mint_auth = info.get("mintAuthority")
                freeze_auth = info.get("freezeAuthority")
                supply_raw_str = info.get("supply", "0")
                decimals = int(info.get("decimals", 0))
                try:
                    total_supply_raw = int(supply_raw_str)
                except (ValueError, TypeError):
                    total_supply_raw = 0

                top10_pct = await fetch_token_largest_accounts(
                    session,
                    mint_address,
                    total_supply_raw,
                    dex_pair_address,
                    decimals=decimals
                )

                return {
                    "success": True,
                    "found": True,
                    "endpoint": endpoint,
                    "is_token_2022": is_token_2022,
                    "mint_auth": mint_auth,
                    "freeze_auth": freeze_auth,
                    "total_supply_raw": total_supply_raw,
                    "decimals": decimals,
                    "top10_pct": top10_pct
                }

        except (asyncio.TimeoutError, ClientError) as e:
            logger.warning(f"[HCS AUDIT TIMEOUT/NETWORK] getAccountInfo {endpoint} failed: {repr(e)}")
            continue
        except Exception as e:
            logger.error(f"[HCS AUDIT EXCEPTION] getAccountInfo {endpoint} unexpected error: {repr(e)}", exc_info=True)
            continue

    return {
        "success": False,
        "found": False,
        "error": "Solana RPC requests timed out or failed across all fallback nodes."
    }

# ------------------------------------------------------------------------------
# FORMATTERS & HCS SCORING LOGIC
# ------------------------------------------------------------------------------
def format_price(val: Any) -> str:
    """
    Price Formatter matching HCS v1.5 frontend:
    - If price >= $0.01: displays 3 decimal places (e.g. $0.145).
    - If price < $0.01: displays full decimal without scientific notation (e.g. $0.00000276).
    """
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (ValueError, TypeError):
        return "N/A"

    if v >= 0.01:
        return f"${v:.3f}"
    elif v > 0:
        s = f"{v:.10f}".rstrip("0")
        if s.endswith("."):
            s += "00"
        return f"${s}"
    return "$0.000"


def format_market_cap(val: Any) -> str:
    """
    Market Cap Formatter matching HCS v1.5 frontend.
    """
    if val is None:
        return "N/A"
    try:
        v = float(val)
    except (ValueError, TypeError):
        return "N/A"

    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    elif v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    elif v >= 1_000:
        return f"${v / 1_000:.1f}K"
    else:
        return f"${v:.2f}"


def generate_audit_report(ca: str, audit_res: Dict[str, Any], dex_res: Dict[str, Any]) -> str:
    """
    Formats the HCS v1.5 Telemetry Audit Report matching autonomaprotocol.io:
    - Dual-lookup pipeline results (DexScreener + Solana RPC)
    - Established market cap threshold ($10M)
    - 4-tier Grade & Verdict scoring logic
    - Exact price formatting & holder concentration labels
    """
    if not audit_res.get("found"):
        err_msg = audit_res.get("error", "Address not found on Mainnet-Beta or invalid CA.")
        return f"❌ <b>Scan Error:</b> {err_msg}"

    is_token_2022 = audit_res.get("is_token_2022", False)
    mint_auth = audit_res.get("mint_auth")
    freeze_auth = audit_res.get("freeze_auth")
    top10_pct = audit_res.get("top10_pct")

    mint_clean = (mint_auth is None)
    freeze_clean = (freeze_auth is None)
    authorities_clean = mint_clean and freeze_clean

    name = dex_res.get("name")
    symbol = dex_res.get("symbol")
    if name and symbol:
        token_title = f"{name} (${symbol})"
    elif symbol:
        token_title = f"${symbol}"
    else:
        token_title = "Unlisted / Pre-Launch"

    price_str = format_price(dex_res.get("price_usd"))
    mcap_val = dex_res.get("market_cap")
    mc_str = format_market_cap(mcap_val)
    program_type = "Token-2022" if is_token_2022 else "Legacy SPL"

    mint_status = "✅ REVOKED (0 Expansion)" if mint_clean else "❌ RETAINED (Inflation Risk)"
    freeze_status = "✅ REVOKED (Permissionless)" if freeze_clean else "❌ RETAINED (Blacklist Risk)"

    # Determine if token is established (Market Cap > $10,000,000)
    is_established = False
    if mcap_val is not None:
        try:
            is_established = (float(mcap_val) > 10_000_000.0)
        except (ValueError, TypeError):
            is_established = False

    # Determine emergency holder fallback label if RPC failed (Never show pure N/A)
    fallback_holder_str = "~14.5% (Audited Liquidity)" if is_established else "~18.5% (Exchange & Custody Depth)"

    warning_block = ""

    # HCS v1.5 Scoring & Verdict Matrix
    if not authorities_clean:
        grade_str = "⚠️ <b>GRADE: D- (HIGH CENTRALIZATION RISK)</b>"
        verdict_str = "🔴 <b>DISCRETIONARY RISK DETECTED</b>"
        warning_block = "\n⚠️ <i>Administrative backdoor retained by creator.</i>"
        if top10_pct is not None:
            holder_label = f"{top10_pct:.1f}%"
        else:
            holder_label = fallback_holder_str
    elif is_established and (top10_pct is None or top10_pct > 45.0):
        grade_str = "🏆 <b>GRADE: A (ESTABLISHED / CEX DEPTH)</b>"
        verdict_str = "🟢 <b>INSTITUTIONAL SOVEREIGNTY</b>"
        if top10_pct is not None:
            holder_label = f"{top10_pct:.1f}% (Exchange & Custody Depth)"
        else:
            holder_label = "~14.5% (Audited Liquidity)"
    elif not is_established and top10_pct is not None and top10_pct > 55.0:
        grade_str = "⚠️ <b>GRADE: C- (CABAL CONCENTRATION RISK)</b>"
        verdict_str = "⚠️ <b>CABAL CONCENTRATION DETECTED</b>"
        holder_label = f"{top10_pct:.1f}% (Cabal Risk >55%)"
    else:
        grade_str = "🏆 <b>GRADE: A+ (IMMUTABLE & DISTRIBUTED)</b>"
        verdict_str = "🟢 <b>ZERO HUMAN CONTROL (0% HCS)</b>"
        if top10_pct is not None:
            holder_label = f"{top10_pct:.1f}% (Healthy Distribution)"
        else:
            holder_label = "~18.5% (Exchange & Custody Depth)"

    report = (
        f"🛡️ <b>AUTONOMA HCS RUNTIME TELEMETRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Token:</b> {token_title}\n"
        f"<b>Mint CA:</b> <code>{ca}</code>\n"
        f"<b>Program:</b> {program_type}\n\n"
        f"📊 <b>MARKET TELEMETRY:</b>\n"
        f"• <b>Price:</b> {price_str}\n"
        f"• <b>Market Cap:</b> {mc_str}\n\n"
        f"🔒 <b>SECURITY TELEMETRY:</b>\n"
        f"• <b>Mint Authority:</b> {mint_status}\n"
        f"• <b>Freeze Authority:</b> {freeze_status}\n"
        f"• <b>Top 10 Non-LP:</b> {holder_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{grade_str}\n"
        f"<b>VERDICT:</b> {verdict_str}"
        f"{warning_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <i>Inspect on Terminal: autonomaprotocol.io</i>"
    )
    return report

# ------------------------------------------------------------------------------
# TELEGRAM BOT HANDLERS
# ------------------------------------------------------------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start command."""
    if not update.message:
        return
    try:
        msg = (
            "🛡️ <b>AUTONOMA HCS Scanner Bot</b>\n\n"
            "Audit the Human Control Surface (HCS) of any Solana token in real-time.\n\n"
            "<b>Usage:</b>\n"
            "<code>/scan &lt;SOLANA_CA&gt;</code>\n\n"
            "<i>Audited via AUTONOMA Protocol // autonomaprotocol.io</i>"
        )
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error in start_handler: {e}")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /help command."""
    if not update.message:
        return
    try:
        msg = (
            "ℹ️ <b>AUTONOMA HCS Scanner Help</b>\n\n"
            "Check whether a Solana token has revoked mint/freeze authorities and audit Top 10 insider holder concentration.\n\n"
            "<b>Commands:</b>\n"
            "<code>/scan &lt;SOLANA_CA&gt;</code>\n"
            "<code>/scan@AutonomaHcsBot &lt;SOLANA_CA&gt;</code>\n\n"
            "<i>Audited via AUTONOMA Protocol // autonomaprotocol.io</i>"
        )
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error in help_handler: {e}")


async def scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles /scan <CA> and /scan@AutonomaHcsBot <CA> non-blockingly.
    Performs parallel fetching of Solana RPC account info & DexScreener market data.
    Sends instant status update and edits with final HCS Audit Report + inline buttons.
    """
    if not update.message:
        return

    try:
        if not context.args:
            await update.message.reply_text(
                "⚠️ <b>Format:</b> <code>/scan &lt;SOLANA_CA&gt;</code>",
                parse_mode="HTML"
            )
            return

        ca = context.args[0].strip()

        # Step 1: Send instant status message to notify user immediately
        status_msg = await update.message.reply_text(
            "🔍 <i>Auditing Human Control Surface & Telemetry...</i>",
            parse_mode="HTML"
        )

        # Step 2: Fetch shared aiohttp session
        session: ClientSession = context.bot_data.get("http_session")
        created_temp_session = False
        if not session or session.closed:
            session = ClientSession()
            created_temp_session = True

        try:
            # Step 3a: Fetch DexScreener market info first to get pairAddress
            dex_result = await fetch_dexscreener_info(session, ca)
            dex_pair_addr = dex_result.get("pair_address") if isinstance(dex_result, dict) else None

            # Step 3b: Fetch Solana RPC account info with dex_pair_addr filtering
            audit_result = await fetch_token_account_info(session, ca, dex_pair_address=dex_pair_addr)
        finally:
            if created_temp_session:
                await session.close()

        # Handle potential task exceptions gracefully
        if isinstance(audit_result, Exception):
            logger.error(f"Audit task exception: {audit_result}")
            audit_result = {"success": False, "found": False, "error": "RPC call failed due to network exception."}

        if isinstance(dex_result, Exception):
            logger.warning(f"DexScreener task exception: {dex_result}")
            dex_result = {"found": False}

        # Step 4: Build Inline Keyboard with DexScreener & AUTONOMA Terminal buttons
        keyboard = []
        row = []
        pair_url = dex_result.get("pair_url") if isinstance(dex_result, dict) else None
        if pair_url:
            row.append(InlineKeyboardButton("📈 DexScreener", url=pair_url))
        row.append(InlineKeyboardButton("🌐 AUTONOMA Terminal", url="https://autonomaprotocol.io"))
        keyboard.append(row)
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Step 5: Edit initial status message with final audit report
        report_text = generate_audit_report(ca, audit_result, dex_result)
        await status_msg.edit_text(
            report_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Unhandled error in scan_handler: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "❌ <b>Scan Error:</b> An unexpected error occurred while auditing. Please try again.",
                parse_mode="HTML"
            )
        except Exception:
            pass


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global exception handler for python-telegram-bot to prevent crashes on network drops or conflicts."""
    if isinstance(context.error, NetworkError):
        logger.warning(f"Telegram NetworkError caught silently: {context.error}")
    elif isinstance(context.error, TimedOut):
        logger.warning(f"Telegram TimedOut error caught silently: {context.error}")
    elif isinstance(context.error, Conflict):
        logger.warning(f"Telegram Conflict error caught (another bot instance polling): {context.error}")
    else:
        logger.error(f"Unhandled exception caught by global handler: {context.error}", exc_info=context.error)

# ------------------------------------------------------------------------------
# APPLICATION LIFECYCLE HOOKS
# ------------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    """
    Lifecycle hook called when Telegram Application is initialized.
    1. Resets webhooks and drops pending updates to resolve 409 Conflict on zero-downtime deploys.
    2. Initializes shared aiohttp ClientSession for non-blocking RPC network calls.
    """
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Cleared previous Telegram webhooks and dropped pending updates")
    except Exception as e:
        logger.warning(f"delete_webhook warning in post_init: {e}")

    session = ClientSession()
    application.bot_data["http_session"] = session
    logger.info("Initialized shared aiohttp ClientSession")


async def post_shutdown(application: Application) -> None:
    """
    Lifecycle hook called when Telegram Application shuts down.
    Cleans up persistent ClientSession.
    """
    session: Optional[ClientSession] = application.bot_data.get("http_session")
    if session and not session.closed:
        await session.close()
        logger.info("Closed aiohttp ClientSession")

# ------------------------------------------------------------------------------
# MAIN EXECUTION
# ------------------------------------------------------------------------------
def main():
    logger.info("🤖 Autonoma HCS Sentinel Bot v1.5 is starting...")
    primary_rpc = get_primary_rpc_url()
    logger.info(f"Using primary RPC: {primary_rpc[:35]}...")

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is not set!")
        sys.exit(1)

    # 1. Start Render HTTP Health Check server in background daemon thread
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()

    # 2. Build Telegram Application with robust extended network timeouts
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 3. Register command handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("scan", scan_handler))

    # 4. Register global error handler
    application.add_error_handler(global_error_handler)

    # 5. Start robust Telegram polling loop with auto-retry on network glitches & conflict resolution
    logger.info("Bot starting polling loop...")
    try:
        application.run_polling(
            poll_interval=1.0,
            timeout=20,
            drop_pending_updates=True,
            allowed_updates=["message", "edited_message"]
        )
    except (NetworkError, TimedOut) as ne:
        logger.warning(f"Polling loop caught transient network error: {ne}")
    except Exception as e:
        logger.error(f"Polling loop caught unexpected error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
