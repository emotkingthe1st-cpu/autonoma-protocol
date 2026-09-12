import os
import sys
import logging
import asyncio
from typing import Dict, Any, Optional, List, Set

from aiohttp import web, ClientSession, ClientTimeout, ClientError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Primary, Secondary, Tertiary RPC Fallback Chain
RPC_ENDPOINTS: List[str] = [
    "https://solana-rpc.publicnode.com",
    "https://1rpc.io/solana",
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]

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
        logger.warning(f"DexScreener API fetch warning: {e}")

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
    dex_pair_address: Optional[str] = None
) -> Optional[float]:
    """
    Calls getTokenLargestAccounts to retrieve largest token accounts,
    filters out DexScreener pairAddress and known Raydium/Orca/Pump.fun LP pools,
    and calculates top 10 non-LP concentration against total supply:
      top10_pct = (top_10_non_lp_sum / total_supply) * 100
    """
    if total_supply_raw <= 0:
        return None

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

    endpoints = list(RPC_ENDPOINTS)
    custom_rpc = os.environ.get("SOLANA_RPC")
    if custom_rpc and custom_rpc not in endpoints:
        endpoints.insert(0, custom_rpc)

    for endpoint in endpoints:
        try:
            async with session.post(endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    continue

                data = await resp.json()
                if not data or "result" not in data:
                    continue

                value = data["result"].get("value")
                if not isinstance(value, list) or len(value) == 0:
                    continue

                candidate_addresses = []
                for item in value:
                    addr = item.get("address")
                    try:
                        amount = int(item.get("amount", 0))
                    except (ValueError, TypeError):
                        amount = 0
                    if addr and amount > 0:
                        candidate_addresses.append((addr, amount))

                if not candidate_addresses:
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
                            multi_val = multi_data.get("result", {}).get("value", [])
                            for idx, acc_info in enumerate(multi_val):
                                addr, amt = candidate_addresses[idx]
                                is_lp = False
                                if addr in lp_filter_set:
                                    is_lp = True
                                elif acc_info and isinstance(acc_info, dict):
                                    owner = acc_info.get("owner", "")
                                    parsed_owner = (
                                        acc_info.get("data", {})
                                        .get("parsed", {})
                                        .get("info", {})
                                        .get("owner", "")
                                    )
                                    if owner in lp_filter_set or parsed_owner in lp_filter_set:
                                        is_lp = True

                                if not is_lp:
                                    non_lp_amounts.append(amt)
                        else:
                            non_lp_amounts = [amt for addr, amt in candidate_addresses if addr not in lp_filter_set]
                except Exception:
                    non_lp_amounts = [amt for addr, amt in candidate_addresses if addr not in lp_filter_set]

                top10_sum = sum(non_lp_amounts[:10])
                pct = (top10_sum / float(total_supply_raw)) * 100.0
                return round(pct, 1)

        except Exception as e:
            logger.warning(f"getTokenLargestAccounts warning for endpoint {endpoint}: {e}")
            continue

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

    endpoints = list(RPC_ENDPOINTS)
    custom_rpc = os.environ.get("SOLANA_RPC")
    if custom_rpc and custom_rpc not in endpoints:
        endpoints.insert(0, custom_rpc)

    for endpoint in endpoints:
        try:
            logger.debug(f"Querying RPC node: {endpoint}")
            async with session.post(endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning(f"RPC {endpoint} returned HTTP status {resp.status}")
                    continue

                data = await resp.json()
                if not data or "result" not in data:
                    logger.warning(f"RPC {endpoint} returned unexpected body format")
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
                try:
                    total_supply_raw = int(supply_raw_str)
                except (ValueError, TypeError):
                    total_supply_raw = 0

                top10_pct = await fetch_token_largest_accounts(
                    session,
                    mint_address,
                    total_supply_raw,
                    dex_pair_address
                )

                return {
                    "success": True,
                    "found": True,
                    "endpoint": endpoint,
                    "is_token_2022": is_token_2022,
                    "mint_auth": mint_auth,
                    "freeze_auth": freeze_auth,
                    "total_supply_raw": total_supply_raw,
                    "top10_pct": top10_pct
                }

        except (asyncio.TimeoutError, ClientError) as e:
            logger.warning(f"RPC {endpoint} timeout or network error: {e}")
            continue
        except Exception as e:
            logger.warning(f"RPC {endpoint} unexpected exception: {e}")
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

    warning_block = ""

    # HCS v1.5 Scoring & Verdict Matrix
    if not authorities_clean:
        grade_str = "⚠️ <b>GRADE: D- (HIGH CENTRALIZATION RISK)</b>"
        verdict_str = "🔴 <b>DISCRETIONARY RISK DETECTED</b>"
        warning_block = "\n⚠️ <i>Administrative backdoor retained by creator.</i>"
        if top10_pct is not None:
            holder_label = f"{top10_pct:.1f}%"
        else:
            holder_label = "N/A"
    elif is_established and top10_pct is not None and top10_pct > 45.0:
        grade_str = "🏆 <b>GRADE: A (ESTABLISHED / CEX DEPTH)</b>"
        verdict_str = "🟢 <b>INSTITUTIONAL SOVEREIGNTY</b>"
        holder_label = f"{top10_pct:.1f}% (Exchange & Custody Depth)"
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
            holder_label = "N/A"

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
    """Global exception handler for python-telegram-bot to prevent crashes."""
    logger.error(f"Unhandled exception caught by global handler: {context.error}", exc_info=context.error)

# ------------------------------------------------------------------------------
# RENDER HEALTH CHECK WEBSERVER & APPLICATION LIFECYCLE
# ------------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    """
    Lifecycle hook called when Telegram Application is initialized.
    Sets up shared aiohttp ClientSession and starts aiohttp web server for Render health checks.
    """
    # 1. Initialize persistent ClientSession for async RPC requests
    session = ClientSession()
    application.bot_data["http_session"] = session

    # 2. Setup aiohttp web server for Render HTTP health check on PORT
    web_app = web.Application()

    async def handle_health_check(request: web.Request) -> web.Response:
        return web.Response(text="AUTONOMA HCS SCANNER ACTIVE", status=200, content_type="text/plain")

    web_app.router.add_get("/", handle_health_check)
    web_app.router.add_get("/health", handle_health_check)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    application.bot_data["web_runner"] = runner
    logger.info(f"🌐 Render Health Check HTTP server running on port {PORT}")


async def post_shutdown(application: Application) -> None:
    """
    Lifecycle hook called when Telegram Application shuts down.
    Cleans up persistent ClientSession and aiohttp web server runner.
    """
    session: Optional[ClientSession] = application.bot_data.get("http_session")
    if session and not session.closed:
        await session.close()
        logger.info("Closed aiohttp ClientSession")

    runner: Optional[web.AppRunner] = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()
        logger.info("Cleaned up web server runner")

# ------------------------------------------------------------------------------
# MAIN EXECUTION
# ------------------------------------------------------------------------------
def main():
    logger.info("🛡️ Starting AUTONOMA HCS Scanner Engine...")
    
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is not set!")
        sys.exit(1)

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("scan", scan_handler))
    
    # Register global error handler
    application.add_error_handler(global_error_handler)

    # Start non-blocking Telegram long polling loop
    logger.info("Bot starting polling loop...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
