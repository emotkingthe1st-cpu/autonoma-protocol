import os
import sys
import logging
import asyncio
from typing import Dict, Any, Optional, List

from aiohttp import web, ClientSession, ClientTimeout, ClientError
from telegram import Update
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
RPC_TIMEOUT_SECONDS = 2.5
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Primary, Secondary, Tertiary RPC Fallback Chain
RPC_ENDPOINTS: List[str] = [
    "https://solana-rpc.publicnode.com",
    "https://1rpc.io/solana",
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]

# ------------------------------------------------------------------------------
# ASYNC SOLANA RPC TOKEN AUDIT ENGINE
# ------------------------------------------------------------------------------
async def fetch_token_account_info(session: ClientSession, mint_address: str) -> Dict[str, Any]:
    """
    Asynchronously queries Solana getAccountInfo using aiohttp with aggressive timeout
    (max 2.5 seconds per node) and automatic RPC node fallback chaining.
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

                return {
                    "success": True,
                    "found": True,
                    "endpoint": endpoint,
                    "is_token_2022": is_token_2022,
                    "mint_auth": mint_auth,
                    "freeze_auth": freeze_auth
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


def generate_audit_report(ca: str, audit_res: Dict[str, Any]) -> str:
    """
    Formats the HCS Audit Report as clean HTML for Telegram display.
    """
    if not audit_res.get("found"):
        err_msg = audit_res.get("error", "Address not found on Mainnet-Beta or invalid CA.")
        return f"❌ <b>Scan Error:</b> {err_msg}"

    is_token_2022 = audit_res.get("is_token_2022", False)
    mint_auth = audit_res.get("mint_auth")
    freeze_auth = audit_res.get("freeze_auth")

    is_clean = (mint_auth is None) and (freeze_auth is None)
    program_type = "Token-2022" if is_token_2022 else "Legacy SPL"

    if is_clean:
        verdict = "🟢 <b>ZERO HUMAN CONTROL (0% HCS)</b>"
    else:
        verdict = "🔴 <b>DISCRETIONARY RISK DETECTED</b>"

    mint_status = "✅ REVOKED (0 Expansion)" if mint_auth is None else "❌ RETAINED (Inflation Risk)"
    freeze_status = "✅ REVOKED (Permissionless)" if freeze_auth is None else "❌ RETAINED (Blacklist Risk)"

    report = (
        f"🛡️ <b>AUTONOMA HCS RUNTIME REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Target CA:</b> <code>{ca}</code>\n"
        f"<b>Program:</b> {program_type}\n\n"
        f"• <b>Mint Authority:</b> {mint_status}\n"
        f"• <b>Freeze Authority:</b> {freeze_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>VERDICT:</b> {verdict}\n\n"
        f"<i>Audited via AUTONOMA Protocol // autonomaprotocol.io</i>"
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
            "Check whether a Solana token has revoked mint and freeze authorities.\n\n"
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
    Sends instant temporary status message and edits it when RPC results are ready.
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
            "🔍 <i>Auditing Human Control Surface...</i>",
            parse_mode="HTML"
        )

        # Step 2: Fetch shared aiohttp session
        session: ClientSession = context.bot_data.get("http_session")
        created_temp_session = False
        if not session or session.closed:
            session = ClientSession()
            created_temp_session = True

        try:
            # Step 3: Async Solana token RPC audit with fallback chain
            audit_result = await fetch_token_account_info(session, ca)
        finally:
            if created_temp_session:
                await session.close()

        # Step 4: Edit initial status message with audit report
        report_text = generate_audit_report(ca, audit_result)
        await status_msg.edit_text(
            report_text,
            parse_mode="HTML",
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
