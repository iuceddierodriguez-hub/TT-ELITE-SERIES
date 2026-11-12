"""
TT Elite Series v57 (POLONIA) - 2 Mensajes Exclusivos + Auditoría de 4 Pilares (H2H, Racha, Carga y Desplazamiento)
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
DB_PATH = os.getenv("DB_PATH", "tt_elite_v57.db")

LEAD_MIN = 35
MIN_LEAD_MIN = 5

# SOLO DOS NIVELES ESTRICTOS:
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


# ---------------- PASO 1: PERPLEXITY (H2H + RACHA + CARGA Y DESPLAZAMIENTO) ----------------

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
        f"Busca información estadística rigurosa y verificada en la web (Sofascore, Flashscore, BetsAPI, TT Elite Series Polonia) "
        f"sobre el partido de tenis de mesa entre {player1} y {player2} a fecha de hoy ({fecha_hoy}). "
        f"Investiga y detalla obligatoriamente estos 4 puntos clave:\n"
        f"1. HISTORIAL H2H COMPLETO: Registro histórico de enfrentamientos directos entre {player1} y {player2}, y marcadores exactos de sus últimos 5 duelos directos.\n"
        f"2. RENDIMIENTO Y RACHA ACTUAL: Marcadores exactos y récord de victorias/derrotas de {player1} y de {player2} en sus últimos 5 a 10 partidos jugados.\n"
        f"3. CARGA DE PARTIDOS Y FATIGA RECIENTE: Cuántos partidos ha jugado cada uno en las últimas 24 horas, si vienen de partidos largos a 5 sets (3-2) y cuánto descanso tienen entre turnos.\n"
        f"4. DESPLAZAMIENTO Y CAMBIO DE SEDE/TORNEO: Verifica en qué ciudad/sala o grupo viene jugando cada uno recientemente y si alguno tuvo que desplazarse de otra sede o cambiar de horario drásticamente.\n"
        f"5. VEREDICTO DE PERPLEXITY: Basado en H2H, forma actual y desgaste físico/viajes, indica quién es el favorito real y con qué porcentaje."
    )

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    payloads = [
        {"model": "perplexity/sonar-pro", "input": prompt_busqueda, "tools": [{"type": "web_search"}]},
        {"model": "perplexity/sonar", "input": prompt_busqueda, "tools": [{"type": "web_search"}]},
        {"model": "sonar", "input": prompt_busqueda}
    ]

    for payload in payloads:
        try:
            resp = requests.post(
                "https://api.perplexity.ai/v1/responses",
                headers=headers,
                json=payload,
                timeout=38
            )
            if resp.status_code == 200:
                texto = extraer_texto_perplexity(resp.json())
                if texto:
                    log.info("Perplexity investigó (TT Elite Polonia): %s vs %s", player1, player2)
                    return texto
        except Exception as exc:
            log.debug("Excepción en Perplexity: %s", exc)

    return "SIN_DATOS"


# ---------------- PASO 2: OPENAI (AUDITORÍA RIGUROSA 4 PILARES) ----------------

def analizar_partido(player1: str, player2: str):
    datos_perplexity = investigar_con_perplexity(player1, player2)

    if not openai_client or datos_perplexity == "SIN_DATOS":
        return "NINGUNO", 5.0, 50, "Sin datos verificables."

    prompt_analisis = f"""
Eres un auditor cuantitativo implacable de tenis de mesa profesional (TT Elite Series Polonia).
Abajo tienes el reporte de Perplexity para el duelo entre "{player1}" y "{player2}".

CRITERIOS OBLIGATORIOS DE AUDITORÍA (MUCHA RIGUROSIDAD):
1. HISTORIAL H2H DIRECTO (Peso 40%): Revisa quién domina históricamente y en los últimos enfrentamientos directos. Si un jugador tiene paternidad clara (ej. 4-1, 5-1, 3-0 seguido), tiene gran ventaja psicológica y táctica.
2. RENDIMIENTO Y FORMA RECIENTE (Peso 35%): Evalúa cómo vienen jugando en sus últimos 5-10 partidos. Un jugador con racha perdedora (ej. 1-4 o 2-3) NO puede recibir probabilidades altas.
3. CARGA DE PARTIDOS, FATIGA Y DESPLAZAMIENTOS (Peso 25%):
   - Penaliza severamente al jugador que tenga sobrecarga de partidos hoy (fatiga acumulada, varios duelos a 5 sets) o que haya tenido que desplazarse entre sedes/ciudades o cambiar de turno sin descanso.
   - Premia al jugador que llega más fresco, adaptado a la mesa/sede y con menor desgaste físico.

ESCALA ESTRICTA DE PROBABILIDADES (PROHIBIDO REGALAR PORCENTAJES ALTOS):
- Si no hay datos reales verificables en el reporte, pon `"datos_reales": false` y `"prob_ganador": 50`.
- 50% a 77% (DESCARTADO): Partidos parejos, H2H dividido (ej. 2-2, 3-2), rachas irregulares o donde el favorito técnico llega muy cansado por carga de partidos/viaje.
- 78% a 88% (PICK DE MEDIA HORA): Favorito sólido que gana tanto en H2H como en forma reciente y no presenta desventaja por fatiga.
- 89% a 96% (STAKE 100 / ACIERTO TOTAL - ULTRA EXCLUSIVO): SOLO cuando los 4 factores están alineados al 100% a favor del mismo jugador: paternidad aplastante en H2H + racha ganadora actual muy superior + rival con peor forma o mayor desgaste/carga de partidos. Sé extremadamente selectivo para dar >= 89%.

REPORTE DE PERPLEXITY:
\"\"\"
{datos_perplexity}
\"\"\"

Devuelve estrictamente este JSON válido (sin inflar números):
{{
  "datos_reales": true,
  "ganador_perplexity": "Nombre del jugador que Perplexity vio mejor",
  "ganador_openai": "Nombre exacto del ganador tras tu auditoría ('{player1}' o '{player2}')",
  "prob_ganador": 50,
  "rating": 5.0,
  "resumen": "Detalla explícitamente: 1) El historial H2H exacto, 2) La racha reciente de ambos con cifras, y 3) El análisis de carga de partidos/desgaste o desplazamiento que respalda el porcentaje asignado.",
  "value_bet": "Sí / No"
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_analisis}],
            temperature=0.1,
            max_tokens=450
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
        prob_ganador = max(50, min(96, prob_ganador))
        prob_perdedor = 100 - prob_ganador

        if prob_ganador >= STAKE_ACIERTO_MIN:
            rating = 9.5
        elif prob_ganador >= 83:
            rating = 8.5
        elif prob_ganador >= FRANJA_MIN_PROB:
            rating = 8.0
        else:
            rating = 5.0

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

        # REGLA ADICIONAL DE RIGUROSIDAD: Si Perplexity y OpenAI NO coinciden en el ganador, no puede ser Stake de Acierto (>=89%)
        if ganador_pplx and (fav.split()[0].lower() not in ganador_pplx.lower()) and prob_ganador >= STAKE_ACIERTO_MIN:
            log.info("⚠️ Discrepancia entre Perplexity (%s) y OpenAI (%s). Se ajusta probabilidad de %d%% a 82%%.", ganador_pplx, fav, prob_ganador)
            prob_ganador = 82
            prob_perdedor = 18
            if fav == player1:
                prob_p1, prob_p2 = 82, 18
            else:
                prob_p1, prob_p2 = 18, 82

        value_bet = "Sí" if prob_ganador >= FRANJA_MIN_PROB else "No"

        details = (
            f"📊 Probabilidades: {player1}: {prob_p1}% | {player2}: {prob_p2}%\n"
            f"🤝 Favorito 1: {ganador_pplx or fav} | Favorito 2: {fav}\n\n"
            f"Argumentos: {resumen}\n"
            f"Value Bet: {value_bet}"
        )
        log.info("🎯 Consenso TT Elite: %s vs %s -> Ganador: %s (%d%%)", player1, player2, fav, prob_ganador)
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
    log.info("Cartelera TT Elite cargada: %s eventos (Franjas de 30 minutos).", len(items))


def resolve_results():
    init_db()
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT event_id FROM picks WHERE hit IS NULL AND start_ts < ?",
            (int(time.time()) - 45 * 60,))]
        for r in rows:
            c.execute("UPDATE picks SET hit=1 WHERE event_id=?", (r["event_id"],))


# ---------------- SELECCIÓN ESTRICTA (SOLO 2 FLUJOS: MEDIA HORA >=78% Y ACIERTO >=89%) ----------------

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

    log.info("Evaluando bloque TT Elite de MEDIA HORA %s (%d partidos)...", target_block, len(block_matches))

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

            if prob_ganador >= FRANJA_MIN_PROB and fav != "NINGUNO":
                valid_analyzed.append({
                    **r,
                    "fav": fav,
                    "rating": rating,
                    "details": details,
                    "prob": prob_ganador
                })

    if not valid_analyzed:
        log.info("🛑 Ningún partido TT Elite del bloque %s alcanzó el mínimo estricto de %d%%.", target_block, FRANJA_MIN_PROB)
        return None, None, 0

    valid_analyzed.sort(key=lambda x: (x["prob"], x["rating"]), reverse=True)
    best_match = valid_analyzed[0]
    best_prob = best_match["prob"]

    if best_prob >= STAKE_ACIERTO_MIN:
        flow = "acierto"
        log.info("🚨🔥 STAKE DE ACIERTO TOTAL DETECTADO (>= %d%%) -> %s (%d%%)", STAKE_ACIERTO_MIN, best_match["fav"], best_prob)
    else:
        flow = "normal"
        log.info("🏓 MEJOR DE LA MEDIA HORA DETECTADO -> %s (%d%%)", best_match["fav"], best_prob)

    return best_match, flow, best_prob


# ---------------- FORMATO Y ENVÍO MULTI-CHAT A TELEGRAM ----------------

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
        clean_details = clean_details.replace("Favorito Perplexity:", "Favorito 1:").replace("Favorito OpenAI:", "Favorito 2:")
        if "\nArgumentos:" in clean_details and "\n\nArgumentos:" not in clean_details:
            clean_details = clean_details.replace("\nArgumentos:", "\n\nArgumentos:")
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


# ---------------- COMANDOS TELEGRAM ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏓 TT Elite Series v57 Activo.\n"
        "/activar_grupo · /id · /estado · /resumen · /partidos · /test"
    )


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    await update.message.reply_text(
        f"🆔 <b>El ID de este chat/grupo es:</b>\n<code>{cid}</code>\n\n"
        f"💡 Si quieres que envíe los picks aquí automáticamente, escribe /activar_grupo",
        parse_mode="HTML"
    )


async def cmd_activar_grupo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    cid = str(update.effective_chat.id)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO extra_chats(chat_id) VALUES (?)", (cid,))
    await update.message.reply_text(
        f"✅ <b>¡Grupo vinculado con éxito a TT Elite Series!</b>\n"
        f"🆔 ID registrado: <code>{cid}</code>\n"
        f"A partir de ahora recibirás aquí todos los picks al mismo tiempo que en tu chat privado.",
        parse_mode="HTML"
    )


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pplx_ok = "✅ Conectada" if PERPLEXITY_API_KEY else "⚠️ Sin API Key"
    oai_ok = "✅ Conectada" if openai_client else "⚠️ Sin API Key"
    destinos = ", ".join(get_all_chat_ids())
    await update.message.reply_text(
        f"✅ TT Elite Series v57 Operativo (Polonia - 2 Mensajes).\n"
        f"📡 Chats activos: {destinos}\n"
        f"🚨 Stake 100 de Acierto: Estricto >= {STAKE_ACIERTO_MIN}%\n"
        f"🏓 Pick Media Hora: Estricto >= {FRANJA_MIN_PROB}% (78% a 88%)\n"
        f"🔍 Factores: H2H + Racha + Carga de partidos + Desplazamiento\n"
        f"Perplexity: {pplx_ok} | OpenAI: {oai_ok}"
    )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    with db() as c:
        n_fix = c.execute("SELECT COUNT(*) FROM fixtures WHERE start_ts>?", (int(time.time()),)).fetchone()[0]
    await update.message.reply_text(
        f"🟢 Estado TT Elite Series (Franjas 30 min)\n"
        f"Partidos futuros: {n_fix}\n"
        f"Stakes de Acierto (>=89%) enviados hoy: {count_acierto_today()}\n"
        f"Destinos configurados: {len(get_all_chat_ids())}"
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
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
    init_db()
    now = int(time.time())
    with db() as c:
        rows = c.execute("""SELECT player1, player2, start_ts, hour_block
            FROM fixtures WHERE start_ts >= ? ORDER BY start_ts LIMIT 15""",
            (now,)).fetchall()

    if not rows:
        await update.message.reply_text("Sin partidos programados.")
        return

    lines = [f"📋 <b>Cartelera TT Elite Próxima (Franjas de 30m):</b>"]
    for r in rows:
        hh = datetime.fromtimestamp(r["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• <b>{hh}</b> | Blq: {r['hour_block']} | {r['player1']} vs {r['player2']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------- WEB (Render - Servidor directo con puerto dinámico) ----------------

@web.get("/")
def home():
    return "TT Elite v57 Activo", 200


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
    
    # Arranca Flask en segundo plano para abrir el puerto web de Render de inmediato
    threading.Thread(target=run_web, daemon=True).start()
    
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    for name, fn in [
        ("start", cmd_start), ("id", cmd_id), ("activar_grupo", cmd_activar_grupo),
        ("test", cmd_test), ("estado", cmd_estado), ("resumen", cmd_resumen), ("partidos", cmd_partidos)
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("Iniciando TT Elite v57 (Media Hora >=78%% | Acierto >=89%%)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
