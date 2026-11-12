"""
TT ELITE SERIES - BOT DE ANALISIS INFORMATIVO
- Revisa la cartelera de partidos.txt por bloques de 30 minutos.
- Perplexity busca resultados recientes y H2H con fuentes web (con timeout optimizado).
- Python calcula WR5, WR10 y H2H a partir de los datos recuperados.
- OpenAI analiza exclusivamente esas estadísticas.
- Selecciona el mejor partido del bloque y envía un único informe.
- No inventa resultados, porcentajes ni fuentes. Si faltan datos, lo informa.
- No incluye stakes ni instrucciones de apuestas.
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIGURACION ----------------

TZ = ZoneInfo("America/Bogota")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

DB_PATH = os.getenv("DB_PATH", "tt_elite_analisis.db")
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
MIN_MATCHES_PER_PLAYER = 3

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("TT_ELITE")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_blocks (
                block_id TEXT PRIMARY KEY,
                sent_at INTEGER NOT NULL,
                message_type TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS match_reports (
                event_id TEXT PRIMARY KEY,
                block_id TEXT NOT NULL,
                player1 TEXT NOT NULL,
                player2 TEXT NOT NULL,
                favorite TEXT,
                quality TEXT NOT NULL,
                report TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)


def block_sent(block_id):
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_blocks WHERE block_id=?",
            (block_id,),
        ).fetchone()
    return row is not None


def mark_block_sent(block_id, message_type):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_blocks(block_id, sent_at, message_type) VALUES(?,?,?)",
            (block_id, int(time.time()), message_type),
        )


# ---------------- UTILIDADES ----------------

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


# ---------------- CARTELERA ----------------

def load_fixtures():
    path = "partidos.txt"
    if not os.path.exists(path):
        log.warning("No existe partidos.txt")
        return []

    now = datetime.now(TZ)
    fixtures = []

    with open(path, "r", encoding="utf-8") as file:
        for line_no, raw in enumerate(file, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split("|", 1)
            if len(parts) != 2 or " vs " not in parts[1]:
                log.warning("Formato incorrecto en partidos.txt linea %s", line_no)
                continue

            time_text, matchup = parts[0].strip(), parts[1].strip()
            player1, player2 = [x.strip() for x in matchup.split(" vs ", 1)]

            if not player1 or not player2:
                continue

            try:
                hour, minute = map(int, time_text.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError("hora fuera de rango")
            except Exception:
                log.warning("Hora invalida en linea %s: %s", line_no, time_text)
                continue

            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if start < now - timedelta(hours=3):
                start = start + timedelta(days=1)

            block_minute = 30 if minute >= 30 else 0
            block_id = f"{start.date().isoformat()}_{hour:02d}:{block_minute:02d}"
            event_id = f"{block_id}_{line_no}_{normalize(player1)}_{normalize(player2)}"

            fixtures.append({
                "event_id": event_id,
                "block_id": block_id,
                "player1": player1,
                "player2": player2,
                "start_ts": int(start.timestamp()),
            })

    return fixtures


# ---------------- PERPLEXITY: BUSQUEDA WEB ----------------

def perplexity_search(player1, player2):
    if not PERPLEXITY_API_KEY:
        return None, [], "Falta PERPLEXITY_API_KEY."

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    system = "Eres un investigador de resultados deportivos de tenis de mesa. Devuelve EXCLUSIVAMENTE JSON valido sin texto adicional."

    user = f"""
Busca partidos recientes y H2H de:
- Jugador 1: {player1}
- Jugador 2: {player2}
Fecha: {today}
Torneo: TT Elite Series, Polonia.

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
            timeout=20,  # Timeout optimizado a 20s para evitar bloqueos largos al arrancar
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


# ---------------- ESTADISTICAS EN PYTHON ----------------

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


def stats_leader(stats, player1, player2):
    if not stats["p1"] or not stats["p2"]:
        return None

    wr1 = stats["p1_wr10"] if stats["p1_wr10"] is not None else 0.5
    wr2 = stats["p2_wr10"] if stats["p2_wr10"] is not None else 0.5
    recent1 = stats["p1_wr5"] if stats["p1_wr5"] is not None else wr1
    recent2 = stats["p2_wr5"] if stats["p2_wr5"] is not None else wr2

    score1 = 0.65 * wr1 + 0.35 * recent1
    score2 = 0.65 * wr2 + 0.35 * recent2

    h2h_diff = stats["h2h_p1"] - stats["h2h_p2"]
    score1 += max(-3, min(3, h2h_diff)) * 0.025

    if abs(score1 - score2) < 0.04:
        return None
    return player1 if score1 > score2 else player2


# ---------------- OPENAI: ANALISIS ESTADISTICO ----------------

def openai_analysis(player1, player2, stats, quality):
    if not openai_client:
        return None

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
        "h2h_victorias": {
            player1: stats["h2h_p1"],
            player2: stats["h2h_p2"],
        },
    }

    prompt = f"""
Analiza tenis de mesa usando EXCLUSIVAMENTE estos datos estructurados.
No uses conocimiento externo, no inventes resultados ni completes estadisticas.
Si la muestra es pequeña, incompleta o contradictoria, devuelve "NINGUNO".

DATOS:
{json.dumps(evidence, ensure_ascii=False)}

Devuelve JSON:
{{
  "favorito": "nombre exacto de uno de los jugadores o NINGUNO",
  "confianza": "BAJA, MEDIA o ALTA",
  "explicacion": "maximo dois frases basadas en cifras concretas"
}}
No des porcentajes de probabilidad. No presentes una estimacion como certeza.
"""

    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
        )
        return parse_json(response.choices[0].message.content)
    except Exception as exc:
        log.warning("OpenAI fallo: %s", exc)
        return None


# ---------------- INFORME DEL PARTIDO ----------------

def analyze_fixture(fixture):
    player1 = fixture["player1"]
    player2 = fixture["player2"]

    data, citations, error = perplexity_search(player1, player2)
    if not data:
        return {
            "favorite": None,
            "quality": "SIN DATOS",
            "report": error or "No se recuperaron datos verificables.",
        }

    stats = calculate_stats(data, player1, player2, citations)
    quality = data_quality(stats)

    if quality == "SIN DATOS":
        return {
            "favorite": None,
            "quality": quality,
            "report": (
                "Perplexity no devolvio registros que cumplieran los controles "
                "de fecha, resultado y URL citada. No se recomienda inferir un ganador."
            ),
        }

    ai = openai_analysis(player1, player2, stats, quality)
    ppx_favorite = player_match(data.get("favorito_perplexity"), player1, player2)
    ai_favorite = player_match((ai or {}).get("favorito"), player1, player2)
    statistical_favorite = stats_leader(stats, player1, player2)

    if ppx_favorite and ai_favorite and ppx_favorite == ai_favorite:
        favorite = ppx_favorite
        agreement = "COINCIDEN"
    else:
        favorite = None
        agreement = "SIN CONSENSO"

    def match_lines(items):
        if not items:
            return "No hay registros aceptados."
        lines = []
        for item in items[:5]:
            result = "Victoria" if item["gano"] else "Derrota"
            score = f" | {item['marcador']}" if item["marcador"] else ""
            host = source_host(item["url"])
            lines.append(f"• {item['fecha']} vs {item['rival']}: {result}{score} ({host})")
        return "\n".join(lines)

    lines = [
        f"📶 Calidad de datos: {quality}",
        f"🤖 Favorito según Perplexity: {ppx_favorite or 'Sin veredicto'}",
        f"🧠 Favorito según OpenAI: {ai_favorite or 'Sin veredicto'}",
        f"📊 Líder estadístico calculado: {statistical_favorite or 'Sin ventaja clara'}",
        f"🤝 Consenso entre IAs: {agreement}",
        "",
        f"📈 {player1} | WR5: {percent(stats['p1_wr5'])} | WR10: {percent(stats['p1_wr10'])}",
        match_lines(stats["p1"]),
        "",
        f"📈 {player2} | WR5: {percent(stats['p2_wr5'])} | WR10: {percent(stats['p2_wr10'])}",
        match_lines(stats["p2"]),
        "",
        f"🤝 H2H validado: {player1} {stats['h2h_p1']} - {stats['h2h_p2']} {player2}",
        f"🔎 URLs citadas por Perplexity: {len(citations)}",
    ]

    reason = str(data.get("razon") or "").strip()
    if reason:
        lines.extend(["", f"🤖 Nota de búsqueda: {reason}"])

    ai_reason = str((ai or {}).get("explicacion") or "").strip()
    if ai_reason:
        lines.extend(["", f"🧠 Nota estadística: {ai_reason}"])

    if favorite:
        lines.extend([
            "",
            f"📌 Favorito informativo por consenso: {favorite}",
            "Este análisis describe datos disponibles; no garantiza el resultado.",
        ])
    else:
        lines.extend([
            "",
            "📌 No hay consenso suficiente para señalar un favorito.",
        ])

    return {
        "favorite": favorite,
        "quality": quality,
        "report": "\n".join(lines),
    }


# ---------------- TELEGRAM ----------------

async def send_to_chats(app, message):
    chat_ids = [x.strip() for x in CHAT_ID.split(",") if x.strip()]
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=message,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.error("No se pudo enviar a %s: %s", chat_id, exc)


async def command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏓 Bot de análisis TT Elite activo.\n"
        "Comandos: /id, /test, /estado, /partidos"
    )


async def command_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID de este chat: {update.effective_chat.id}")


async def command_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ El bot está respondiendo correctamente.")


async def command_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sent_blocks").fetchone()[0]
    await update.message.reply_text(
        f"🟢 Bot activo\nBloques procesados: {count}\nZona horaria: America/Bogota"
    )


async def command_partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fixtures = load_fixtures()
    if not fixtures:
        await update.message.reply_text("No hay partidos validos en partidos.txt.")
        return

    lines = ["📋 Cartelera cargada:"]
    for item in fixtures[:20]:
        start = datetime.fromtimestamp(item["start_ts"], TZ).strftime("%I:%M %p")
        lines.append(f"• {start}: {item['player1']} vs {item['player2']}")
    await update.message.reply_text("\n".join(lines))


async def run_cycle(app):
    now = int(time.time())
    fixtures = load_fixtures()

    # Selecciona únicamente los partidos de la franja actual (con margen previo de hasta 35 minutos)
    upcoming = [
        item for item in fixtures
        if 0 <= item["start_ts"] - now <= 2100
    ]
    if not upcoming:
        return

    # Procesa estrictamente un bloque por ciclo para evitar saturar las APIs
    blocks = sorted(set(item["block_id"] for item in upcoming))
    block_id = next((b for b in blocks if not block_sent(b)), None)
    if not block_id:
        return

    matches = [item for item in upcoming if item["block_id"] == block_id]
    reports = []

    for fixture in matches:
        try:
            result = await asyncio.to_thread(analyze_fixture, fixture)
            reports.append((fixture, result))
        except Exception as exc:
            log.exception("Error analizando partido")
            reports.append((fixture, {
                "favorite": None,
                "quality": "ERROR",
                "report": f"No se pudo completar el análisis: {str(exc)[:180]}",
            }))

    if not reports:
        return

    # Prioriza el partido que tenga consenso de favorito y mejor calidad de datos
    reports.sort(
        key=lambda pair: (
            pair[1]["favorite"] is not None,
            pair[1]["quality"] == "ALTA",
            pair[1]["quality"] == "MEDIA",
        ),
        reverse=True,
    )

    fixture, result = reports[0]
    kickoff = datetime.fromtimestamp(fixture["start_ts"], TZ).strftime("%I:%M %p")
    message = (
        "🏓 TT ELITE SERIES | INFORME DE FRANJA\n\n"
        f"⚔️ {fixture['player1']} vs {fixture['player2']}\n"
        f"⏱️ Inicio: {kickoff} (Colombia)\n\n"
        f"{result['report']}"
    )

    await send_to_chats(app, message)
    mark_block_sent(block_id, "consenso" if result["favorite"] else "informe_sin_consenso")

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO match_reports
            (event_id, block_id, player1, player2, favorite, quality, report, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                fixture["event_id"],
                block_id,
                fixture["player1"],
                fixture["player2"],
                result["favorite"],
                result["quality"],
                result["report"],
                int(time.time()),
            ),
        )

    log.info("Informe enviado para bloque %s", block_id)


async def monitor_loop(app):
    while True:
        try:
            await run_cycle(app)
        except Exception:
            log.exception("Error en ciclo del monitor")
        await asyncio.sleep(POLL_SECONDS)


async def post_init(app):
    app.create_task(monitor_loop(app))


def main():
    missing = []
    if not TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not PERPLEXITY_API_KEY:
        missing.append("PERPLEXITY_API_KEY")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if missing:
        raise RuntimeError("Faltan variables de entorno: " + ", ".join(missing))

    init_db()

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", command_start))
    app.add_handler(CommandHandler("id", command_id))
    app.add_handler(CommandHandler("test", command_test))
    app.add_handler(CommandHandler("estado", command_estado))
    app.add_handler(CommandHandler("partidos", command_partidos))

    log.info("Iniciando bot de análisis TT Elite...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
