"""
SKATE10 v57.4 - TT ELITE SERIES (POLONIA)
- Revisa la cartelera de partidos.txt por bloques de 30 minutos.
- Búsqueda web específica para TT Elite Series con Perplexity (timeout optimizado de 20s).
- Auditoría rigurosa y estadística con OpenAI (gpt-4o-mini).
- Servidor web Flask integrado para despliegue en Render y soporte multi-chat.
- Sin apuestas, solo análisis informativo y selectivo por franja.
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
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from openai import OpenAI

# ---------------- CONFIGURACION ----------------

TZ = ZoneInfo("America/Bogota")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

DB_PATH = os.getenv("DB_PATH", "tt_elite_analisis.db")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))

LEAD_MIN = 35
MIN_LEAD_MIN = 3
MIN_MATCHES_PER_PLAYER = 3

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
for noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("TT_ELITE")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

web = Flask(__name__)
started_at = time.time()


# ---------------- BASE DE DATOS ----------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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


def block_already_sent(hour_block):
    with db() as c:
        row = c.execute("SELECT COUNT(*) FROM picks WHERE hour_block=?", (hour_block,)).fetchone()
        return row[0] > 0 if row else False


# ---------------- UTILIDADES ----------------

import re

def normalize(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def player_match(value, player1, player2):
    n = normalize(value)
    a = normalize(player1)
    b = normalize(player2)

    if not n:
        return None
    if n == a:
        return player1
    if n == b:
        return player2

    a_last = normalize(player1.split()[-1])
    b_last = normalize(player2.split()[-1])

    hit_a = n == a_last or a_last in n or n in a
    hit_b = n == b_last or b_last in n or n in b

    if hit_a and not hit_b:
        return player1
    if hit_b and not hit_a:
        return player2
    return None


def parse_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None


def valid_date(value):
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def clean_matches(items, citation_urls):
    allowed = set(citation_urls or [])
    cleaned = []
    seen = set()

    for item in items or []:
        if not isinstance(item, dict):
            continue

        date = str(item.get("fecha", "")).strip()
        rival = str(item.get("rival", "")).strip()
        won = item.get("gano")
        url = str(item.get("url", "")).strip()
        score = str(item.get("marcador", "")).strip()

        if not valid_date(date) or not rival or not isinstance(won, bool):
            continue
        if not url or url not in allowed:
            continue

        key = (date, normalize(rival), won, score)
        if key in seen:
            continue
        seen.add(key)

        cleaned.append({
            "fecha": date,
            "rival": rival,
            "gano": won,
            "marcador": score,
            "url": url,
        })

    cleaned.sort(key=lambda x: x["fecha"], reverse=True)
    return cleaned[:10]


def win_rate(matches, count=None):
    selected = matches[:count] if count else matches
    if not selected:
        return None
    return sum(1 for item in selected if item["gano"]) / len(selected)


def percent(value):
    return "Sin datos" if value is None else f"{round(value * 100)}%"


def source_host(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# ---------------- PERPLEXITY: BÚSQUEDA WEB ----------------

def perplexity_search(player1, player2):
    if not PERPLEXITY_API_KEY:
        return None, [], "Falta PERPLEXITY_API_KEY."

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    system = "Eres un investigador de resultados deportivos de tenis de mesa. Devuelve EXCLUSIVAMENTE JSON valido sin texto adicional."

    user = f"""
Busca partidos recientes y H2H de tenis de mesa para:
- Jugador 1: {player1}
- Jugador 2: {player2}
Fecha de referencia: {today}
Torneo: TT Elite Series (Polonia).

Devuelve SOLO este JSON:
{{
  "partidos_j1": [
    {{"fecha":"YYYY-MM-DD","rival":"nombre","gano":true,"marcador":"3-1","url":"URL exacta"}}
  ],
  "partidos_j2": [
    {{"fecha":"YYYY-MM-DD","rival":"nombre","gano":false,"marcador":"1-3","url":"URL exacta"}}
  ],
  "h2h": [
    {{"fecha":"YYYY-MM-DD","ganador":"nombre exacto","marcador":"3-1","url":"URL exacta"}}
  ],
  "favorito_perplexity": "nombre exacto o null",
  "razon": "breve explicacion"
}}
"""

    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,  # Timeout optimizado a 20s para agilizar el arranque de la franja
        )
        if response.status_code != 200:
            log.warning("Perplexity HTTP %s", response.status_code)
            return None, [], "Perplexity no respondio a tiempo."

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        result = parse_json(content)
        citations = data.get("citations") or data.get("search_results") or []

        urls = []
        for citation in citations:
            if isinstance(citation, str) and citation.startswith("http"):
                urls.append(citation)
            elif isinstance(citation, dict):
                url = citation.get("url")
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)

        if result is not None:
            return result, list(dict.fromkeys(urls)), None

    except Exception as exc:
        log.warning("Timeout o error en Perplexity: %s", exc)
        return None, [], "Tiempo de espera agotado para Perplexity."

    return None, [], "Perplexity no devolvio una respuesta util."


# ---------------- ESTADISTICAS Y ANALISIS OPENAI ----------------

def calculate_stats(data, player1, player2, citations):
    matches1 = clean_matches(data.get("partidos_j1"), citations)
    matches2 = clean_matches(data.get("partidos_j2"), citations)

    h2h = []
    h2h_wins1 = 0
    h2h_wins2 = 0

    for item in data.get("h2h") or []:
        if not isinstance(item, dict):
            continue
        date = str(item.get("fecha", "")).strip()
        url = str(item.get("url", "")).strip()
        winner = player_match(item.get("ganador"), player1, player2)
        if not valid_date(date) or not winner or url not in set(citations):
            continue

        if winner == player1:
            h2h_wins1 += 1
        else:
            h2h_wins2 += 1

        h2h.append({
            "fecha": date,
            "ganador": winner,
            "marcador": str(item.get("marcador", "")).strip(),
            "url": url,
        })

    h2h.sort(key=lambda x: x["fecha"], reverse=True)

    return {
        "p1": matches1,
        "p2": matches2,
        "h2h": h2h,
        "p1_wr5": win_rate(matches1, 5),
        "p1_wr10": win_rate(matches1, 10),
        "p2_wr5": win_rate(matches2, 5),
        "p2_wr10": win_rate(matches2, 10),
        "h2h_p1": h2h_wins1,
        "h2h_p2": h2h_wins2,
    }


def data_quality(stats):
    n1, n2 = len(stats["p1"]), len(stats["p2"])
    if n1 >= 5 and n2 >= 5:
        return "ALTA"
    if n1 >= MIN_MATCHES_PER_PLAYER and n2 >= MIN_MATCHES_PER_PLAYER:
        return "MEDIA"
    if n1 + n2 > 0 or stats["h2h"]:
        return "BAJA"
    return "SIN DATOS"


def analizar_partido(player1: str, player2: str):
    data, citations, error = perplexity_search(player1, player2)
    if not data:
        return "NINGUNO", 5.0, 50, error or "No se recuperaron datos verificables."

    stats = calculate_stats(data, player1, player2, citations)
    quality = data_quality(stats)

    if quality == "SIN DATOS":
        return "NINGUNO", 5.0, 50, "Datos insuficientes para emitir juicio."

    if not openai_client:
        return player1, 7.5, 75, "OpenAI no configurado."

    evidence = {
        "calidad": quality,
        "jugador_1": player1,
        "jugador_2": player2,
        "jugador_1_ultimos": stats["p1"],
        "jugador_2_ultimos": stats["p2"],
        "jugador_1_wr5": stats["p1_wr5"],
        "jugador_1_wr10": stats["p1_wr10"],
        "jugador_2_wr5": stats["p2_wr5"],
        "jugador_2_wr10": stats["p2_wr10"],
        "h2h": stats["h2h"],
        "h2h_victorias": {player1: stats["h2h_p1"], player2: stats["h2h_p2"]},
    }

    prompt = f"""
Analiza tenis de mesa (TT Elite Series) usando EXCLUSIVAMENTE estos datos estructurados.
No uses conocimiento externo, no inventes resultados ni completes estadisticas.
Si la muestra es pequeña, incompleta o contradictoria, devuelve favorito "NINGUNO".

DATOS:
{json.dumps(evidence, ensure_ascii=False)}

Devuelve JSON:
{{
  "favorito": "nombre exacto de uno de los jugadores o NINGUNO",
  "confianza": "BAJA, MEDIA o ALTA",
  "explicacion": "maximo dos frases basadas en cifras concretas"
}}
"""

    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
        )
        ai_res = parse_json(response.choices[0].message.content) or {}
        fav = player_match(ai_res.get("favorito"), player1, player2) or "NINGUNO"
        expl = str(ai_res.get("explicacion", "")).strip()

        prob = 80 if ai_res.get("confianza") == "ALTA" else 75
        rating = 8.5 if prob >= 80 else 7.5

        details = (
            f"📶 Calidad de datos: {quality}\n"
            f"🤝 H2H: {player1} {stats['h2h_p1']} - {stats['h2h_p2']} {player2}\n"
            f"💡 Análisis: {expl or 'Datos evaluados por consenso IA.'}"
        )
        return fav, rating, prob, details
    except Exception as exc:
        log.warning("OpenAI fallo: %s", exc)
        return "NINGUNO", 5.0, 50, "Error en procesamiento estadístico."


# ---------------- CARGA DE CARTELERA ----------------

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
            parts = [p.strip() for p in linea.split("|")]
            if len(parts) >= 2:
                time_str = parts[0]
                match_str = parts[1]
                if " vs " in match_str:
                    player1, player2 = match_str.split(" vs ", 1)
                else:
                    player1, player2 = match_str, "Rival"
            else:
                continue

            try:
                h, m = map(int, time_str.split(":"))
                target_date = hoy_dt
                match_dt = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
                if match_dt < hoy_dt - timedelta(hours=3):
                    match_dt += timedelta(days=1)

                ts = int(match_dt.timestamp())
                half_m = 30 if m >= 30 else 0
                hour_block = f"{match_dt.date().isoformat()}_{h:02d}:{half_m:02d}"
            except Exception:
                ts = int(time.time()) + (idx + 1) * 1800
                hour_block = f"{hoy_dt.date().isoformat()}_{time_str.strip()}"

            partidos.append({
                "id": f"match_{idx}_{time_str.replace(':', '')}",
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


# ---------------- SELECCIÓN POR FRANJA ----------------

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
        """, (target_block, now + 2 * 60)).fetchall()]

    if not block_matches:
        return None, None, 0

    valid_analyzed = []
    with db() as c:
        for r in block_matches:
            eid = r["event_id"]
            an = c.execute("SELECT fav, rating, prob, details FROM analysis WHERE event_id=?", (eid,)).fetchone()
            if not an:
                fav, rating, prob_ganador, details = analizar_partido(r["player1"], r["player2"])
                c.execute("""INSERT OR REPLACE INTO analysis(event_id, fav, rating, prob, quality, details, analyzed_at) 
                             VALUES (?,?,?,?,?,?,?)""",
                          (eid, fav, rating, prob_ganador, "alta" if prob_ganador >= 75 else "moderada", details, now))
                time.sleep(0.3)
            else:
                fav = an["fav"]
                rating = an["rating"]
                prob_ganador = an["prob"]
                details = an["details"]

            if fav != "NINGUNO":
                valid_analyzed.append({
                    **r,
                    "fav": fav,
                    "rating": rating,
                    "details": details,
                    "prob": prob_ganador
                })

    if not valid_analyzed:
        return None, None, 0

    valid_analyzed.sort(key=lambda x: (x["prob"], x["rating"]), reverse=True)
    best_match = valid_analyzed[0]
    return best_match, "consenso", best_match["prob"]


# ---------------- ENVÍO TELEGRAM ----------------

def format_message(p, prob):
    kickoff = datetime.fromtimestamp(p["start_ts"], TZ).strftime("%I:%M %p").lstrip("0")
    lines = [
        "🏓 <b>TT ELITE SERIES | INFORME DE FRANJA</b>",
        f"🏆 <b>Ganador Sugerido: {p['fav']}</b>",
        f"⚔️ {p['player1']} vs {p['player2']}",
        f"⏱️ Inicio: {kickoff} (Colombia)\n",
        p["details"]
    ]
    return "\n".join(lines)


async def send_pick(app, p, prob):
    text = format_message(p, prob)
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


async def run_cycle(app):
    await asyncio.to_thread(fetch_fixtures_only)
    best_p, flow, prob = await asyncio.to_thread(select_best_pick_for_current_blocks)
    if best_p and get_all_chat_ids():
        await send_pick(app, best_p, prob)

        with db() as c:
            c.execute("""INSERT OR IGNORE INTO picks
                (event_id, flow, rating, fav, player1, player2, start_ts, sent_at, details, hour_block)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (best_p["event_id"], flow, best_p["rating"], best_p["fav"], best_p["player1"],
                 best_p["player2"], best_p["start_ts"], int(time.time()), best_p["details"], best_p["hour_block"]))

        log.info("Informe enviado para bloque %s -> Ganador: %s", best_p["hour_block"], best_p["fav"])


async def monitor_loop(app: Application):
    while True:
        try:
            await run_cycle(app)
        except Exception as exc:
            log.error("Fallo en bucle monitor: %s", str(exc)[:300])
        await asyncio.sleep(POLL_SECONDS)


# ---------------- COMANDOS TELEGRAM ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Bot de análisis TT Elite Series activo.")


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
    await update.message.reply_text("✅ El bot de TT Elite Series está respondiendo correctamente.")


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM fixtures WHERE start_ts>?", (int(time.time()),)).fetchone()[0]
    await update.message.reply_text(f"🟢 Bot TT Elite Activo\nPartidos futuros cargados: {count}\nZona horaria: America/Bogota")


async def cmd_partidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    init_db()
    now = int(time.time())
    with db() as c:
        rows = c.execute("SELECT player1, player2, start_ts FROM fixtures WHERE start_ts >= ? ORDER BY start_ts LIMIT 15", (now,)).fetchall()
    if not rows:
        await update.message.reply_text("No hay partidos validos en partidos.txt.")
        return
    lines = ["📋 Cartelera TT Elite Series cargada:"]
    for r in rows:
        hh = datetime.fromtimestamp(r["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• {hh}: {r['player1']} vs {r['player2']}")
    await update.message.reply_text("\n".join(lines))


# ---------------- WEB (Render) ----------------

@web.get("/")
def home():
    return "TT Elite Series Bot Activo", 200


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
        raise RuntimeError("Faltan variables esenciales de Telegram.")
    init_db()

    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    for name, fn in [
        ("start", cmd_start), ("id", cmd_id), ("activar_grupo", cmd_activar_grupo),
        ("test", cmd_test), ("estado", cmd_estado), ("partidos", cmd_partidos)
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer()))

    log.info("Iniciando bot TT Elite Series con servidor web integrado...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
