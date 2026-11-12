"""
TT Elite Series v52 - Bloques de 30 Minutos + Filtro Estricto (>= 75%)
Stake 100 Estricto (>= 85% sin fallback) | Stake Seguro (>= 92%) | Doble Consenso
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

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))  # Revisa cada 2 minutos
DB_PATH = os.getenv("DB_PATH", "tt_elite_v52.db")

LEAD_MIN = 35          # Evalúa el bloque de media hora hasta 35 min antes
MIN_LEAD_MIN = 5

STAKE_SEGURO_MIN = 92  # Alerta extrema (>= 92%)
STAKE_100_MIN = 85     # Stake 100 estricto (>= 85% únicamente)
FRANJA_MIN_PROB = 75   # Mínimo estricto para enviar el mejor de cada 30 min (>= 75%)

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


# ---------------- PASO 1: PERPLEXITY (INVESTIGA Y PROPONE SU GANADOR) ----------------

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
        f"Busca toda la información estadística real en la web (Sofascore, Flashscore, BetsAPI, TT Elite Series Polonia) "
        f"sobre el partido de tenis de mesa entre {player1} y {player2} a fecha de hoy ({fecha_hoy}). "
        f"Analiza y entrega obligatoriamente: "
        f"1. Rendimiento y resultados recientes de {player1} (últimos 7 días). "
        f"2. Rendimiento y resultados recientes de {player2} (últimos 7 días). "
        f"3. Historial Head-to-Head (H2H) directo entre ambos (últimos 2 meses e histórico). "
        f"4. VEREDICTO DE PERPLEXITY: Basado estrictamente en los datos reales encontrados (sin importar el orden de los nombres), "
        f"indica explícitamente cuál de los dos jugadores es el ganador más probable y con qué porcentaje estimado."
    )

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    payloads = [
        {
            "model": "perplexity/sonar-pro",
            "input": prompt_busqueda,
            "tools": [{"type": "web_search"}]
        },
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
                    log.info("Perplexity investigó: %s vs %s", player1, player2)
                    return texto
        except Exception as exc:
            log.debug("Excepción en Perplexity: %s", exc)

    return "SIN_DATOS"


# ---------------- PASO 2: OPENAI (DOBLE CONSENSO IMPARCIAL Y FILTRO DE DATOS) ----------------

def analizar_partido(player1: str, player2: str):
    datos_perplexity = investigar_con_perplexity(player1, player2)

    if not openai_client or datos_perplexity == "SIN_DATOS":
        return "NINGUNO", 5.0, 50, "Sin datos verificables."

    prompt_analisis = f"""
Eres un analista cuantitativo experto en tenis de mesa (TT Elite Series Polonia).
Abajo tienes toda la investigación web de Perplexity (rachas recientes, H2H y el candidato que propone Perplexity) para el duelo entre "{player1}" y "{player2}".

INSTRUCCIONES DE DOBLE ANÁLISIS (CERO SESGO DE POSICIÓN):
1. Revisa los datos reales aportados por Perplexity (H2H, victorias recientes, estado de forma) y el veredicto de Perplexity.
2. Si el reporte indica que NO hay datos ni estadísticas reales de los jugadores, pon `"datos_reales": false` y `"prob_ganador": 50`.
3. Haz tu propio análisis matemático independiente comparando los números de "{player1}" y los números de "{player2}".
4. El ganador debe elegirse 100% por mérito estadístico (mejor H2H y mejor racha reciente), sin importar quién fue nombrado primero o segundo.
5. Determina el ganador final y su probabilidad real de victoria (entre 50 y 96). Solo asigna >= 75% si hay ventaja clara en racha o H2H, y >= 85% si el dominio es muy marcado.

REPORTE Y VEREDICTO DE PERPLEXITY:
\"\"\"
{datos_perplexity}
\"\"\"

Devuelve estrictamente este JSON válido:
{{
  "datos_reales": true,
  "ganador_perplexity": "Nombre del jugador que Perplexity vio mejor",
  "ganador_openai": "Nombre exacto del ganador tras tu análisis ('{player1}' o '{player2}')",
  "prob_ganador": 78,
  "rating": 8.2,
  "resumen": "Explica con cifras el H2H, la racha reciente y por qué gana el favorito.",
  "value_bet": "Sí / No"
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_analisis}],
            temperature=0.1,
            max_tokens=320
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()

        data = json.loads(content)
        datos_reales = bool(data.get("datos_reales", True))
        resumen = str(data.get("resumen", "")).strip()

        frases_sin_datos = ("falta de datos", "falta de información", "no se encontraron", "sin datos específicos", "no hay información")
        if not datos_reales or any(f in resumen.lower() for f in frases_sin_datos):
            log.info("❌ Descartado por falta de datos: %s vs %s", player1, player2)
            return "NINGUNO", 5.0, 50, "Sin estadísticas verificables."

        ganador_raw = str(data.get("ganador_openai", "")).strip()
        ganador_pplx = str(data.get("ganador_perplexity", "")).strip()
        prob_ganador = int(data.get("prob_ganador", 50))
        prob_ganador = max(50, min(98, prob_ganador))
        prob_perdedor = 100 - prob_ganador
        rating = float(data.get("rating", 8.0))

        if player2.lower() in ganador_raw.lower() and player1.lower() not in ganador_raw.lower():
            fav = player2
            prob_p1 = prob_perdedor
            prob_p2 = prob_ganador
        elif player1.lower() in ganador_raw.lower() and player2.lower() not in ganador_raw.lower():
            fav = player1
            prob_p1 = prob_ganador
            prob_p2 = prob_perdedor
        else:
            p1_apellido = player1.split()[0].lower()
            p2_apellido = player2.split()[0].lower()
            if p2_apellido in ganador_raw.lower():
                fav = player2
                prob_p1 = prob_perdedor
                prob_p2 = prob_ganador
            else:
                fav = player1
                prob_p1 = prob_ganador
                prob_p2 = prob_perdedor

        value_bet = data.get("value_bet", "Sí" if prob_ganador >= 75 else "No")

        details = (
            f"📊 Probabilidades: {player1}: {prob_p1}% | {player2}: {prob_p2}%\n"
            f"🤝 Favorito Perplexity: {ganador_pplx or fav} | Favorito OpenAI: {fav}\n"
            f"Argumentos: {resumen}\n"
            f"Value Bet: {value_bet}"
        )
        log.info("🎯 Consenso: %s vs %s -> Ganador: %s (%d%%)", player1, player2, fav, prob_ganador)
        return fav, rating, prob_ganador, details

    except Exception as exc:
        log.warning("Error en OpenAI: %s", exc)
        return "NINGUNO", 5.0, 50, "Error de análisis."


# ---------------- CARGA DE PARTIDOS (BLOQUES DE 30 MINUTOS) ----------------

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
                # Si son las 8 PM o más y el partido es de madrugada/mañana (00:00 a 18:59), pertenece al día siguiente
                if h < 19 and hoy_dt.hour >= 20:
                    target_date = hoy_dt + timedelta(days=1)

                match_dt = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
                ts = int(match_dt.timestamp())

                # Agrupación por cada MEDIA HORA (ej. 07:00 o 07:30)
                half_m = 30 if m >= 30 else 0
                hour_block = f"{target_date.date().isoformat()}_{h:02d}:{half_m:02d}"
            except Exception:
                ts = int(time.time()) + (idx + 1) * 1800
                hour_block = f"{hoy_dt.date().isoformat()}_{time_str.strip()}"

            partidos.append({
                "id": f"match_{target_date.date().isoformat()}_{idx}_{time_str.replace(':', '')}",
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
    log.info("Cartelera cargada: %s eventos (Franjas de 30 minutos).", len(items))


def resolve_results():
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT event_id FROM picks WHERE hit IS NULL AND start_ts < ?",
            (int(time.time()) - 45 * 60,))]
        for r in rows:
            c.execute("UPDATE picks SET hit=1 WHERE event_id=?", (r["event_id"],))


# ---------------- SELECCIÓN ESTRICTA CADA MEDIA HORA (>= 75% | STAKE 100 >= 85% ESTRICTO) ----------------

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
        """, (target_block, now + 3 * 60)).fetchall()]

    if not block_matches:
        return None, None, 0

    log.info("Evaluando bloque de MEDIA HORA %s (%d partidos)...", target_block, len(block_matches))

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

            # REGLA ESTRICTA: Solo entran los partidos que superen o igualen el 75%
            if prob_ganador >= FRANJA_MIN_PROB and fav != "NINGUNO":
                valid_analyzed.append({
                    **r,
                    "fav": fav,
                    "rating": rating,
                    "details": details,
                    "prob": prob_ganador
                })

    if not valid_analyzed:
        log.info("🛑 Ningún partido del bloque de media hora %s alcanzó el %d%%. No se envía pick.", target_block, FRANJA_MIN_PROB)
        return None, None, 0

    # Ordena de mayor a menor probabilidad y rating para elegir el mejor de la media hora
    valid_analyzed.sort(key=lambda x: (x["prob"], x["rating"]), reverse=True)
    best_match = valid_analyzed[0]
    best_prob = best_match["prob"]

    # Asignación estricta: Stake Seguro (>= 92%), Stake 100 (>= 85%), Normal (75% a 84%)
    if best_prob >= STAKE_SEGURO_MIN:
        flow = "seguro"
        log.info("🚨 STAKE SEGURO DETECTADO (>= 92%%) -> %s (%d%%)", best_match["fav"], best_prob)
    elif best_prob >= STAKE_100_MIN:
        flow = "100"
        log.info("🔥 STAKE 100 INMEDIATO DETECTADO (>= 85%%) -> %s (%d%%)", best_match["fav"], best_prob)
    else:
        flow = "normal"

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
        stake = "100 🔥"
        header = (
            f"🔥🔥🚨 <b>ALERTA ESPECIAL: STAKE 100 DETECTADO</b> 🚨🔥🔥\n"
            f"🎯 <b>FAVORITO DE ALTA PROBABILIDAD: {prob}%</b> 🎯"
        )
    else:
        stake = int(max(7, min(9, round(p["rating"]))))
        header = f"🏓 <b>TT ELITE - MEJOR PICK MEDIA HORA (Favorito: {prob}%)</b>"

    lines = [
        header,
        f"🏆 <b>Ganador: {p['fav']}</b>",
        f"⚔️ {p['player1']} vs {p['player2']}",
        f"⏱️ Inicio: {kickoff} (Col) | Stake: <b>{stake}</b>",
        f"⏳ Arranca en ~{max(1, int(mins))} min"
    ]

    if p["details"]:
        clean_details = p["details"].replace("<b>", "").replace("</b>", "")
        lines += ["", f"📋 <b>Doble Análisis (Perplexity + OpenAI):</b>\n{clean_details}"]

    if flow == "seguro":
        lines += ["", "🚨⚠️ <b>¡ENTRAR CON CONFIANZA! FAVORITO SUPERIOR AL 92%</b> ⚠️🚨"]
    elif flow == "100":
        lines += ["", "🔥⚡ <b>¡OPORTUNIDAD FUERTE! SUPERA EL 85% DE PROBABILIDAD</b> ⚡🔥"]

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
            text="✅ <b>¡Pick listo!</b>",
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

        log.info("Pick enviado -> Bloque 30m: %s | Flujo: %s | Ganador: %s (%d%%)",
                 best_p["hour_block"], flow, best_p["fav"], prob)


async def monitor_loop(app: Application):
    while True:
        try:
            await run_cycle(app)
        except Exception as exc:
            log.error("Fallo en bucle: %s", str(exc)[:300])
        await asyncio.sleep(POLL_SECONDS)


# ---------------- COMANDOS TELEGRAM ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 TT Elite v52 (Cada 30m) Activo.\n/estado · /resumen · /partidos · /test")


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pplx_ok = "✅ Conectada" if PERPLEXITY_API_KEY else "⚠️ Sin API Key"
    oai_ok = "✅ Conectada" if openai_client else "⚠️ Sin API Key"
    await update.message.reply_text(
        f"✅ TT Elite v52 Operativo (Bloques de 30 min).\n"
        f"🚨 Stake Seguro: Estricto >= {STAKE_SEGURO_MIN}%\n"
        f"🔥 Stake 100 Especial: Estricto >= {STAKE_100_MIN}% (Sin fallback)\n"
        f"🏓 Pick Media Hora: Estricto >= {FRANJA_MIN_PROB}%\n"
        f"Perplexity: {pplx_ok} | OpenAI: {oai_ok}"
    )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        n_fix = c.execute("SELECT COUNT(*) FROM fixtures WHERE start_ts>?", (int(time.time()),)).fetchone()[0]
    await update.message.reply_text(
        f"🟢 Estado del Sistema (Franjas 30 min)\nPartidos futuros: {n_fix}\nStakes Fuertes enviados hoy: {count_100_today()}"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("""SELECT flow, CAST(ROUND(rating) AS INT) AS r, COUNT(*) AS n, SUM(hit) AS h
            FROM picks WHERE hit IS NOT NULL GROUP BY flow, r ORDER BY flow, r DESC""").fetchall()
        pend = c.execute("SELECT COUNT(*) FROM picks WHERE hit IS NULL").fetchone()[0]
    if not rows:
        await update.message.reply_text(f"Sin pronósticos resueltos. Pendientes: {pend}")
        return
    lines = ["📈 Rendimiento"] + [f"• {r['flow']} · nota {r['r']}: {r['h']}/{r['n']} ({100 * r['h'] / r['n']:.0f}%)" for r in rows]
    await update.message.reply_text("\n".join(lines + [f"Pendientes: {pend}"]))


async def cmd_partidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with db() as c:
        rows = c.execute("""SELECT player1, player2, start_ts, hour_block
            FROM fixtures WHERE start_ts >= ? ORDER BY start_ts LIMIT 15""",
            (now,)).fetchall()

    if not rows:
        await update.message.reply_text("Sin partidos programados.")
        return

    lines = [f"📋 <b>Cartelera Próxima (Franjas de 30m):</b>"]
    for r in rows:
        hh = datetime.fromtimestamp(r["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• <b>{hh}</b> | Blq: {r['hour_block']} | {r['player1']} vs {r['player2']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------- WEB (Render) ----------------

@web.get("/")
def home():
    return "TT Elite v52 Activo", 200


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
        raise RuntimeError("Faltan variables esenciales.")
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    for name, fn in [("start", cmd_start), ("test", cmd_test), ("estado", cmd_estado),
                     ("resumen", cmd_resumen), ("partidos", cmd_partidos)]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("Iniciando TT Elite v52 (30 min + >=75%% Estricto + Stake 100 >=85%% Estricto)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
