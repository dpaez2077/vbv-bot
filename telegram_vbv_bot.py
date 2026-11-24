"""
Telegram Bot - Braintree VBV Checker
=====================================
Comandos:
  /start          - Info del bot
  /vbv number|mm|yy|cvv  - Ejecutar VBV check
  /check number|mm|yy|cvv - Ídem
  /help           - Ayuda detallada

Instalación:
  pip install python-telegram-bot requests

Ejecución:
  set BOT_TOKEN=tu_token_aqui
  python telegram_vbv_bot.py
"""

import os, sys, logging, json, asyncio
from typing import Dict, Optional

# ── Telegram ────────────────────────────────────────────────────────────
try:
    from telegram import Update, ParseMode
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    print("ERROR: Instala python-telegram-bot: pip install python-telegram-bot")
    sys.exit(1)

# ── Braintree Engine ───────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from braintree_vbv_improved import (
    ConfigLoader, BraintreeVBVEngine, CardValidator, ProxyValidator, ProxyManager
)

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Response Mapper ────────────────────────────────────────────────────

RESPONSE_MAP = {
    "lookup_not_enrolled": {
        "status": "N",
        "label": "✅ NO ENROLLED",
        "icon": "🟢",
        "explanation": (
            "El banco emisor NO participa en el programa 3D Secure.\n"
            "La tarjeta pasa sin autenticación adicional.\n"
            "Es el mejor escenario para bypass."
        )
    },
    "lookup_enrolled": {
        "status": "Y",
        "label": "✅ ENROLLED & AUTHENTICATED",
        "icon": "🟢",
        "explanation": (
            "El banco emisor está enrolado en 3DS y la autenticación "
            "fue exitosa. El pago puede procesarse."
        )
    },
    "lookup_enrolled|A": {
        "status": "A",
        "label": "🟡 ATTEMPTED",
        "icon": "🟡",
        "explanation": (
            "El banco intentó autenticar pero no pudo completar el proceso.\n"
            "Se considera como 'autenticación intentada'.\n"
            "El merchant puede aceptar el pago bajo su propio riesgo."
        )
    },
    "lookup_enrolled|N": {
        "status": "N",
        "label": "🟡 ENROLLED NOT AUTHENTICATED",
        "icon": "🟡",
        "explanation": (
            "El banco está enrolado pero NO autenticó al titular.\n"
            "Posible bypass por monto bajo, merchant confiable, "
            "o reglas internas del banco.\n"
            "El merchant decide si acepta."
        )
    },
    "challenge_required": {
        "status": "C",
        "label": "🔴 CHALLENGE REQUIRED",
        "icon": "🔴",
        "explanation": (
            "⚠️ REQUIERE CHALLENGE 3DS.\n"
            "El banco exige autenticación del titular vía OTP, SMS, "
            "app bancaria o biometría.\n"
            "NO se puede completar sin resolver el challenge."
        )
    },
    "Unavailable": {
        "status": "U",
        "label": "⚫ UNAVAILABLE",
        "icon": "⚫",
        "explanation": (
            "El sistema 3DS no está disponible o no devolvió información.\n"
            "Puede deberse a: tarjeta no enrolada, error temporal, "
            "o BIN sin soporte 3DS."
        )
    }
}

ERROR_MAP = {
    "✗ PRODUCT": "Error obteniendo la página del producto. Proxy o conectividad.",
    "✗ CART": "Error creando el carrito express. Posible bloqueo de sesión.",
    "✗ EMAIL": "Error actualizando email del carrito.",
    "✗ METHODS": "Visa no disponible como método de pago para este producto/país.",
    "✗ SELECT": "Error seleccionando Visa como método de pago.",
    "✗ DETAILS": "Error guardando datos personales.",
    "✗ SESSION": "Error iniciando sesión de pago Braintree. Proxy o conectividad.",
    "✗ TOKENIZE": "Error tokenizando la tarjeta. Tarjeta inválida o rechazada.",
    "✗ CHECKOUT": "Error en 3DS lookup. Proxy o conectividad.",
    "✗ VALIDATION": "La tarjeta no pasó validación (Luhn, formato, CVV).",
    "✗ MAX_RETRIES": "Se agotaron los reintentos automáticos.",
    "✗ THREAD": "Error en ejecución multi-thread."
}


def parse_result(raw: str) -> Dict:
    """Interpreta el resultado crudo del engine y devuelve dict formateado."""
    result = {
        "raw": raw,
        "type": "unknown",
        "icon": "❓",
        "label": "UNKNOWN",
        "status_code": "",
        "explanation": "Resultado no reconocido.",
        "challenge_url": ""
    }

    # Check for challenge URL
    if raw.startswith("CHALLENGE"):
        parts = raw.split(" | ", 2)
        result["type"] = "challenge"
        result["icon"] = "🔴"
        result["label"] = "CHALLENGE REQUIRED"
        result["status_code"] = "C"
        result["challenge_url"] = parts[2] if len(parts) > 2 else ""
        result["explanation"] = RESPONSE_MAP["challenge_required"]["explanation"]
        return result

    # Check for errors
    for err_prefix, err_text in ERROR_MAP.items():
        if raw.startswith(err_prefix):
            result["type"] = "error"
            result["icon"] = "❌"
            result["label"] = err_prefix
            result["explanation"] = err_text
            detail = raw.replace(err_prefix, "", 1).strip().lstrip("|").strip()
            if detail:
                result["explanation"] += f"\nDetalle: {detail}"
            return result

    # Parse 3DS status
    parts = raw.split(" | ")
    status_3ds = parts[0].strip() if len(parts) > 0 else ""
    trans_status = parts[1].strip() if len(parts) > 1 else ""

    result["status_code"] = trans_status

    # Match against known responses
    combined = f"{status_3ds}|{trans_status}" if trans_status else status_3ds

    for key, mapping in RESPONSE_MAP.items():
        if combined.startswith(key):
            result["type"] = "3ds"
            result["icon"] = mapping["icon"]
            result["label"] = mapping["label"]
            result["status_code"] = mapping["status"]
            result["explanation"] = mapping["explanation"]
            return result

    # Fallback
    result["type"] = "3ds"
    result["label"] = status_3ds or "UNKNOWN"
    result["explanation"] = f"Estado 3DS: {status_3ds} | TransStatus: {trans_status}"
    return result


def format_response(card_bin: str, brand: str, parsed: Dict) -> str:
    """Formatea el resultado para Telegram."""
    lines = []
    lines.append(f"💳 **VBV CHECK RESULT**")
    lines.append("")
    lines.append(f"┌ BIN: `{card_bin}`")
    lines.append(f"├ Brand: `{brand}`")
    lines.append(f"└ Result: {parsed['icon']} **{parsed['label']}**")
    lines.append("")

    if parsed["status_code"]:
        lines.append(f"**TransStatus:** `{parsed['status_code']}`")
        lines.append("")

    lines.append(parsed["explanation"])

    if parsed.get("challenge_url"):
        lines.append("")
        lines.append(f"🔗 Challenge URL (primeros 80 chars):")
        lines.append(f"`{parsed['challenge_url'][:80]}...`")

    lines.append("")
    lines.append(f"`{parsed['raw']}`")

    return "\n".join(lines)


# ── Bot Handlers ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Braintree VBV Bot**\n\n"
        "Comandos:\n"
        "`/vbv numero|mm|yy|cvv` — Ejecutar VBV check\n"
        "`/check numero|mm|yy|cvv` — Ídem\n"
        "`/help` — Ayuda detallada\n"
        "`/proxy` — Probar proxy actual\n\n"
        "Ejemplo:\n"
        "`/vbv 5548435904230378|06|30|885`",
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 **AYUDA VBV BOT**\n\n"
        "**Formato:**\n"
        "`/vbv numero|mes|año|cvv`\n\n"
        "**Ejemplos:**\n"
        "`/vbv 4000056655665556|12|28|123`\n"
        "`/vbv 5548435904230378|06|30|885`\n\n"
        "**Respuestas posibles:**\n\n"
        "🟢 `lookup_not_enrolled | N`\n"
        "→ No enrolado, pasa sin 3DS\n\n"
        "🟢 `lookup_enrolled | Y`\n"
        "→ Enrolado y autenticado\n\n"
        "🟡 `lookup_enrolled | A`\n"
        "→ Intento de autenticación\n\n"
        "🟡 `lookup_enrolled | N`\n"
        "→ Enrolado no autenticado\n\n"
        "🔴 `challenge_required | C`\n"
        "→ Requiere challenge OTP/SMS\n\n"
        "⚫ `Unavailable | U`\n"
        "→ No disponible\n\n"
        "**Hosting:**\n"
        "Render, PythonAnywhere, Railway, o tu propio servidor"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Probando proxy...")
    try:
        cfg = ConfigLoader()
        pm = ProxyManager(cfg)
        proxy = pm.get_proxy()
        if not proxy:
            await update.message.reply_text("❌ No hay proxy configurado en braintree_config.json")
            return
        proxy_url = proxy.get("http") or proxy.get("https") or ""
        result = ProxyValidator.test(proxy_url)
        if result["working"]:
            await update.message.reply_text(
                f"✅ **Proxy OK**\n"
                f"IP: `{result['ip']}`\n"
                f"País: `{result['country']}`\n"
                f"Latencia: `{result['latency_ms']}ms`",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ **Proxy FAILED**\nError: `{result['error']}`",
                parse_mode="Markdown"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def vbv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ Uso: `/vbv numero|mm|yy|cvv`\n"
            "Ej: `/vbv 5548435904230378|06|30|885`",
            parse_mode="Markdown"
        )
        return

    raw_input = " ".join(context.args).strip()
    parts = [x.strip() for x in raw_input.replace(",", "|").split("|")]

    if len(parts) < 4:
        await update.message.reply_text(
            "❌ Formato inválido. Usa: `/vbv numero|mm|yy|cvv`",
            parse_mode="Markdown"
        )
        return

    card, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    card_bin = card[:6]
    brand = CardValidator.detect_brand(card)

    # Validate
    val_err = CardValidator.validate(card, mm, yy, cvv)
    if val_err:
        parsed = {
            "type": "error",
            "icon": "❌",
            "label": "VALIDATION ERROR",
            "status_code": "",
            "explanation": f"Error de validación: {val_err}",
            "challenge_url": "",
            "raw": f"✗ VALIDATION | {val_err}"
        }
        await update.message.reply_text(
            format_response(card_bin, brand, parsed),
            parse_mode="Markdown"
        )
        return

    # Send "processing" message
    msg = await update.message.reply_text(
        f"⏳ Procesando...\nBIN: `{card_bin}` ({brand})",
        parse_mode="Markdown"
    )

    try:
        cfg = ConfigLoader()
        engine = BraintreeVBVEngine(cfg)
        result = engine.execute(card, mm, yy, cvv)
        parsed = parse_result(result)
        response = format_response(card_bin, brand, parsed)
        await msg.edit_text(response, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(
            f"❌ **ERROR INESPERADO**\n\n`{e}`",
            parse_mode="Markdown"
        )


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("=" * 60)
        print("  TELEGRAM VBV BOT")
        print("=" * 60)
        print()
        print("  Debes configurar el token de tu bot.")
        print()
        print("  Opción 1: Variable de entorno")
        print("    set BOT_TOKEN=tu_token_aqui")
        print("    python telegram_vbv_bot.py")
        print()
        print("  Opción 2: Pasar como argumento")
        print("    python telegram_vbv_bot.py tu_token_aqui")
        print()
        print("  ¿Cómo crear un bot?")
        print("    1. Abre Telegram y busca @BotFather")
        print("    2. Envía /newbot y sigue las instrucciones")
        print("    3. Copia el token que te da BotFather")
        print("=" * 60)
        if len(sys.argv) > 1:
            token = sys.argv[1]
        else:
            return

    print(f"  Starting bot...")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("proxy", proxy))
    app.add_handler(CommandHandler("vbv", vbv))
    app.add_handler(CommandHandler("check", vbv))

    print(f"  Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()