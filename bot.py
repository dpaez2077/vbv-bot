#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║              🐍 VBV BOT v4.0 - TELEGRAM                    ║
║                    by K4muz                                ║
╚══════════════════════════════════════════════════════════════╝

Comandos:
  /vbv numero|mes|ano|cvv  - Verificar tarjeta (3DS/VBV)
  /bin 554843              - Informacion del BIN
  /start                   - Info del bot

Admin:
  /wl add <id>   - Agregar usuario a whitelist
  /wl remove <id> - Eliminar usuario
  /wl list       - Listar usuarios
  /wl on         - Activar whitelist
  /wl off        - Desactivar whitelist
  /id            - Tu ID de Telegram
"""

import os, sys, json, logging, re, requests
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APIFY_PASSWORD = os.environ.get("APIFY_PROXY_PASSWORD", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN no configurado")
    sys.exit(1)

# ── Paths ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WHITELIST_PATH = os.path.join(BASE_DIR, "whitelist.json")

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Telegram ────────────────────────────────────────────────────────────
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Engine ──────────────────────────────────────────────────────────────
sys.path.insert(0, BASE_DIR)
os.environ["APIFY_PROXY_PASSWORD"] = APIFY_PASSWORD
from braintree_vbv_improved import ConfigLoader, BraintreeVBVEngine, CardValidator

# ═══════════════════════════════════════════════════════════════════════
#  WHITELIST
# ═══════════════════════════════════════════════════════════════════════

class Whitelist:
    def __init__(self):
        self._data = self._load()

    def _load(self):
        if os.path.exists(WHITELIST_PATH):
            try:
                with open(WHITELIST_PATH) as f:
                    return json.load(f)
            except: pass
        return {"enabled": False, "users": []}

    def _save(self):
        with open(WHITELIST_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def enabled(self): return self._data.get("enabled", False)

    @enabled.setter
    def enabled(self, v): self._data["enabled"] = v; self._save()

    @property
    def users(self): return self._data.get("users", [])

    def add(self, uid):
        if uid in self.users: return f"El usuario {uid} ya esta en la whitelist."
        self._data["users"].append(uid); self._save()
        return f"Usuario {uid} agregado a la whitelist."

    def remove(self, uid):
        if uid not in self.users: return f"El usuario {uid} no esta en la whitelist."
        self._data["users"].remove(uid); self._save()
        return f"Usuario {uid} eliminado de la whitelist."

    def authorized(self, uid):
        if not self.enabled: return True
        return uid in self.users

WL = Whitelist()

# ═══════════════════════════════════════════════════════════════════════
#  BIN LOOKUP
# ═══════════════════════════════════════════════════════════════════════

def lookup_bin(bin_num):
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin_num[:8]}",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Version": "3"},
            timeout=10)
        if r.status_code == 200: return r.json()
        if r.status_code == 429: return {"error": "Rate limited. Espera 30s."}
        return None
    except Exception as e:
        return {"error": f"Error: {e}"}

def format_bin(data):
    if not data: return "No se encontro info para este BIN."
    if "error" in data: return data["error"]
    lines = [f"🔢 BIN: {data.get('bin', '?')}", ""]
    lines.append(f"  Marca:     {data.get('scheme', '?')}")
    lines.append(f"  Tipo:      {data.get('type', '?')}")
    lines.append(f"  Categoria: {data.get('brand', '?')}")
    lines.append(f"  Pais:      {data.get('country', {}).get('name', '?')} ({data.get('country', {}).get('alpha2', '?')})")
    lines.append(f"  Banco:     {data.get('bank', {}).get('name', '?')}")
    lines.append(f"  Moneda:    {data.get('country', {}).get('currency', '?')}")
    lines.append(f"  Prepaid:   {'Si' if data.get('prepaid', False) else 'No'}")
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════════════
#  VBV RESULT PARSER
# ═══════════════════════════════════════════════════════════════════════

RESULTADOS = {
    "lookup_not_enrolled":    {"icon": "🟢", "label": "NO ENROLADO", "status": "N",
        "msg": "El banco NO participa en 3D Secure. BYPASS TOTAL."},
    "lookup_enrolled":        {"icon": "✅", "label": "ENROLADO Y AUTENTICADO", "status": "Y",
        "msg": "Banco enrolado y autenticacion exitosa."},
    "authenticate_successful":{"icon": "✅", "label": "AUTENTICADO", "status": "Y",
        "msg": "Autenticacion 3DS exitosa."},
    "lookup_enrolled|A":      {"icon": "🟡", "label": "INTENTO", "status": "A",
        "msg": "El banco intento autenticar sin completar."},
    "lookup_enrolled|N":      {"icon": "🟢", "label": "ENROLADO NO AUTENT", "status": "N",
        "msg": "Banco enrolado pero NO autentico. Posible bypass."},
    "challenge_required":     {"icon": "🔴", "label": "CHALLENGE REQUERIDO", "status": "C",
        "msg": "Requiere OTP/SMS/app bancaria. No se puede completar."},
    "Unavailable":            {"icon": "⚫", "label": "NO DISPONIBLE", "status": "U",
        "msg": "Sistema 3DS no disponible."},
}

ERRORES = {
    "PRODUCT":  "Error conectando a la tienda. Proxy caido.",
    "CART":     "Error creando carrito. Sesion bloqueada.",
    "EMAIL":    "Error actualizando email.",
    "METHODS":  "Visa no disponible como metodo de pago.",
    "SELECT":   "Error seleccionando Visa.",
    "DETAILS":  "Error guardando datos personales.",
    "SESSION":  "Error iniciando sesion de pago. Proxy caido o IP bloqueada.",
    "TOKENIZE": "Error tokenizando tarjeta. Tarjeta invalida.",
    "CHECKOUT": "Error en 3DS lookup.",
    "VALIDATION": "Tarjeta no paso validacion.",
}

def parse_vbv(raw):
    res = {"icon": "❓", "label": "DESCONOCIDO", "status": "", "msg": "", "pais": "", "raw": raw}
    m = re.search(r' \| 🌐 ([A-Z]{2})$', raw)
    if m:
        res["pais"] = m.group(1)
        raw = raw[:m.start()]
    for key, txt in ERRORES.items():
        if f"✗ {key}" in raw:
            res["icon"] = "❌"; res["label"] = f"ERROR {key}"; res["msg"] = txt
            det = raw.split("|")[-1].strip() if "|" in raw else ""
            if det and "🌐" not in det: res["msg"] += f"\n{det}"
            return res
    if raw.startswith("CHALLENGE"):
        res["icon"] = "🔴"; res["label"] = "CHALLENGE REQUERIDO"; res["status"] = "C"
        res["msg"] = RESULTADOS["challenge_required"]["msg"]
        return res
    parts = raw.split(" | ")
    s3ds = parts[0].strip() if parts else ""
    trans = parts[1].strip() if len(parts) > 1 else ""
    res["status"] = trans
    combined = f"{s3ds}|{trans}" if trans else s3ds
    for key, val in RESULTADOS.items():
        if combined.startswith(key):
            res["icon"] = val["icon"]; res["label"] = val["label"]
            res["status"] = val["status"]; res["msg"] = val["msg"]
            return res
    res["label"] = s3ds or "DESCONOCIDO"
    res["msg"] = f"Estado 3DS: {s3ds} | TransStatus: {trans}"
    return res

# ═══════════════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════════════

def auth(update):
    return WL.authorized(update.effective_user.id)

def is_admin(user_id):
    return True if ADMIN_ID == 0 else user_id == ADMIN_ID

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "🐍 VBV BOT v4.0 - by K4muz\n\n"
        "Comandos:\n"
        "/vbv numero|mes|ano|cvv  - Verificar tarjeta\n"
        "/bin 554843              - Info del BIN\n"
        "/wl add/remove/list/on/off - Whitelist (admin)\n"
        "/id                      - Tu ID de Telegram\n\n"
        "Ej: /vbv 5175469433364689|01|2030|702"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    user = update.effective_user
    await update.message.reply_text(
        f"Tu ID: {user.id}\n"
        f"Username: @{user.username or 'N/A'}\n"
        f"Admin: {'Si' if is_admin(user.id) else 'No'}"
    )

async def cmd_bin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    if not context.args:
        await update.message.reply_text("Uso: /bin 554843")
        return
    bin_num = re.sub(r'\D', '', context.args[0])[:8]
    if len(bin_num) < 6:
        await update.message.reply_text("BIN invalido. Minimo 6 digitos.")
        return
    msg = await update.message.reply_text(f"Buscando BIN {bin_num}...")
    data = lookup_bin(bin_num)
    await msg.edit_text(format_bin(data))

async def cmd_vbv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    if not context.args:
        await update.message.reply_text(
            "Uso: /vbv numero|mes|ano|cvv\n"
            "Ej: /vbv 5175469433364689|01|2030|702"
        )
        return
    raw = " ".join(context.args).strip()
    parts = [x.strip() for x in raw.replace(",", "|").split("|")]
    if len(parts) < 4:
        await update.message.reply_text("Formato invalido. Usa: numero|mes|ano|cvv")
        return
    card, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    card = re.sub(r'\D', '', card)
    card_bin = card[:6]
    brand = CardValidator.detect_brand(card)
    val_err = CardValidator.validate(card, mm, yy, cvv)
    if val_err:
        await update.message.reply_text(f"Error: {val_err}")
        return
    msg = await update.message.reply_text(f"Procesando... BIN: {card_bin} ({brand})")
    try:
        cfg = ConfigLoader()
        engine = BraintreeVBVEngine(cfg)
        result = engine.execute(card, mm, yy, cvv)
        parsed = parse_vbv(result)
        bin_data = lookup_bin(card_bin)
        banco = ""
        pais = ""
        if bin_data and "error" not in bin_data:
            banco = bin_data.get("bank", {}).get("name", "")
            pais = f"{bin_data.get('country', {}).get('name', '')} ({bin_data.get('country', {}).get('alpha2', '')})"
        response = (
            f"🐍 VBV CHECK\n"
            f"{'─'*40}\n"
            f"  BIN:     {card_bin}\n"
            f"  Marca:   {brand}\n"
            f"  Banco:   {banco or 'N/A'}\n"
            f"  Pais:    {pais or 'N/A'}\n"
            f"  Proxy:   {parsed['pais'] or 'N/A'}\n"
            f"{'─'*40}\n"
            f"  {parsed['icon']} {parsed['label']}\n"
            f"  TransStatus: {parsed['status'] or 'N/A'}\n"
            f"{'─'*40}\n"
            f"  {parsed['msg']}"
        )
        await msg.edit_text(response)
    except Exception as e:
        await msg.edit_text(f"ERROR INESPERADO\n{e}")

async def cmd_wl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text(f"No autorizado. Tu ID: {user.id}")
        return
    if not context.args:
        await update.message.reply_text(
            "/wl add <id>  - Agregar usuario\n"
            "/wl remove <id> - Eliminar\n"
            "/wl list     - Listar\n"
            "/wl on       - Activar\n"
            "/wl off      - Desactivar"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "list":
        users = WL.users
        if not users:
            await update.message.reply_text("Whitelist vacia.")
            return
        lines = [f"Whitelist ({'ACTIVADA' if WL.enabled else 'DESACTIVADA'})", ""]
        for uid in users: lines.append(f"  - {uid}")
        await update.message.reply_text("\n".join(lines))
    elif cmd == "on":
        WL.enabled = True
        await update.message.reply_text("Whitelist ACTIVADA. Solo usuarios autorizados.")
    elif cmd == "off":
        WL.enabled = False
        await update.message.reply_text("Whitelist DESACTIVADA. Todos pueden usar el bot.")
    elif cmd == "add":
        if len(context.args) < 2: return
        try:
            uid = int(context.args[1])
            await update.message.reply_text(WL.add(uid))
        except: await update.message.reply_text("ID debe ser numero.")
    elif cmd == "remove":
        if len(context.args) < 2: return
        try:
            uid = int(context.args[1])
            await update.message.reply_text(WL.remove(uid))
        except: await update.message.reply_text("ID debe ser numero.")
    else:
        await update.message.reply_text("Comando invalido. Usa: add, remove, list, on, off")

# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("=" * 50)
    print("  VBV BOT v4.0 - by K4muz")
    print(f"  Admin ID: {ADMIN_ID}")
    print(f"  Whitelist: {'ACTIVADA' if WL.enabled else 'DESACTIVADA'} ({len(WL.users)} users)")
    print("=" * 50)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("vbv", cmd_vbv))
    app.add_handler(CommandHandler("check", cmd_vbv))
    app.add_handler(CommandHandler("bin", cmd_bin))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("wl", cmd_wl))
    print("  Bot running. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()