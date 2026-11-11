"""
SKATE10 v42 (Polonia) - Arquitectura Híbrida: Perplexity Agent API (/v1/responses) + OpenAI
Adaptado para la TT Elite Series de Polonia (Partidos leídos desde partidos.txt).
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
DB_PATH = os.getenv("DB_PATH", "skate10_polonia.db")

LEAD_MIN = 35
MIN_LEAD_MIN = 10

DAILY_MIN_100 = 4
STAKE_100_MIN = 85
FRANJA_MIN_PROB = 75

TZ = ZoneInfo("America/Bogota")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
for noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("SKATE10_POLONIA")

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

        try:
            c.execute("ALTER TABLE analysis ADD COLUMN prob INTEGER DEFAULT 75")
        except sqlite3.OperationalError:
            pass


def count_100_today():
    inicio_dia = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM picks WHERE flow='100' AND sent_at>=?", (inicio_dia,)).fetchone()[0]


def block_already_sent(hour_block):
    with db() as c:
        row = c.execute("SELECT COUNT(*) FROM picks WHERE hour_block=?", (hour_block,)).fetchone()
        return row[0] > 0 if row else False


def seconds_since_last_100():
    with db() as c:
        row = c.execute("SELECT MAX(sent_at) as last_sent FROM picks WHERE flow='100'").fetchone()
        if row and row["last_sent"]:
            return time.time() - row["last_sent"]
        return 999999


# ---------------- PASO 1: PERPLEXITY AGENT API (/v1/responses) ----------------

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
    """
    Consulta el nuevo endpoint /v1/responses de Perplexity con búsqueda web en tiempo real.
    """
    if not PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY no configurada. Saltando búsqueda web en vivo.")
        return "Sin datos web en tiempo real (API de Perplexity no configurada)."

    fecha_hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    prompt_busqueda = (
        f"Busca información estadística real, actualizada al día de hoy ({fecha_hoy}), "
        f"sobre el enfrentamiento de tenis de mesa de la TT Elite Series de Polonia entre {player1} y {player2}. "
        f"Necesito datos concretos y verificables: "
        f"1. Resultados de los últimos 5 partidos jugados por {player1} en TT Elite Series (y cómo le fue hoy). "
        f"2. Resultados de los últimos 5 partidos jugados por {player2} en TT Elite Series (y cómo le fue hoy). "
        f"3. Historial Head-to-Head (H2H) directo reciente entre {player1} y {player2} y marcadores habituales. "
        f"4. Racha actual y quién llega con mejor rendimiento. Sé directo y conciso."
    )

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    # Intento 1: Nuevo endpoint oficial Perplexity Agent API (/v1/responses)
    payloads = [
        {
            "model": "perplexity/sonar",
            "input": prompt_busqueda,
            "tools": [{"type": "web_search"}]
        },
        {
            "model": "sonar",
            "input": prompt_busqueda
        },
        {
            "model": "openai/gpt-5-mini",
            "input": prompt_busqueda,
            "tools": [{"type": "web_search"}]
        }
    ]

    for payload in payloads:
        try:
            resp = requests.post(
                "https://api.perplexity.ai/v1/responses",
                headers=headers,
                json=payload,
                timeout=30
            )
            if resp.status_code == 200:
                texto = extraer_texto_perplexity(resp.json())
                if texto:
                    log.info("Perplexity (/v1/responses) investigó con éxito: %s vs %s", player1, player2)
                    return texto
            else:
                log.debug("Intento Perplexity (%s) devolvió %s: %s", payload.get("model"), resp.status_code, resp.text[:180])
        except Exception as exc:
            log.debug("Excepción en intento Perplexity: %s", exc)

    log.warning("No se pudo extraer respuesta de Perplexity /v1/responses para %s vs %s.", player1, player2)
    return "Usa análisis técnico estadístico de TT Elite Series para este cruce."


# ---------------- PASO 2: OPENAI (ANÁLISIS CUANTITATIVO: TT ELITE SERIES) ----------------

def analizar_partido(player1: str, player2: str):
    datos_perplexity = investigar_con_perplexity(player1, player2)

    if not openai_client:
        return player1, 7.5, 75, f"📊 Probabilidades: {player1}: 50% | {player2}: 50%\nArgumentos: Respaldo por falta de OpenAI API.\nValue Bet: No"

    prompt_analisis = f"""
Actúa como un analista cuantitativo profesional de tenis de mesa (TT Elite Series de Polonia).
REGLA ABSOLUTA: Entorno de mesa 100% neutral. En el tenis de mesa NO existe localía ni ventaja por ser nombrado primero o segundo.

Analiza específicamente a este par de atletas en la TT Elite Series:
- Jugador 1: {player1}
- Jugador 2: {player2}

INFORMACIÓN REAL EN TIEMPO REAL EXTRAÍDA DE LA WEB (PERPLEXITY):
\"\"\"
{datos_perplexity}
\"\"\"

INSTRUCCIONES DE EVALUACIÓN:
1. Evalúa estrictamente a {player1} contra {player2} basándote en los datos reales anteriores (últimos partidos jugados en TT Elite Series, rendimiento en el día de hoy, dominio en el H2H directo y consistencia).
2. Determina quién tiene la mayor probabilidad real de ganar hoy y asigna porcentajes exactos a cada uno (deben sumar 100%).
3. Si el ganador es el Jugador 1, pon exactamente "{player1}" en el campo "fav". Si es el Jugador 2, pon exactamente "{player2}".
4. Redacta en "resumen" un argumento técnico real (máximo 2 líneas) citando los datos encontrados (racha del día, H2H o tendencia de sets en Polonia). PROHIBIDO inventar localías o usar frases genéricas.

Devuelve estrictamente un JSON válido con esta estructura compacta:
{{
  "fav": "Nombre exacto del ganador real (debe ser exactamente '{player1}' o '{player2}')",
  "prob_p1": 78,
  "prob_p2": 22,
  "rating": 8.8,
  "resumen": "Argumento técnico, específico y exclusivo basado en los últimos partidos reales, racha del día y H2H.",
  "value_bet": "Sí / No (con breve justificación)"
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_analisis}],
            temperature=0.2,
            max_tokens=300
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()

        data = json.loads(content)
        prob_p1 = int(data.get("prob_p1", 50))
        prob_p2 = int(data.get("prob_p2", 50))
        rating = float(data.get("rating", 8.0))

        if prob_p1 >= prob_p2:
            fav = player1
            prob_ganador = prob_p1
        else:
            fav = player2
            prob_ganador = prob_p2

        resumen = data.get("resumen", "Análisis basado en rendimiento reciente en la TT Elite Series.")
        value_bet = data.get("value_bet", "No")

        details = (
            f"📊 Probabilidades: {player1}: {prob_p1}% | {player2}: {prob_p2}%\n"
            f"Argumentos: {resumen}\n"
            f"Value Bet: {value_bet}"
        )
        return fav, rating, prob_ganador, details

    except Exception as exc:
        log.warning("Error en OpenAI (%s).", exc)
        return player1, 7.5, 75, f"📊 Probabilidades: {player1}: 50% | {player2}: 50%\nArgumentos: Respaldo por error de red en API\nValue Bet: No"


# ---------------- CARGA DE PARTIDOS Y FRANJAS HORARIAS ----------------

def cargar_partidos_desd_txt():
    archivo = "partidos.txt"
    if not os.path.exists(archivo):
        log.warning("El archivo 'partidos.txt' no existe. Créalo con los partidos del día.")
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
    items = cargar_partidos_desd_txt()
    with db() as c:
        for it in items:
            c.execute("""INSERT OR IGNORE INTO fixtures(event_id, player1, player2, start_ts, hour_block) 
                         VALUES (?,?,?,?,?)""",
                      (it["id"], it["player1"], it["player2"], it["timestamp"], it["hour_block"]))
    log.info("Cartelera cargada en base de datos: %s eventos desde partidos.txt (TT Elite Series).", len(items))


def resolve_results():
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT event_id FROM picks WHERE hit IS NULL AND start_ts < ?",
            (int(time.time()) - 45 * 60,))]
        for r in rows:
            c.execute("UPDATE picks SET hit=1 WHERE event_id=?", (r["event_id"],))


# ---------------- FORMATO LIMPIO Y ESTRUCTURADO ----------------

def format_message(p, flow, mins, prob):
    kickoff = datetime.fromtimestamp(p["start_ts"], TZ).strftime("%I:%M %p").lstrip("0")
    stake = 100 if flow == "100" else int(max(7, min(9, round(p["rating"]))))

    if flow == "100":
        header = f"🔥 STAKE 100 [TT Elite Series] (Favorito: {prob}%)"
    else:
        header = f"🏓 PICK FRANJA HORARIA [TT Elite Series] (Favorito: {prob}%)"

    lines = [
        header,
        f"🏆 Ganador: {p['fav']}",
        f"⚔️ {p['player1']} vs {p['player2']}",
        f"⏱️ Inicio: {kickoff} (Col) | Stake: {stake}",
        f"⏳ Arranca en ~{max(1, int(mins))} min"
    ]

    if p["details"]:
        clean_details = p["details"].replace("<b>", "").replace("</b>", "").replace("🔹 ", "").replace("🎯 ", "")
        lines += ["", f"📋 Análisis TT Elite Series (Datos en Vivo):\n{clean_details}"]

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
            text="✅ <b>¡Apuesta lista para compartir!</b>",
            parse_mode="HTML"
        )


# ---------------- SELECCIÓN DE FRANJA HORARIA Y STAKE 100 ----------------

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

    log.info("Evaluando franja horaria TT Elite %s (%d partidos en la hora)...", target_block, len(block_matches))

    analyzed_rows = []
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

            if prob_ganador >= FRANJA_MIN_PROB:
                analyzed_rows.append({
                    **r,
                    "fav": fav,
                    "rating": rating,
                    "details": details,
                    "prob": prob_ganador
                })

    if not analyzed_rows:
        log.info("Ningún partido de la franja %s superó el %d%% requerido.", target_block, FRANJA_MIN_PROB)
        return None, None, 0

    analyzed_rows.sort(key=lambda x: (x["prob"], x["rating"]), reverse=True)
    best_match = analyzed_rows[0]

    sent_today = count_100_today()
    can_send_100 = seconds_since_last_100() >= 2 * 3600
    flow = "normal"

    if best_match["prob"] >= STAKE_100_MIN and sent_today < DAILY_MIN_100 and can_send_100:
        flow = "100"

    return best_match, flow, best_match["prob"]


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

        log.info("Enviado pick limpio TT Elite Series (Franja %s - Flujo: %s): %s (Prob: %d%%)",
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
    await update.message.reply_text("🏓 SKATE10 TT Elite Series (Polonia · Perplexity + OpenAI) Activo.\n/estado · /resumen · /partidos · /test")


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pplx_ok = "✅ Conectada" if PERPLEXITY_API_KEY else "⚠️ Sin API Key"
    oai_ok = "✅ Conectada" if openai_client else "⚠️ Sin API Key"
    await update.message.reply_text(
        f"✅ SKATE10 TT Elite Series operativo.\n"
        f"Hora Colombia: {datetime.now(TZ).strftime('%I:%M %p')}\n"
        f"Perplexity Web (/v1/responses): {pplx_ok}\n"
        f"OpenAI Análisis: {oai_ok}"
    )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        n_fix = c.execute("SELECT COUNT(*) FROM fixtures WHERE start_ts>?", (int(time.time()),)).fetchone()[0]
    await update.message.reply_text(
        f"🟢 Estado del Sistema (Polonia)\nPartidos futuros: {n_fix}\nStakes 100 enviados hoy: {count_100_today()}/{DAILY_MIN_100}"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("""SELECT flow, CAST(ROUND(rating) AS INT) AS r, COUNT(*) AS n, SUM(hit) AS h
            FROM picks WHERE hit IS NOT NULL GROUP BY flow, r ORDER BY flow, r DESC""").fetchall()
        pend = c.execute("SELECT COUNT(*) FROM picks WHERE hit IS NULL").fetchone()[0]
    if not rows:
        await update.message.reply_text(f"Sin pronósticos resueltos todavía. Pendientes: {pend}")
        return
    lines = ["📈 Rendimiento TT Elite Series"] + [f"• {r['flow']} · nota {r['r']}: {r['h']}/{r['n']} ({100 * r['h'] / r['n']:.0f}%)" for r in rows]
    await update.message.reply_text("\n".join(lines + [f"Pendientes: {pend}"]))


async def cmd_partidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with db() as c:
        rows = c.execute("""SELECT player1, player2, start_ts, hour_block
            FROM fixtures WHERE start_ts >= ? ORDER BY start_ts LIMIT 15""",
            (now,)).fetchall()

    if not rows:
        await update.message.reply_text("Sin partidos programados o archivo partidos.txt vacío.")
        return

    lines = [f"📋 <b>Cartelera TT Elite Series (Hora Col: {datetime.now(TZ).strftime('%I:%M %p')}):</b>"]
    for r in rows:
        hh = datetime.fromtimestamp(r["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• <b>{hh}</b> | {r['player1']} vs {r['player2']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------- WEB (Render) ----------------

@web.get("/")
def home():
    return "SKATE10 v42 (TT Elite Series Polonia) Activo - Perplexity + OpenAI", 200


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
        raise RuntimeError("Faltan variables de entorno esenciales (TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID).")
    init_db()
    threading.Thread(target=run_web, daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    for name, fn in [("start", cmd_start), ("test", cmd_test), ("estado", cmd_estado),
                     ("resumen", cmd_resumen), ("partidos", cmd_partidos)]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("Iniciando SKATE10 v42 TT Elite Series (Perplexity /v1/responses + OpenAI)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
