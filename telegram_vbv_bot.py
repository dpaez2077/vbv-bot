"""
Telegram Bot - VBV Checker v3.0
================================
Comandos:
  /start                          - Info del bot
  /vbv number|mm|yy|cvv           - VBV check individual
  /check number|mm|yy|cvv         - Ídem
  /bin 554843                     - BIN lookup (banco, país, tipo)
  /batch                          - VBV check múltiple (1 por línea)
  /stats                          - Estadísticas de la sesión
  /proxy on/off                   - Activar/desactivar proxy
  /export                         - Exportar resultados CSV
  /help                           - Ayuda completa

Instalación:
  pip install python-telegram-bot requests

Ejecución:
  python telegram_vbv_bot.py
  (Token se lee de .env o variable de entorno BOT_TOKEN)
"""

import os, sys, logging, json, csv, io, re, time, random, requests
from typing import Dict, Optional, List, Tuple
from datetime import datetime
from pathlib import Path

# ── Cargar .env si existe ──────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Telegram ───────────────────────────────────────────────────────────
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
except ImportError:
    print("ERROR: Instala python-telegram-bot: pip install python-telegram-bot")
    sys.exit(1)

# ── VBV Engine ────────────────────────────────────────────────────────
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

# ── Config paths ───────────────────────────────────────────────────────
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BASE_DIR / "braintree_config.json"
STATS_PATH = BASE_DIR / "bot_stats.json"
RESULTS_CSV = BASE_DIR / "bot_results.csv"

# ── User whitelist (IDs de Telegram autorizados) ───────────────────────
# Dejar vacío = todos autorizados. Agregar IDs numéricos para restringir.
ALLOWED_USERS: List[int] = []
# Para obtener tu ID: envía /start al bot y revisa los logs

# ── Stats manager ──────────────────────────────────────────────────────

class StatsManager:
    def __init__(self):
        self._data = self._load()

    def _load(self) -> Dict:
        if STATS_PATH.exists():
            try:
                with open(STATS_PATH, "r") as f:
                    return json.load(f)
            except:
                pass
        return {"total_checks": 0, "success": 0, "fail": 0, "by_bin": {}, "users": {}}

    def _save(self):
        with open(STATS_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    def record(self, user_id: int, username: str, card_bin: str, brand: str, result: str):
        s = self._data
        s["total_checks"] += 1
        is_success = "✗" not in result and "error" not in result.lower()
        if is_success:
            s["success"] += 1
        else:
            s["fail"] += 1
        # By BIN
        if card_bin not in s["by_bin"]:
            s["by_bin"][card_bin] = {"count": 0, "brand": brand, "last_result": ""}
        s["by_bin"][card_bin]["count"] += 1
        s["by_bin"][card_bin]["last_result"] = result
        # By user
        uid = str(user_id)
        if uid not in s["users"]:
            s["users"][uid] = {"username": username, "checks": 0}
        s["users"][uid]["checks"] += 1
        self._save()

    def get_summary(self) -> Dict:
        return self._data

    def reset(self):
        self._data = {"total_checks": 0, "success": 0, "fail": 0, "by_bin": {}, "users": {}}
        self._save()

# ── CSV Logger ─────────────────────────────────────────────────────────

class CSVLogger:
    def __init__(self):
        if not RESULTS_CSV.exists():
            with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "user", "bin", "brand", "result"])

    def log(self, user: str, card_bin: str, brand: str, result: str):
        with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user, card_bin, brand, result])

# ── BIN Lookup ─────────────────────────────────────────────────────────

class BINLookup:
    API_URL = "https://lookup.binlist.net/{bin}"

    @staticmethod
    def lookup(bin_num: str) -> Optional[Dict]:
        """Consulta información del BIN. Gratuito, sin API key."""
        try:
            r = requests.get(
                BINLookup.API_URL.format(bin=bin_num[:8]),
                headers={"User-Agent": "Mozilla/5.0", "Accept-Version": "3"},
                proxies={"http": None, "https": None},  # Sin proxy
                timeout=10
            )
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                return {"error": "Rate limited. Espera 30s."}
            return None
        except Exception as e:
            return {"error": f"Error de conexión: {e}"}

    @staticmethod
    def format(data: Dict) -> str:
        if not data:
            return "🐍 No se encontró información para este BIN."
        if "error" in data:
            return f"🐍 {data['error']}"

        lines = [f"🐍 **BIN Lookup: {data.get('bin', '?')}**", ""]
        lines.append(f"├ Marca: `{data.get('scheme', '?')}`")
        lines.append(f"├ Tipo: `{data.get('type', '?')}`")
        lines.append(f"├ Categoría: `{data.get('brand', '?')}`")
        lines.append(f"├ País: `{data.get('country', {}).get('name', '?')} ({data.get('country', {}).get('alpha2', '?')})`")
        lines.append(f"├ Banco: `{data.get('bank', {}).get('name', '?')}`")
        lines.append(f"├ Moneda: `{data.get('country', {}).get('currency', '?')}`")
        lines.append(f"└ Prepaid: `{'Sí' if data.get('prepaid', False) else 'No'}`")

        lines.append("")
        lines.append("🌐 [Ver más](https://lookup.binlist.net/{})".format(data.get('bin', '')))
        return "\n".join(lines)

# ── Response Mapper ────────────────────────────────────────────────────

RESPONSE_MAP = {
    "lookup_not_enrolled": {
        "status": "N", "label": "✅ NO ENROLLED", "icon": "🟢",
        "explanation": "El banco emisor NO participa en 3D Secure.\nLa tarjeta pasa sin autenticación.\nMEJOR ESCENARIO para bypass."
    },
    "lookup_enrolled": {
        "status": "Y", "label": "✅ ENROLLED & AUTHENTICATED", "icon": "🟢",
        "explanation": "Banco enrolado en 3DS y autenticación exitosa.\nEl pago puede procesarse."
    },
    "authenticate_successful": {
        "status": "Y", "label": "✅ AUTHENTICATED", "icon": "🟢",
        "explanation": "Autenticación 3DS exitosa.\nEl banco verificó al titular correctamente."
    },
    "lookup_enrolled|A": {
        "status": "A", "label": "🟡 ATTEMPTED", "icon": "🟡",
        "explanation": "El banco intentó autenticar sin completar el proceso.\nEl merchant puede aceptar bajo su riesgo."
    },
    "lookup_enrolled|N": {
        "status": "N", "label": "🟡 ENROLLED NOT AUTH", "icon": "🟡",
        "explanation": "Banco enrolado pero NO autenticó.\nPosible bypass por monto bajo o reglas del banco."
    },
    "challenge_required": {
        "status": "C", "label": "🔴 CHALLENGE REQUIRED", "icon": "🔴",
        "explanation": "⚠️ REQUIERE CHALLENGE 3DS (OTP/SMS/app bancaria).\nNO se puede completar sin resolver el challenge."
    },
    "Unavailable": {
        "status": "U", "label": "⚫ UNAVAILABLE", "icon": "⚫",
        "explanation": "Sistema 3DS no disponible o sin información.\nTarjeta no enrolada, error temporal, o BIN sin soporte 3DS."
    }
}

ERROR_MAP = {
    "✗ PRODUCT": "Error conectando con la tienda. Proxy caído o IP bloqueada.",
    "✗ CART": "Error creando carrito. Posible bloqueo de sesión.",
    "✗ EMAIL": "Error actualizando email del carrito.",
    "✗ METHODS": "Visa no disponible como método de pago para este producto/país.",
    "✗ SELECT": "Error seleccionando método de pago Visa.",
    "✗ DETAILS": "Error guardando datos personales.",
    "✗ SESSION": "Error iniciando sesión de pago. Proxy caído.",
    "✗ TOKENIZE": "Error tokenizando tarjeta. Tarjeta inválida o rechazada por el gateway.",
    "✗ CHECKOUT": "Error en 3DS lookup. Proxy caído o timeout.",
    "✗ VALIDATION": "Tarjeta no pasó validación (Luhn, formato, CVV incorrecto).",
    "✗ MAX_RETRIES": "Se agotaron los reintentos automáticos (3 intentos).",
    "✗ THREAD": "Error en ejecución multi-thread."
}

def parse_result(raw: str) -> Dict:
    result = {"raw": raw, "type": "unknown", "icon": "❓", "label": "UNKNOWN", "status_code": "", "explanation": "Resultado no reconocido.", "challenge_url": ""}

    if raw.startswith("CHALLENGE"):
        parts = raw.split(" | ", 2)
        result["type"] = "challenge"
        result["icon"] = "🔴"
        result["label"] = "CHALLENGE REQUIRED"
        result["status_code"] = "C"
        result["challenge_url"] = parts[2] if len(parts) > 2 else ""
        result["explanation"] = RESPONSE_MAP["challenge_required"]["explanation"]
        return result

    for err_prefix, err_text in ERROR_MAP.items():
        if raw.startswith(err_prefix):
            result["type"] = "error"
            result["icon"] = "❌"
            result["label"] = err_prefix
            result["explanation"] = err_text
            detail = raw.replace(err_prefix, "", 1).strip().lstrip("|").strip()
            if detail:
                result["explanation"] += f"\n\n📋 Detalle: `{detail[:200]}`"
            return result

    parts = raw.split(" | ")
    status_3ds = parts[0].strip() if len(parts) > 0 else ""
    trans_status = parts[1].strip() if len(parts) > 1 else ""
    result["status_code"] = trans_status
    combined = f"{status_3ds}|{trans_status}" if trans_status else status_3ds

    for key, mapping in RESPONSE_MAP.items():
        if combined.startswith(key):
            result["type"] = "3ds"
            result["icon"] = mapping["icon"]
            result["label"] = mapping["label"]
            result["status_code"] = mapping["status"]
            result["explanation"] = mapping["explanation"]
            return result

    result["type"] = "3ds"
    result["label"] = status_3ds or "UNKNOWN"
    result["explanation"] = f"Estado 3DS: {status_3ds} | TransStatus: {trans_status}"
    return result

def format_response(card_bin: str, brand: str, parsed: Dict) -> str:
    lines = [f"🐍 **VBV CHECK**", ""]
    lines.append(f"┌ BIN: `{card_bin}`")
    lines.append(f"├ Brand: `{brand}`")
    lines.append(f"└ Resultado: {parsed['icon']} **{parsed['label']}**")
    lines.append("")
    if parsed["status_code"]:
        lines.append(f"**TransStatus:** `{parsed['status_code']}`")
        lines.append("")
    lines.append(parsed["explanation"])
    if parsed.get("challenge_url"):
        lines.append("")
        lines.append(f"🔗 Challenge (primeros 80):")
        lines.append(f"`{parsed['challenge_url'][:80]}...`")
    lines.append("")
    lines.append(f"`{parsed['raw'][:100]}`")
    return "\n".join(lines)

# ── Card formatter ─────────────────────────────────────────────────────

def clean_card_input(raw: str) -> str:
    """Limpia el input: elimina espacios, guiones, y separadores."""
    return re.sub(r'[\s\-_\.\,]', '', raw)

def parse_card_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """Parsea una línea de tarjeta. Acepta | , . y espacios como separadores."""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None
    # Reemplazar separadores comunes por pipe
    for sep in [",", ".", ";", "\t", "  "]:
        line = line.replace(sep, "|")
    # Colapsar pipes múltiples
    line = re.sub(r'\|+', '|', line)
    parts = [clean_card_input(x) for x in line.split("|")]
    if len(parts) >= 4:
        return (parts[0], parts[1], parts[2], parts[3])
    return None

# ── Proxy manager via Telegram ─────────────────────────────────────────

def set_proxy_enabled(enabled: bool) -> str:
    """Cambia enabled/disabled del proxy en braintree_config.json"""
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        cfg["proxy"]["apify"]["enabled"] = enabled
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=4)
        return f"🐍 Proxy **{'activado' if enabled else 'desactivado'}** correctamente."
    except Exception as e:
        return f"🐍 Error actualizando config: {e}"

def get_proxy_status() -> str:
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        enabled = cfg.get("proxy", {}).get("apify", {}).get("enabled", False)
        return f"🐍 Proxy: **{'ACTIVADO' if enabled else 'DESACTIVADO'}**"
    except:
        return "❌ No se pudo leer la config."

# ── Auth check ─────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

def auth_check(update: Update) -> bool:
    user = update.effective_user
    if not is_authorized(user.id):
        return False
    return True

# ═══════════════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) started the bot.")
    if not auth_check(update):
        await update.message.reply_text("🐍 No autorizado.")
        return

    await update.message.reply_text(
        "🐍 **Hydra VBV Bot v3.0**\n\n"
        "**Comandos:**\n"
        "`/vbv numero|mm|yy|cvv` — VBV check\n"
        "`/bin 554843` — Info del BIN\n"
        "`/batch` — Múltiples tarjetas\n"
        "`/stats` — Estadísticas\n"
        "`/proxy on/off` — Proxy toggle\n"
        "`/export` — Exportar CSV\n"
        "`/help` — Ayuda completa\n\n"
        "Ej: `/vbv 4000056655665556|12|28|123`",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    text = (
        "🐍 **AYUDA COMPLETA**\n\n"
        "**VBV Check:**\n"
        "`/vbv numero|mes|año|cvv`\n"
        "Acepta separadores: `|` `,` `.` `;` espacio\n"
        "Ej: `/vbv 4000056655665556|12|28|123`\n\n"
        "**BIN Lookup:**\n"
        "`/bin 554843` — Info del BIN (banco, país, tipo)\n\n"
        "**Batch:**\n"
        "`/batch` y luego envía 1 tarjeta por línea\n\n"
        "**Proxy:**\n"
        "`/proxy on` — Activar proxy\n"
        "`/proxy off` — Desactivar proxy\n"
        "`/proxy` — Ver estado\n\n"
        "**Respuestas 3DS:**\n"
        "🟢 `N` = No enrolado, bypass\n"
        "🟢 `Y` = Autenticado\n"
        "🟡 `A` = Intento\n"
        "🟡 `N` = No autenticado\n"
        "🔴 `C` = Challenge requerido\n"
        "⚫ `U` = No disponible\n\n"
        "**Host:** `localhost` — Cloud próximamente"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_bin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if not context.args:
        await update.message.reply_text("🐍 Uso: `/bin 554843`", parse_mode=ParseMode.MARKDOWN)
        return
    bin_num = re.sub(r'\D', '', context.args[0])[:8]
    if len(bin_num) < 6:
        await update.message.reply_text("🐍 El BIN debe tener al menos 6 dígitos.")
        return
    msg = await update.message.reply_text(f"🐍 Buscando BIN `{bin_num}`...", parse_mode=ParseMode.MARKDOWN)
    data = BINLookup.lookup(bin_num)
    await msg.edit_text(BINLookup.format(data), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    stats = StatsManager().get_summary()
    total = stats["total_checks"]
    if total == 0:
        await update.message.reply_text("🐍 No hay estadísticas aún. Usa `/vbv` para empezar.", parse_mode=ParseMode.MARKDOWN)
        return
    rate = (stats["success"] / total * 100) if total > 0 else 0
    lines = [
    "🐍 **ESTADÍSTICAS**", "",
        f"├ Total checks: `{total}`",
        f"├ Exitosos: `{stats['success']}` ({rate:.1f}%)",
        f"├ Fallidos: `{stats['fail']}` ({100-rate:.1f}%)",
        f"├ BINs únicos: `{len(stats['by_bin'])}`",
        f"└ Usuarios: `{len(stats['users'])}`", "",
        "**Top BINs:**"
    ]
    top_bins = sorted(stats["by_bin"].items(), key=lambda x: -x[1]["count"])[:5]
    for bin_num, info in top_bins:
        lines.append(f"├ `{bin_num}` ({info['brand']}): {info['count']}x")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if not context.args:
        status = get_proxy_status()
        # Test proxy
        cfg = ConfigLoader()
        pm = ProxyManager(cfg)
        proxy = pm.get_proxy()
        if proxy:
            proxy_url = proxy.get("http") or proxy.get("https") or ""
            result = ProxyValidator.test(proxy_url)
            if result["working"]:
                status += f"\n├ Estado: `✅ Funcionando`"
                status += f"\n├ IP: `{result['ip']}`"
                status += f"\n├ País: `{result['country']}`"
                status += f"\n└ Latencia: `{result['latency_ms']}ms`"
            else:
                status += f"\n└ Estado: `❌ Caído: {result['error'][:60]}`"
        else:
            status += "\n└ Sin proxy configurado"
        await update.message.reply_text(status, parse_mode=ParseMode.MARKDOWN)
        return

    arg = context.args[0].lower()
    if arg in ("on", "1", "true", "si"):
        result = set_proxy_enabled(True)
    elif arg in ("off", "0", "false", "no"):
        result = set_proxy_enabled(False)
    else:
        result = "🐍 Usa: `/proxy on` o `/proxy off`"
    await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if not RESULTS_CSV.exists():
        await update.message.reply_text("🐍 No hay resultados para exportar.")
        return
    with open(RESULTS_CSV, "rb") as f:
        await update.message.reply_document(f, filename="vbv_results.csv", caption="🐍 Resultados exportados")

async def cmd_batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    context.user_data["batch_mode"] = True
    context.user_data["batch_cards"] = []
    await update.message.reply_text(
        "🐍 **Modo Batch activado**\n\n"
        "Envía las tarjetas, 1 por línea:\n"
        "`5548435904230378|06|30|885`\n"
        "`4000056655665556|12|28|123`\n\n"
        "Envía `/done` para ejecutar o `/cancel` para salir.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    text = update.message.text.strip()

    # ── Batch mode ─────────────────────────────────────────────────
    if context.user_data.get("batch_mode"):
        if text.lower() == "/done":
            cards = context.user_data.get("batch_cards", [])
            context.user_data["batch_mode"] = False
            if not cards:
                await update.message.reply_text("🐍 No hay tarjetas para procesar.")
                return
            await update.message.reply_text(f"🐍 Procesando {len(cards)} tarjetas...")
            await process_batch(update, context, cards)
            return
        elif text.lower() == "/cancel":
            context.user_data["batch_mode"] = False
            await update.message.reply_text("🐍 Batch cancelado.")
            return
        else:
            parsed = parse_card_line(text)
            if parsed:
                card, mm, yy, cvv = parsed
                val_err = CardValidator.validate(card, mm, yy, cvv)
                if val_err:
                    await update.message.reply_text(f"❌ `{card[:6]}...` {val_err}", parse_mode=ParseMode.MARKDOWN)
                else:
                    context.user_data["batch_cards"].append(parsed)
                    await update.message.reply_text(f"✅ `{card[:6]}...` agregada ({len(context.user_data['batch_cards'])})", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text("🐍 Formato inválido. Usa: `numero|mm|yy|cvv`", parse_mode=ParseMode.MARKDOWN)
            return

    # ── Si no es batch, ignorar mensajes sueltos ────────────────────
    await update.message.reply_text(
        "🐍 Usa `/vbv numero|mm|yy|cvv` o `/help` para ayuda.",
        parse_mode=ParseMode.MARKDOWN
    )

async def process_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, cards: List[Tuple]):
    stats_mgr = StatsManager()
    csv_logger = CSVLogger()
    user = update.effective_user
    username = user.username or str(user.id)
    results = []

    for i, (card, mm, yy, cvv) in enumerate(cards, 1):
        card_bin = card[:6]
        brand = CardValidator.detect_brand(card)
        try:
            cfg = ConfigLoader()
            engine = BraintreeVBVEngine(cfg)
            result = engine.execute(card, mm, yy, cvv)
        except Exception as e:
            result = f"✗ ERROR | {e}"

        parsed = parse_result(result)
        results.append((card_bin, brand, parsed))
        stats_mgr.record(user.id, username, card_bin, brand, result)
        csv_logger.log(username, card_bin, brand, result)

    # Enviar resumen
    success = sum(1 for _, _, p in results if "✗" not in p["raw"])
    lines = [f"🐍 **Batch completado: {len(results)} tarjetas**", ""]
    lines.append(f"├ ✅ Exitosas: `{success}`")
    lines.append(f"└ ❌ Fallidas: `{len(results) - success}`")
    lines.append("")
    for card_bin, brand, parsed in results:
        icon = "✅" if "✗" not in parsed["raw"] else "❌"
        lines.append(f"{icon} `{card_bin}` ({brand}) → {parsed['label']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_vbv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth_check(update): return
    if not context.args:
        await update.message.reply_text(
            "🐍 Uso: `/vbv numero|mm|yy|cvv`\n"
            "Ej: `/vbv 5548435904230378|06|30|885`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    raw_input = " ".join(context.args).strip()
    parsed = parse_card_line(raw_input)
    if not parsed:
        await update.message.reply_text(
            "🐍 Formato inválido. Usa: `/vbv numero|mm|yy|cvv`\n"
            "Separa con `|` `,` `.` o espacio.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    card, mm, yy, cvv = parsed
    card_bin = card[:6]
    brand = CardValidator.detect_brand(card)

    # Validar
    val_err = CardValidator.validate(card, mm, yy, cvv)
    if val_err:
        parsed_resp = {"type": "error", "icon": "❌", "label": "VALIDATION ERROR",
                       "status_code": "", "explanation": f"Error: {val_err}",
                       "challenge_url": "", "raw": f"✗ VALIDATION | {val_err}"}
        await update.message.reply_text(
            format_response(card_bin, brand, parsed_resp), parse_mode=ParseMode.MARKDOWN
        )
        return

    msg = await update.message.reply_text(
        f"🐍 Procesando...\nBIN: `{card_bin}` ({brand})",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        cfg = ConfigLoader()
        engine = BraintreeVBVEngine(cfg)
        result = engine.execute(card, mm, yy, cvv)
        parsed_resp = parse_result(result)
        response = format_response(card_bin, brand, parsed_resp)

        # Registrar stats
        user = update.effective_user
        username = user.username or str(user.id)
        StatsManager().record(user.id, username, card_bin, brand, result)
        CSVLogger().log(username, card_bin, brand, result)

        await msg.edit_text(response, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"🐍 **ERROR INESPERADO**\n\n`{e}`", parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("=" * 60)
        print("  HYDRA VBV BOT v3.0")
        print("=" * 60)
        print()
        print("  Configura el token en .env o variable BOT_TOKEN")
        print()
        print("  .env file:")
        print("    BOT_TOKEN=tu_token_aqui")
        print("    APIFY_PROXY_PASSWORD=tu_password (opcional)")
        print()
        print("  O por variable de entorno:")
        print("    set BOT_TOKEN=tu_token_aqui")
        print("    python telegram_vbv_bot.py")
        print("=" * 60)
        if len(sys.argv) > 1:
            token = sys.argv[1]
        else:
            return

    print(f"  Starting Hydra VBV Bot v3.0...")
    print(f"  Allowed users: {'Todos' if not ALLOWED_USERS else ALLOWED_USERS}")
    print(f"  Bot is running. Press Ctrl+C to stop.")

    app = Application.builder().token(token).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("vbv", cmd_vbv))
    app.add_handler(CommandHandler("check", cmd_vbv))
    app.add_handler(CommandHandler("bin", cmd_bin))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("proxy", cmd_proxy))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("batch", cmd_batch_start))
    app.add_handler(CommandHandler("done", handle_message))
    app.add_handler(CommandHandler("cancel", handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()