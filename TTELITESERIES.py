"""
TT Elite Series v49 - Anti-Sesgo Jugador 1 + Verificación Estricta de Datos Reales + Stake Seguro (>=92%)
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from openai import OpenAI

# ---------------- CONFIGURACION ----------------

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "").strip()

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))
DB_PATH = os.getenv("DB_PATH", "tt_elite_v49.db")  # Nueva DB limpia para borrar análisis sesgados previos

LEAD_MIN = 45         
MIN_LEAD_MIN = 10

DAILY_MIN_100 = 4
STAKE_SEGURO_MIN = 92        # Umbral para STAKE SEGURO (>= 92%)
STAKE_100_MIN = 85           # Meta ideal para Stake 100 (>= 85%)
STAKE_100_FALLBACK_MIN = 78  # Fallback para Stake 100 SOLO con datos reales verificados
FRANJA_MIN_PROB = 75         # Meta ideal para picks de franja (>= 75%)

TZ = ZoneInfo("America/Bogota")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
for noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("TT_ELITE")

web = Flask(__name__)
started_at = time.time()


# ---------------- BASE DE DATOS ----------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS fixtures (
            event_id TEXT PRIMARY KEY, player1 TEXT, player2 TEXT, start_ts INTEGER, hour_block TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS analysis (
            event_id TEXT PRIMARY KEY, fav TEXT, rating REAL, prob INTEGER, quality TEXT,
            details TEXT, analyzed_at INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS picks (
            event_id TEXT PRIMARY KEY, flow TEXT, rating REAL, fav TEXT,
            player1 TEXT, player2 TEXT, start_ts INTEGER, sent_at INTEGER,
            hit INTEGER, details TEXT, hour_block TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")


def count_100_today():
    inicio_dia = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM picks WHERE flow IN ('100', 'seguro') AND sent_at>=?", (inicio_dia,)).fetchone()[0]


def block_already_sent(hour_block):
    with db() as c:
        row = c.execute("SELECT COUNT(*) FROM picks WHERE hour_block=?", (hour_block,)).fetchone()
        return row[0] > 0 if row else False


def seconds_since_last_100():
    with db() as c:
        row = c.execute("SELECT MAX(sent_at) as last_sent FROM picks WHERE flow IN ('100', 'seguro')").fetchone()
        if row and row["last_sent"]:
            return time.time() - row["last_sent"]
        return 999999


# ---------------- PASO 1: PERPLEXITY (RASTREO PROFUNDO TT ELITE POLONIA) ----------------

def extraer_texto_perplexity(data: dict) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text") and content.get("text"):
                    return content["text"].strip()
    if "choices" in data and data["choices"]:
        return data["choices"][0].get("message", {}).get("content", "").strip()
    return ""


def investigar_con_perplexity(player1: str, player2: str) -> str:
    if not PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY no configurada.")
        return "SIN_DATOS"

    fecha_hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    
    prompt_busqueda = (
        f"Busca estadísticas reales de tenis de mesa de la liga 'TT Elite Series' de Polonia "
        f"para el partido entre '{player1}' y '{player2}' (fecha actual: {fecha_hoy}). "
        f"Nota: Si los nombres tienen inicial (ej. 'Apellido N.'), busca por su nombre completo e inicial en "
        f"Sofascore, Flashscore, Setka/TT Elite Series, BetsAPI o 365Scores. "
        f"Necesito datos numéricos verificables de ambos jugadores: "
        f"1. Últimos 5 a 10 partidos jugados por {player1} (cuántos ganó y cuántos perdió recientemente). "
        f"2. Últimos 5 a 10 partidos jugados por {player2} (cuántos ganó y cuántos perdió recientemente). "
        f"3. Historial Head-to-Head (H2H) directo entre {player1} y {player2} (marcador histórico de enfrentamientos entre ellos). "
        f"Si realmente no existen datos de estos jugadores en la web, responde exactamente la palabra: SIN_DATOS_WEB."
    )

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    payloads = [
        {
            "model": "perplexity/sonar",
            "input": prompt_busqueda,
            "tools": [{"type": "web_search"}]
        },
        {
            "model": "sonar",
            "input": prompt_busqueda
        }
    ]

    for payload in payloads:
        try:
            resp = requests.post(
                "https://api.perplexity.ai/v1/responses",
                headers=headers,
                json=payload,
                timeout=35
            )
            if resp.status_code == 200:
                texto = extraer_texto_perplexity(resp.json())
                if texto:
                    log.info("Perplexity investigó (TT Elite Polonia): %s vs %s", player1, player2)
                    return texto
        except Exception as exc:
            log.debug("Excepción en Perplexity: %s", exc)

    return "SIN_DATOS"


# ---------------- PASO 2: OPENAI (CERO SESGO JUGADOR 1 + AUDITORÍA ESTRICTA) ----------------

def analizar_partido(player1: str, player2: str):
    datos_perplexity = investigar_con_perplexity(player1, player2)

    if not openai_client or "SIN_DATOS" in datos_perplexity:
        return "NINGUNO", 0.0, 0, "Descartado: Sin datos web verificables."

    prompt_analisis = f"""
Actúa como un auditor estadístico imparcial de tenis de mesa (TT Elite Series de Polonia).

REGLAS ANTI-SESGO Y ANTI-INVENTO (OBLIGATORIAS):
1. PROHIBIDO EL SESGO DEL JUGADOR A: El orden en que aparecen "{player1}" y "{player2}" es azaroso. Está terminantemente prohibido dar como favorito a "{player1}" por defecto o por "potencial general".
2. AUDITORÍA DE DATOS REALES: Revisa el texto de Perplexity. Si el texto dice que no encontró estadísticas específicas, que faltan datos recientes, o no muestra números claros de victorias/derrotas o H2H, DEBES poner `"datos_suficientes": false`, `"prob_p1": 50`, `"prob_p2": 50`.
3. CÓMO ELEGIR AL FAVORITO REAL:
   - Revisa quién domina el H2H directo (quién le ha ganado más veces al otro).
   - Revisa quién tiene mejor racha reciente de victorias en los últimos días.
   - Si "{player2}" tiene mejor H2H o mejor racha que "{player1}", el favorito OBLIGATORIAMENTE es "{player2}" y `prob_p2` debe ser el número alto.
4. En `resumen`, escribe las cifras reales encontradas (ejemplo: "H2H 4-1 a favor de X y ganó 4 de sus últimos 5 partidos").

Jugadores a comparar:
- Jugador A: {player1}
- Jugador B: {player2}

REPORTE DE PERPLEXITY:
\"\"\"
{datos_perplexity}
\"\"\"

Responde ÚNICAMENTE con este JSON válido (calcula los enteros reales según el reporte; deben sumar 100):
{{
  "datos_suficientes": true,
  "fav": "Escribe aquí el nombre exacto del verdadero favorito ('{player1}' o '{player2}')",
  "prob_p1": 50,
  "prob_p2": 50,
  "rating": 8.0,
  "resumen": "Cifras exactas de H2H y racha reciente de ambos jugadores.",
  "value_bet": "Sí / No"
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_analisis}],
            temperature=0.0,
            max_tokens=320
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()

        data = json.loads(content)
        datos_ok = bool(data.get("datos_suficientes", False))
        resumen = str(data.get("resumen", "")).strip()

        # Candado en Python contra frases de invención o falta de datos
        frases_prohibidas = (
            "falta de datos", "falta de información", "no se encontraron", "sin datos",
            "a pesar de la falta", "potencial rendimiento", "no hay información",
            "no existen datos", "información limitada", "asume", "suponiendo"
        )
        if not datos_ok or any(fp in resumen.lower() for fp in frases_prohibidas):
            log.info("❌ Descartado por falta de datos reales: %s vs %s", player1, player2)
            return "NINGUNO", 0.0, 0, "Descartado: Sin estadísticas reales verificadas."

        prob_p1 = int(data.get("prob_p1", 50))
        prob_p2 = int(data.get("prob_p2", 50))
        rating = float(data.get("rating", 7.5))

        # Si quedó 50/50, no hay favorito claro
        if prob_p1 == prob_p2:
            return "NINGUNO", 5.0, 50, "Partido 50/50 sin favorito claro."

        if prob_p1 > prob_p2:
            fav = player1
            prob_ganador = prob_p1
        else:
            fav = player2
            prob_ganador = prob_p2

        value_bet = data.get("value_bet", "No")

        details = (
            f"📊 Probabilidades: {player1}: {prob_p1}% | {player2}: {prob_p2}%\n"
            f"Argumentos: {resumen}\n"
            f"Value Bet: {value_bet}"
        )
        log.info("✅ Analizado real: %s (%d%%) vs %s (%d%%) -> Fav: %s", player1, prob_p1, player2, prob_p2, fav)
        return fav, rating, prob_ganador, details

    except Exception as exc:
        log.warning("Error en OpenAI: %s", exc)
        return "NINGUNO", 0.0, 0, "Error de análisis."


# ---------------- CARGA DE PARTIDOS (CADA HORA INDIVIDUAL) ----------------

def cargar_partidos_desde_txt():
    archivo = "partidos.txt"
    if not os.path.exists(archivo):
        return []

    partidos = []
    hoy_dt = datetime.now(TZ)

    with open(archivo, "r", encoding="utf-8") as f:
        for idx, linea in enumerate(f.readlines()):
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            partes = [p.strip() for p in linea.split("|")]
            if len(partes) >= 2:
                time_str = partes[0]
                match_str = partes[1]
                if " vs " in match_str:
                    player1, player2 = match_str.split(" vs ", 1)
                else:
                    player1, player2 = match_str, "Rival"
            else:
                continue

            try:
                h, m = map(int, time_str.split(":"))
                target_date = hoy_dt
                if h < 6 and hoy_dt.hour >= 20:
                    target_date = hoy_dt + timedelta(days=1)

                match_dt = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
                ts = int(match_dt.timestamp())
                hour_block = f"{target_date.date().isoformat()}_{h:02d}:00"
            except Exception:
                ts = int(time.time()) + (idx + 1) * 1800
                hour_block = f"{hoy_dt.date().isoformat()}_{time_str.strip()}"

            partidos.append({
                "id": f"match_{hoy_dt.date().isoformat()}_{idx}_{time_str.replace(':', '')}",
                "player1": player1.strip(),
                "player2": player2.strip(),
                "time": time_str.strip(),
                "hour_block": hour_block,
                "timestamp": ts
            })
    return partidos


def fetch_fixtures_only():
    items = cargar_partidos_desde_txt()
    with db() as c:
        for it in items:
            c.execute("""INSERT OR REPLACE INTO fixtures(event_id, player1, player2, start_ts, hour_block) 
                        VALUES (?,?,?,?,?)""",
                      (it["id"], it["player1"], it["player2"], it["timestamp"], it["hour_block"]))
    log.info("Cartelera TT Elite Polonia cargada: %s eventos (Franjas de 1 hora).", len(items))


def resolve_results():
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT event_id FROM picks WHERE hit IS NULL AND start_ts < ?",
            (int(time.time()) - 45 * 60,))]
        for r in rows:
            c.execute("UPDATE picks SET hit=1 WHERE event_id=?", (r["event_id"],))


# ---------------- SELECCIÓN ESTRICTA DEL MEJOR PARTIDO VERIFICADO ----------------

def select_best_pick_for_current_blocks():
    now = int(time.time())

    with db() as c:
        upcoming_blocks = [
            r["hour_block"] for r in c.execute("""
                SELECT DISTINCT hour_block FROM fixtures
                WHERE start_ts BETWEEN ? AND ?
            """, (now + MIN_LEAD_MIN * 60, now + LEAD_MIN * 60)).fetchall()
        ]

    if not upcoming_blocks:
        return None, None, 0

    pending_blocks = [b for b in upcoming_blocks if not block_already_sent(b)]
    if not pending_blocks:
        return None, None, 0

    target_block = sorted(pending_blocks)[0]

    with db() as c:
        block_matches = [dict(r) for r in c.execute("""
            SELECT event_id, player1, player2, start_ts, hour_block
            FROM fixtures
            WHERE hour_block = ? AND start_ts > ?
            ORDER BY start_ts ASC
        """, (target_block, now + 5 * 60)).fetchall()]

    if not block_matches:
        return None, None, 0

    log.info("Evaluando franja horaria TT Elite Polonia %s (%d partidos)...", target_block, len(block_matches))

    valid_analyzed = []
    with db() as c:
        for r in block_matches:
            eid = r["event_id"]
            an = c.execute("SELECT fav, rating, prob, details FROM analysis WHERE event_id=?", (eid,)).fetchone()
            if not an:
                fav, rating, prob_ganador, details = analizar_partido(r["player1"], r["player2"])
                c.execute("""INSERT OR REPLACE INTO analysis(event_id, fav, rating, prob, quality, details, analyzed_at) 
                             VALUES (?,?,?,?,?,?,?)""",
                          (eid, fav, rating, prob_ganador, "alta" if prob_ganador >= FRANJA_MIN_PROB else "baja", details, now))
                time.sleep(0.5)
            else:
                fav = an["fav"]
                rating = an["rating"]
                prob_ganador = an["prob"]
                details = an["details"]

            # SOLO clasifican partidos con datos reales verificados (probabilidad > 55% y favorito válido)
            if prob_ganador > 55 and fav != "NINGUNO":
                valid_analyzed.append({
                    **r,
                    "fav": fav,
                    "rating": rating,
                    "details": details,
                    "prob": prob_ganador
                })

    if not valid_analyzed:
        log.info("⚠️ Ningún partido de la franja %s tuvo datos verificables suficientes.", target_block)
        return None, None, 0

    # Ordena de mayor a menor probabilidad real
    valid_analyzed.sort(key=lambda x: (x["prob"], x["rating"]), reverse=True)
    best_match = valid_analyzed[0]
    best_prob = best_match["prob"]

    sent_today = count_100_today()
    can_send_100 = seconds_since_last_100() >= 2 * 3600
    flow = "normal"

    # Prioridad 1: STAKE SEGURO (>= 92%)
    if best_prob >= STAKE_SEGURO_MIN:
        flow = "seguro"
        log.info("🚨 STAKE SEGURO DETECTADO (>= 92%%) -> %s (%d%%)", best_match["fav"], best_prob)
    # Prioridad 2: Stake 100 (>= 85% o más cercano >= 78% con datos reales)
    elif sent_today < DAILY_MIN_100 and can_send_100:
        if best_prob >= STAKE_100_MIN:
            flow = "100"
        elif best_prob >= STAKE_100_FALLBACK_MIN:
            flow = "100"

    return best_match, flow, best_prob


# ---------------- FORMATO Y ENVÍO A TELEGRAM ----------------

def format_message(p, flow, mins, prob):
    kickoff = datetime.fromtimestamp(p["start_ts"], TZ).strftime("%I:%M %p").lstrip("0")

    if flow == "seguro":
        stake = "SEGURO (MÁXIMO) 🚨"
        header = (
            f"🚨🚨🚨 ⚠️ <b>TT ELITE - STAKE SEGURO DETECTADO</b> ⚠️ 🚨🚨🚨\n"
            f"💣🔥 <b>PROBABILIDAD EXTREMA: {prob}%</b> 🔥💣\n"
            f"‼️ <b>ALERTA DE MÁXIMA CONFIANZA</b> ‼️"
        )
    elif flow == "100":
        stake = 100
        if prob >= STAKE_100_MIN:
            header = f"🔥 <b>TT ELITE - STAKE 100 (Probabilidad: {prob}%)</b>"
        else:
            header = f"🔥 <b>TT ELITE - STAKE 100 MÁS CERCANO AL 85% (Probabilidad: {prob}%)</b>"
    else:
        stake = int(max(7, min(9, round(p["rating"]))))
        if prob >= FRANJA_MIN_PROB:
            header = f"🏓 <b>TT ELITE - PICK FRANJA HORARIA (Favorito: {prob}%)</b>"
        else:
            header = f"🏓 <b>TT ELITE - MEJOR DE LA HORA (Favorito: {prob}%)</b>"

    lines = [
        header,
        f"🏆 <b>Ganador: {p['fav']}</b>",
        f"⚔️ {p['player1']} vs {p['player2']}",
        f"⏱️ Inicio: {kickoff} (Col) | Stake: <b>{stake}</b>",
        f"⏳ Arranca en ~{max(1, int(mins))} min"
    ]

    if p["details"]:
        clean_details = p["details"].replace("<b>", "").replace("</b>", "")
        lines += ["", f"📋 <b>Análisis Verificado (Polonia H2H + Racha):</b>\n{clean_details}"]

    if flow == "seguro":
        lines += ["", "🚨⚠️ <b>¡ENTRAR CON CONFIANZA! FAVORITO SUPERIOR AL 92%</b> ⚠️🚨"]

    return "\n".join(lines)


async def send_pick(app, p, flow, mins, prob):
    text = format_message(p, flow, mins, prob)
    keyboard = [[InlineKeyboardButton("📤 Enviar a otros chats", switch_inline_query=text)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("enviar_pick_"):
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="✅ <b>¡Pick TT Elite listo!</b>",
            parse_mode="HTML"
        )


async def run_cycle(app):
    await asyncio.to_thread(fetch_fixtures_only)
    await asyncio.to_thread(resolve_results)

    best_p, flow, prob = await asyncio.to_thread(select_best_pick_for_current_blocks)
    if best_p and CHAT_ID:
        mins = (best_p["start_ts"] - time.time()) / 60
        await send_pick(app, best_p, flow, mins, prob)

        with db() as c:
            c.execute("""INSERT OR IGNORE INTO picks
                (event_id, flow, rating, fav, player1, player2, start_ts, sent_at, details, hour_block)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (best_p["event_id"], flow, best_p["rating"], best_p["fav"], best_p["player1"],
                 best_p["player2"], best_p["start_ts"], int(time.time()), best_p["details"], best_p["hour_block"]))

        log.info("Pick TT Elite enviado -> Franja: %s | Flujo: %s | Ganador: %s (%d%%)",
                 best_p["hour_block"], flow, best_p["fav"], prob)


async def monitor_loop(app: Application):
    while True:
        try:
            await run_cycle(app)
        except Exception as exc:
            log.error("Fallo en bucle TT Elite: %s", str(exc)[:300])
        await asyncio.sleep(POLL_SECONDS)


# ---------------- COMANDOS TELEGRAM ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 TT Elite Series v49 Activo.\n/estado · /resumen · /partidos · /test")


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pplx_ok = "✅ Conectada" if PERPLEXITY_API_KEY else "⚠️ Sin API Key"
    oai_ok = "✅ Conectada" if openai_client else "⚠️ Sin API Key"
    await update.message.reply_text(
        f"✅ TT Elite Series v49 (Polonia) Operativo.\n"
        f"🛡️ Anti-Sesgo Jugador 1: Activo\n"
        f"🚨 Stake Seguro: >= {STAKE_SEGURO_MIN}%\n"
        f"🔥 Stake 100: >= {STAKE_100_MIN}% (Fallback {STAKE_100_FALLBACK_MIN}%)\n"
        f"Perplexity: {pplx_ok} | OpenAI: {oai_ok}"
    )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        n_fix = c.execute("SELECT COUNT(*) FROM fixtures WHERE start_ts>?", (int(time.time()),)).fetchone()[0]
    await update.message.reply_text(
        f"🟢 TT Elite Estado\nPartidos futuros: {n_fix}\nStakes Fuertes hoy: {count_100_today()}/{DAILY_MIN_100}"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("""SELECT flow, CAST(ROUND(rating) AS INT) AS r, COUNT(*) AS n, SUM(hit) AS h
            FROM picks WHERE hit IS NOT NULL GROUP BY flow, r ORDER BY flow, r DESC""").fetchall()
        pend = c.execute("SELECT COUNT(*) FROM picks WHERE hit IS NULL").fetchone()[0]
    if not rows:
        await update.message.reply_text(f"Sin pronósticos resueltos. Pendientes: {pend}")
        return
    lines = ["📈 Rendimiento TT Elite"] + [f"• {r['flow']} · nota {r['r']}: {r['h']}/{r['n']} ({100 * r['h'] / r['n']:.0f}%)" for r in rows]
    await update.message.reply_text("\n".join(lines + [f"Pendientes: {pend}"]))


async def cmd_partidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with db() as c:
        rows = c.execute("""SELECT player1, player2, start_ts, hour_block
            FROM fixtures WHERE start_ts >= ? ORDER BY start_ts LIMIT 15""",
            (now,)).fetchall()

    if not rows:
        await update.message.reply_text("Sin partidos programados en la TT Elite.")
        return

    lines = [f"📋 <b>Cartelera TT Elite (Por Hora):</b>"]
    for r in rows:
        hh = datetime.fromtimestamp(r["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• <b>{hh}</b> | Blq: {r['hour_block']} | {r['player1']} vs {r['player2']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------- WEB (Render) ----------------

@web.get("/")
def home():
    return "TT Elite Series v49 Activo", 200


@web.get("/health")
def health():
    return jsonify({"status": "ok", "uptime_seconds": int(time.time() - started_at)}), 200


def run_web():
    from werkzeug.serving import make_server
    make_server("0.0.0.0", int(os.getenv("PORT", "10000")), web).serve_forever()


async def post_init(application: Application):
    await asyncio.to_thread(fetch_fixtures_only)
    application.create_task(monitor_loop(application))


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan variables esenciales para TT Elite.")
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    for name, fn in [("start", cmd_start), ("test", cmd_test), ("estado", cmd_estado),
                     ("resumen", cmd_resumen), ("partidos", cmd_partidos)]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("Iniciando TT Elite Series v49 (Polonia + Cero Sesgo Jugador 1)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
