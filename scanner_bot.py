import time
import json
import urllib.request
import urllib.parse
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8660593861:AAEbPMr5FqblDP6Gb1KSELvyBNWE4IBppEU")
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Dummy webserver så Render Free Tier holder servicen i live gratis
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"AUTONOMA HCS SCANNER ACTIVE")

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def get_token_info(mint_address):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint_address.strip(), {"encoding": "jsonParsed"}]
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(SOLANA_RPC, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode('utf-8'))
            if not res.get("result") or not res["result"].get("value"):
                return None
            val = res["result"]["value"]
            owner = val.get("owner", "")
            info = val.get("data", {}).get("parsed", {}).get("info", {})
            return {
                "is_token_2022": (owner == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
                "mint_auth": info.get("mintAuthority"),
                "freeze_auth": info.get("freezeAuthority")
            }
    except Exception:
        return None

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Send error: {e}")

def bot_loop():
    print("🛡️ AUTONOMA HCS Scanner Engine Active...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=35) as res:
                updates = json.loads(res.read().decode('utf-8'))
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if not text or not chat_id:
                    continue

                if text.startswith("/scan"):
                    parts = text.strip().split()
                    if len(parts) < 2:
                        send_message(chat_id, "⚠️ <b>Format:</b> <code>/scan &lt;SOLANA_CA&gt;</code>")
                        continue
                    ca = parts[1]
                    send_message(chat_id, f"🔍 <i>Auditing Human Control Surface for:</i>\n<code>{ca}</code>...")
                    data = get_token_info(ca)
                    if not data:
                        send_message(chat_id, "❌ <b>Scan Error:</b> Address not found on Mainnet-Beta or invalid CA.")
                        continue
                    is_clean = (data["mint_auth"] is None) and (data["freeze_auth"] is None)
                    status = "🟢 <b>ZERO HUMAN CONTROL (0% HCS)</b>" if is_clean else "🔴 <b>HIGH DISCRETIONARY RISK DETECTED</b>"
                    
                    report = f"""
🛡️ <b>AUTONOMA HCS RUNTIME REPORT</b>
━━━━━━━━━━━━━━━━━━━━
<b>Target CA:</b> <code>{ca}</code>
<b>Program:</b> {'Token-2022' if data['is_token_2022'] else 'Legacy SPL'}

• <b>Mint Authority:</b> {'❌ RETAINED (Inflation Risk)' if data['mint_auth'] else '✅ REVOKED (0 Expansion)'}
• <b>Freeze Authority:</b> {'❌ RETAINED (Blacklist Risk)' if data['freeze_auth'] else '✅ REVOKED (Permissionless)'}
━━━━━━━━━━━━━━━━━━━━
<b>VERDICT:</b> {status}

<i>Audited via AUTONOMA Protocol // autonomaprotocol.io</i>
"""
                    send_message(chat_id, report.strip())
        except Exception:
            time.sleep(3)

if __name__ == "__main__":
    # Start dummy port i baggrundstråd
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    # Kør Telegram scanner-motoren
    bot_loop()
