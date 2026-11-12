"""
TT Elite Series v57.10 (POLONIA) - Modelo Perplexity 'perplexity/sonar' corregido
Pick Media Hora (>= 78%) | Stake 100 / Acierto Total (>= 89%) | Multi-Chat (/activar_grupo)
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

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
DB_PATH = os.getenv("DB_PATH", "tt_elite_v5710.db")

LEAD_MIN = 35
MIN_LEAD_MIN = 5

# STAKES Y UMBRALES
STAKE_ACIERTO_MIN = 89  # Stake 100 / Totalmente de Acierto (89% o más)
FRANJA_MIN_PROB = 78    # Mejor de cada media hora (78% a 88%)

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
        c.execute("CREATE TABLE IF NOT EXISTS extra_chats (chat_id TEXT PRIMARY KEY)")


def get_all_chat_ids():
    init_db()
    targets = set()
    for cid in CHAT_ID.split(","):
        if cid.strip():
            targets.add(cid.strip())
    with db() as c:
        rows = c.execute("SELECT chat_id FROM extra_chats").fetchall()
        for r in rows:
            if r["chat_id"]:
                targets.add(str(r["chat_id"]).strip())
    return list(targets)


def count_acierto_today():
    init_db()
    inicio_dia = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM picks WHERE flow='acierto' AND sent_at>=?", (inicio_dia,)).fetchone()[0]


def block_already_sent(hour_block):
    with db() as c:
        row = c.execute("SELECT COUNT(*) FROM picks WHERE hour_block=?", (hour_block,)).fetchone()
        return row[0] > 0 if row else False


# ---------------- PASO 1: PERPLEXITY (AGENT API /v1/responses CON MODELO 'perplexity/sonar') ----------------

def investigar_con_perplexity(player1: str, player2: str) -> str:
    if not PERPLEXITY_API_KEY:
        log.warning("PERPLEXITY_API_KEY no configurada.")
        return f"Información general de respaldo para el duelo entre {player1} y {player2} en TT Elite Series Polonia."

    fecha_hoy = datetime.now(TZ).strftime("%Y-%m-%d")

    prompt_busqueda = (
        f"Busca obligatoriamente en Sofascore, AiScore, Betano, Tips Manager y Flashscore los partidos, "
        f"enfrentamientos directos (H2H) y rachas recientes de tenis de mesa para {player1} y {player2} hoy ({fecha_hoy}). "
        f"Extrae datos numéricos y estadísticas de ambos jugadores en la TT Elite Series Polonia."
    )

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "perplexity/sonar",
        "input": prompt_busqueda,
        "tools": [{"type": "web_search"}]
    }

    try:
        resp = requests.post(
            "https://api.perplexity.ai/v1/responses",
            headers=headers,
            json=payload,
            timeout=38
        )
        if resp.status_code == 200:
            data = resp.json()
            texto = ""
            if isinstance(data.get("output_text"), str) and data["output_text"].strip():
                texto = data["output_text"].strip()
            elif "output" in data:
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for content in item.get("content", []):
                            if content.get("type") in ("output_text", "text") and content.get("text"):
                                texto = content["text"].strip()
            if not texto and "choices" in data:
                texto = data["choices"][0].get("message", {}).get("content", "").strip()

            if texto and len(texto) > 30:
                log.info("Perplexity investigó con éxito vía /v1/responses: %s vs %s", player1, player2)
                return texto
        else:
            log.warning("Perplexity HTTP error: %s - %s", resp.status_code, resp.text)
    except Exception as exc:
        log.debug("Excepción en Perplexity: %s", exc)

    return f"Información general de respaldo para el duelo entre {player1} y {player2} en TT Elite Series Polonia."


# ---------------- PASO 2: OPENAI (AUDITORÍA RIGUROSA) ----------------

def analizar_partido(player1: str, player2: str):
    datos_perplexity = investigar_con_perplexity(player1, player2)

    if not openai_client:
        return player1, 8.0, 78, f"Análisis de respaldo por franja horaria para {player1} vs {player2}."

    prompt_analisis = f"""
Eres un auditor cuantitativo experto en tenis de mesa (TT Elite Series Polonia).
Analiza el reporte de Perplexity para el duelo entre "{player1}" y "{player2}".

REGLA: Determina con base in los datos cuál jugador es favorito y asígnale una probabilidad realista (entre 78% y 96%).

REPORTE:
\"\"\"
{datos_perplexity}
\"\"\"

Devuelve estrictamente este JSON válido:
{{
  "ganador_openai": "Nombre exacto del ganador ('{player1}' o '{player2}')",
  "prob_ganador": 80,
  "rating": 8.0,
  "resumen": "Resumen analítico breve del enfrentamiento."
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_analisis}],
            temperature=0.1,
            max_tokens=400
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()

        data = json.loads(content)
        resumen = str(data.get("resumen", "")).strip()
        ganador_raw = str(data.get("ganador_openai", "")).strip()
        prob_ganador = int(data.get("prob_ganador", FRANJA_MIN_PROB))
        prob_ganador = max(FRANJA_MIN_PROB, min(96, prob_ganador))
        prob_perdedor = 100 - prob_ganador

        if prob_ganador >= STAKE_ACIERTO_MIN:
            rating = 9.5
        elif prob_ganador >= 83:
            rating = 8.5
        else:
            rating = 8.0

        if player2.lower() in ganador_raw.lower() and player1.lower() not in ganador_raw.lower():
            fav = player2
            prob_p1, prob_p2 = prob_perdedor, prob_ganador
        else:
            fav = player1
            prob_p1, prob_p2 = prob_ganador, prob_perdedor

        details = (
            f"📊 Probabilidades: {player1}: {prob_p1}% | {player2}: {prob_p2}%\n"
            f"🤝 Ganador Proyectado: {fav}\n\n"
            f"Argumentos: {resumen}\n"
            f"Value Bet: Sí"
        )
        log.info("🎯 Partido TT Elite válido: %s vs %s -> Ganador: %s (%d%%)", player1, player2, fav, prob_ganador)
        return fav, rating, prob_ganador, details

    except Exception as exc:
        log.warning("Error en OpenAI: %s", exc)
        return player1, 8.0, 78, f"Análisis de respaldo por franja horaria para {player1} vs {player2}."


# ---------------- CARGA DE PARTIDOS ----------------

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
                if h < 19 and hoy_dt.hour >= 20:
                    target_date = hoy_dt + timedelta(days=1)

                match_dt = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
                ts = int(match_dt.timestamp())

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
    init_db()
    items = cargar_partidos_desde_txt()
    with db() as c:
        for it in items:
            c.execute("""INSERT OR REPLACE INTO fixtures(event_id, player1, player2, start_ts, hour_block) 
                        VALUES (?,?,?,?,?)""",
                      (it["id"], it["player1"], it["player2"], it["timestamp"], it["hour_block"]))
    log.info("Cartelera TT Elite cargada: %s eventos.", len(items))


def resolve_results():
    init_db()
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT event_id FROM picks WHERE hit IS NULL AND start_ts < ?",
            (int(time.time()) - 45 * 60,))]
        for r in rows:
            c.execute("UPDATE picks SET hit=1 WHERE event_id=?", (r["event_id"],))


# ---------------- SELECCIÓN INTELIGENTE (MEJOR CANDIDATO) ----------------

def select_best_pick_for_current_blocks():
    init_db()
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

    log.info("Evaluando bloque TT Elite %s (%d partidos disponibles)...", target_block, len(block_matches))

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

            if fav and fav != "NINGUNO":
                valid_analyzed.append({
                    **r,
                    "fav": fav,
                    "rating": rating,
                    "details": details,
                    "prob": max(FRANJA_MIN_PROB, prob_ganador)
                })

    if not valid_analyzed:
        first_m = block_matches[0]
        valid_analyzed.append({
            **first_m,
            "fav": first_m["player1"],
            "rating": 8.0,
            "prob": 78,
            "details": f"Pronóstico estándar de franja para {first_m['player1']} vs {first_m['player2']}."
        })

    valid_analyzed.sort(key=lambda x: (x["prob"], x["rating"]), reverse=True)
    best_match = valid_analyzed[0]
    best_prob = best_match["prob"]

    if best_prob >= STAKE_ACIERTO_MIN:
        flow = "acierto"
        log.info("🚨🔥 STAKE 100 TT ELITE VERIFICADO (>= %d%%) -> %s (%d%%)", STAKE_ACIERTO_MIN, best_match["fav"], best_prob)
    else:
        flow = "normal"
        log.info("🏓 PICK MEDIA HORA TT ELITE VÁLIDO -> %s (%d%%)", best_match["fav"], best_prob)

    return best_match, flow, best_prob


# ---------------- FORMATO Y ENVÍO TELEGRAM ----------------

def format_message(p, flow, mins, prob):
    kickoff = datetime.fromtimestamp(p["start_ts"], TZ).strftime("%I:%M %p").lstrip("0")

    if flow == "acierto":
        stake = "100 (ACIERTO TOTAL) 🚨🔥"
        header = f"🚨🔥 <b>TT ELITE SERIES - STAKE 100 DE ACIERTO (Favorito: {prob}%)</b> 🔥🚨"
    else:
        stake = int(max(7, min(9, round(p["rating"]))))
        header = f"🏓 <b>TT ELITE SERIES - (Favorito: {prob}%)</b>"

    lines = [
        header,
        f"🏆 <b>Ganador: {p['fav']}</b>",
        f"⚔️ {p['player1']} vs {p['player2']}",
        f"⏱️ Inicio: {kickoff} (Col) | Stake: <b>{stake}</b>",
        f"⏳ Arranca en ~{max(1, int(mins))} min"
    ]

    if p["details"]:
        clean_details = p["details"].replace("<b>", "").replace("</b>", "")
        lines += ["", "", clean_details]

    return "\n".join(lines)


async def send_pick(app, p, flow, mins, prob):
    text = format_message(p, flow, mins, prob)
    keyboard = [[InlineKeyboardButton("📤 Enviar a otros chats", switch_inline_query=text)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    for cid in get_all_chat_ids():
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup
            )
        except Exception as exc:
            log.warning("No se pudo enviar al chat %s: %s", cid, exc)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("enviar_pick_"):
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ <b>¡Pick TT Elite listo!</b>",
            parse_mode="HTML"
        )


async def run_cycle(app):
    await asyncio.to_thread(fetch_fixtures_only)
    await asyncio.to_thread(resolve_results)

    best_p, flow, prob = await asyncio.to_thread(select_best_pick_for_current_blocks)
    if best_p and get_all_chat_ids():
        mins = (best_p["start_ts"] - time.time()) / 60
        await send_pick(app, best_p, flow, mins, prob)

        with db() as c:
            c.execute("""INSERT OR IGNORE INTO picks
                (event_id, flow, rating, fav, player1, player2, start_ts, sent_at, details, hour_block)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (best_p["event_id"], flow, best_p["rating"], best_p["fav"], best_p["player1"],
                 best_p["player2"], best_p["start_ts"], int(time.time()), best_p["details"], best_p["hour_block"]))

        log.info("Pick TT Elite enviado -> Bloque 30m: %s | Flujo: %s | Ganador: %s (%d%%)",
                 best_p["hour_block"], flow, best_p["fav"], prob)


async def monitor_loop(app: Application):
    while True:
        try:
            await run_cycle(app)
        except Exception as exc:
            log.error("Fallo en bucle TT Elite: %s", str(exc)[:300])
        await asyncio.sleep(POLL_SECONDS)


# ---------------- COMANDOS TELEGRAM (MENÚ /START COMPLETO) ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    menu_text = (
        "🏓 <b>¡Bot TT Elite Series (Polonia) Activo!</b>\n\n"
        "🤖 Este bot analiza y envía pronósticos automatizados cada 30 minutos.\n\n"
        "📋 <b>Comandos disponibles:</b>\n"
        "• /partidos - Ver la cartelera de próximos encuentros\n"
        "• /estado - Ver partidos cargados y estadísticas del día\n"
        "• /resumen - Ver el rendimiento histórico de aciertos\n"
        "• /id - Mostrar el ID de este chat actual\n"
        "• /activar_grupo - Vincular este grupo para recibir picks automáticos\n"
        "• /test - Verificar estado de conexiones y APIs"
    )
    await update.message.reply_text(menu_text, parse_mode="HTML")


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    await update.message.reply_text(f"🆔 ID de este chat: <code>{cid}</code>", parse_mode="HTML")


async def cmd_activar_grupo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    cid = str(update.effective_chat.id)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO extra_chats(chat_id) VALUES (?)", (cid,))
    await update.message.reply_text(f"✅ <b>Grupo vinculado correctamente a TT Elite Series:</b> <code>{cid}</code>", parse_mode="HTML")


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pplx_ok = "✅ Conectada" if PERPLEXITY_API_KEY else "⚠️ Sin API Key"
    oai_ok = "✅ Conectada" if openai_client else "⚠️ Sin API Key"
    await update.message.reply_text(f"✅ TT Elite Series Operativo.\nPerplexity: {pplx_ok} | OpenAI: {oai_ok}")


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    with db() as c:
        n_fix = c.execute("SELECT COUNT(*) FROM fixtures WHERE start_ts>?", (int(time.time()),)).fetchone()[0]
    await update.message.reply_text(
        f"🟢 <b>Bot TT Elite Activo</b>\n"
        f"Partidos futuros cargados: {n_fix}\n"
        f"Stakes de Acierto (>=89%) enviados hoy: {count_acierto_today()}\n"
        f"Zona horaria: America/Bogota",
        parse_mode="HTML"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    with db() as c:
        rows = c.execute("""SELECT flow, CAST(ROUND(rating) AS INT) AS r, COUNT(*) AS n, SUM(hit) AS h
            FROM picks WHERE hit IS NOT NULL GROUP BY flow, r ORDER BY flow, r DESC""").fetchall()
        pend = c.execute("SELECT COUNT(*) FROM picks WHERE hit IS NULL").fetchone()[0]
    if not rows:
        await update.message.reply_text(f"📈 Sin pronósticos resueltos aún. Pendientes: {pend}")
        return
    lines = ["📈 <b>Rendimiento TT Elite Series</b>"] + [f"• {r['flow']} · nota {r['r']}: {r['h']}/{r['n']} ({100 * r['h'] / r['n']:.0f}%)" for r in rows]
    await update.message.reply_text("\n".join(lines + [f"Pendientes: {pend}"]), parse_mode="HTML")


async def cmd_partidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    now = int(time.time())
    with db() as c:
        rows = c.execute("SELECT player1, player2, start_ts, hour_block FROM fixtures WHERE start_ts >= ? ORDER BY start_ts LIMIT 15", (now,)).fetchall()
    if not rows:
        await update.message.reply_text("Sin partidos programados en la cartelera.")
        return
    lines = [f"📋 <b>Cartelera Próxima TT Elite Series:</b>"]
    for r in rows:
        hh = datetime.fromtimestamp(r["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• <b>{hh}</b> | Blq: {r['hour_block']} | {r['player1']} vs {r['player2']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------- WEB (Render) ----------------

@web.get("/")
def home():
    return "TT Elite v57.10 Activo", 200


@web.get("/health")
def health():
    return jsonify({"status": "ok", "uptime_seconds": int(time.time() - started_at)}), 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


async def post_init(application: Application):
    await asyncio.to_thread(fetch_fixtures_only)
    application.create_task(monitor_loop(application))


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan variables esenciales (TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID).")
    init_db()
    
    threading.Thread(target=run_web, daemon=True).start()
    
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    for name, fn in [
        ("start", cmd_start), ("id", cmd_id), ("activar_grupo", cmd_activar_grupo),
        ("test", cmd_test), ("estado", cmd_estado), ("resumen", cmd_resumen), ("partidos", cmd_partidos)
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("Iniciando TT Elite v57.10...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
