# ==========================================
# ALLBETS — Value Betting Bot (1xBet + Lesiones)
# ==========================================
import math
import os
import time as _time
import requests
import libsql
from datetime import datetime, timezone, timedelta
from html import escape
from contextlib import contextmanager, asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

# ==========================================
# VARIABLES DE ENTORNO
# ==========================================
TURSO_URL    = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN  = os.environ.get("TURSO_AUTH_TOKEN")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

# ==========================================
# CONFIG
# ==========================================
SIGMA                = 13.5   # desviación estándar del margen (más honesta que 11.5)
EDGE_MIN             = 3.0    # % mínimo para señal
CACHE_STATS_TTL      = 3600   # 1 h
CACHE_ODDS_TTL       = 300    # 5 min
CACHE_INJURIES_TTL   = 1800   # 30 min
CACHE_DEPTH_TTL      = 86400  # 24 h
PENDING_TIMEOUT_DAYS = 3      # PENDING > N días → VOID

# ==========================================
# MAPEO DE EQUIPOS (exact match, evita colisiones tipo "nets" in "hornets")
# ==========================================
TEAM_KEYS = {
    "atlanta hawks": "ATL",
    "boston celtics": "BOS",
    "brooklyn nets": "BKN",
    "charlotte hornets": "CHA",
    "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL",
    "denver nuggets": "DEN",
    "detroit pistons": "DET",
    "golden state warriors": "GSW",
    "houston rockets": "HOU",
    "indiana pacers": "IND",
    "la clippers": "LAC",
    "los angeles clippers": "LAC",
    "la lakers": "LAL",
    "los angeles lakers": "LAL",
    "memphis grizzlies": "MEM",
    "miami heat": "MIA",
    "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP",
    "new york knicks": "NY",
    "oklahoma city thunder": "OKC",
    "orlando magic": "ORL",
    "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX",
    "portland trail blazers": "POR",
    "sacramento kings": "SAC",
    "san antonio spurs": "SAS",
    "toronto raptors": "TOR",
    "utah jazz": "UTA",
    "washington wizards": "WAS",
}

def get_team_key(name):
    return TEAM_KEYS.get(str(name).strip().lower(), str(name).strip().lower())

def norm(name):
    return str(name).strip().lower()

# ==========================================
# CACHÉ GENÉRICO
# ==========================================
_CACHE = {}

def _cached(key, ttl_sec, fn):
    now = _time.time()
    entry = _CACHE.get(key)
    if entry and now - entry["t"] < ttl_sec:
        return entry["v"]
    v = fn()
    _CACHE[key] = {"t": now, "v": v}
    return v

# Caché específico de depth charts (por equipo)
_DEPTH_CACHE = {}

def get_depth_cached(team_id):
    now = _time.time()
    entry = _DEPTH_CACHE.get(str(team_id))
    if entry and now - entry[0] < CACHE_DEPTH_TTL:
        return entry[1]
    starters = _fetch_depth(team_id)
    _DEPTH_CACHE[str(team_id)] = (now, starters)
    return starters

# ==========================================
# SCHEDULER Y LIFESPAN
# ==========================================
scheduler = BackgroundScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        procesar_mercado, "interval", minutes=60,
        id="allbets_job", coalesce=True, max_instances=1,
    )
    scheduler.start()
    print("🚀 Scheduler iniciado: buscando valor en 1xBet cada 60 minutos.")
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

# ==========================================
# CONEXIÓN A TURSO
# ==========================================
@contextmanager
def db():
    con = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    for attempt in range(3):
        try:
            with db() as con:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS predicciones_allbets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT,
                        fecha TEXT,
                        partido TEXT,
                        pick TEXT,
                        linea REAL,
                        cuota REAL,
                        edge REAL,
                        status TEXT DEFAULT 'PENDING',
                        puntos_home INTEGER,
                        puntos_away INTEGER,
                        UNIQUE(event_id, pick)
                    );
                """)
                # Migración one-time desde zcode (si existía)
                try:
                    con.execute("""
                        INSERT OR IGNORE INTO predicciones_allbets
                        (event_id, fecha, partido, pick, linea, cuota, edge, status, puntos_home, puntos_away)
                        SELECT event_id, fecha, partido, pick, linea, cuota, edge, status, puntos_home, puntos_away
                        FROM predicciones_zcode
                    """)
                    print("📦 Migración zcode → allbets ejecutada (si había datos).")
                except Exception:
                    pass  # no había tabla zcode
            return
        except Exception as e:
            print(f"⚠️ init_db intento {attempt + 1}/3: {e}")
            _time.sleep(2 ** attempt)
    raise RuntimeError("No se pudo inicializar Turso tras 3 intentos.")

init_db()

# ==========================================
# MATEMÁTICA
# ==========================================
def normal_cdf(x, mu=0, sigma=1):
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

def implied_fair_pcts(odds_a, odds_b):
    """Probabilidades implícitas SIN vig (normalizadas para sumar 100%)."""
    if odds_a <= 1.0 or odds_b <= 1.0:
        return 0.0, 0.0
    ia, ib = 1.0 / odds_a, 1.0 / odds_b
    t = ia + ib
    return 100.0 * ia / t, 100.0 * ib / t

# ==========================================
# DATOS ESPN
# ==========================================
def get_nba_stats():
    stats_dict = {}
    try:
        url = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        for conf in data.get("children", []):
            for entry in conf.get("standings", {}).get("entries", []):
                team_name = entry["team"]["displayName"]
                team_id = entry["team"]["id"]
                pts_for, pts_against = 110.0, 110.0
                for s in entry.get("stats", []):
                    if s.get("name") == "avgPointsFor":
                        pts_for = float(s.get("value", 110.0))
                    elif s.get("name") == "avgPointsAgainst":
                        pts_against = float(s.get("value", 110.0))
                stats_dict[team_name] = {"pf": pts_for, "pa": pts_against, "id": team_id}
    except Exception as e:
        print(f"⚠️ Error stats: {e}")
    return stats_dict

def _fetch_injuries_global():
    """Lista global de lesiones, indexada por team_id."""
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        by_team = {}
        for team in data.get("injuries", []):
            tid = str(team.get("team", {}).get("id", ""))
            by_team[tid] = team.get("injuries", [])
        return by_team
    except Exception as e:
        print(f"⚠️ Error injuries: {e}")
        return {}

def _fetch_depth(team_id):
    """Set de titulares (primer jugador de cada posición)."""
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/depthchart"
        r = requests.get(url, timeout=10)
        data = r.json()
        titulares = set()
        for group in data.get("depthChart", {}).get("positions", []):
            if group.get("athletes"):
                titulares.add(group["athletes"][0]["displayName"])
        return titulares
    except Exception:
        return set()

def get_injury_penalty(team_id, injuries_global):
    """Puntos a restar por lesiones. Recibe el diccionario ya fetcheado."""
    team_injuries = injuries_global.get(str(team_id), [])
    if not team_injuries:
        return 0.0, ""

    titulares = get_depth_cached(team_id)
    penalty = 0.0
    txt = ""

    for inj in team_injuries:
        status = inj.get("status", "")
        athlete = inj.get("athlete", {}).get("displayName", "")
        short = athlete.split()[-1] if athlete else ""
        is_starter = athlete in titulares

        if status in ("Out", "Doubtful"):
            if is_starter:
                penalty -= 3.5
                txt += f"❌ {short} (Titular) "
            else:
                penalty -= 1.0
                txt += f"⚠️ {short} (Banca) "
        elif status == "Day-To-Day":
            if is_starter:
                penalty -= 1.5
                txt += f"❓ {short} (Titular) "
            else:
                penalty -= 0.5
                txt += f"❓ {short} (Banca) "

    return penalty, txt.strip()

# ==========================================
# CUOTAS EN 1XBET (THE ODDS API)
# ==========================================
def get_all_nba_odds():
    if not ODDS_API_KEY:
        return {}

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": ODDS_API_KEY, "regions": "eu,us",
        "markets": "spreads", "oddsFormat": "decimal",
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return {}
        games = r.json()
        odds_dict = {}

        for game in games:
            h_api = game.get("home_team", "")
            a_api = game.get("away_team", "")
            key = f"{get_team_key(a_api)}_vs_{get_team_key(h_api)}"

            bookmakers = game.get("bookmakers", [])
            book_1xbet = next((b for b in bookmakers if b.get("key") == "onexbet"), None)
            if not book_1xbet:
                continue

            # Puede haber múltiples líneas alternas: guardar la más cercana a 0
            candidatos = []
            for market in book_1xbet.get("markets", []):
                if market.get("key") != "spreads":
                    continue
                spread = {}
                for out in market.get("outcomes", []):
                    if get_team_key(out.get("name", "")) == get_team_key(h_api):
                        spread["home_line"] = float(out.get("point", 0))
                        spread["home_odds"] = float(out.get("price", 0))
                    elif get_team_key(out.get("name", "")) == get_team_key(a_api):
                        spread["away_line"] = float(out.get("point", 0))
                        spread["away_odds"] = float(out.get("price", 0))
                if len(spread) == 4:
                    candidatos.append(spread)

            if candidatos:
                mejor = min(candidatos, key=lambda s: abs(s["home_line"]))
                odds_dict[key] = {"spread": mejor}

        return odds_dict
    except Exception as e:
        print(f"⚠️ Error odds: {e}")
        return {}

# ==========================================
# PERSISTENCIA
# ==========================================
def guardar_prediccion(event_id, partido, pick, linea, cuota, edge):
    try:
        with db() as con:
            existing = con.execute(
                "SELECT id FROM predicciones_allbets WHERE event_id=? AND pick=?",
                (event_id, pick),
            ).fetchone()
            if existing:
                return  # ya existía
            con.execute("""
                INSERT INTO predicciones_allbets
                (event_id, fecha, partido, pick, linea, cuota, edge, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (event_id, datetime.now(timezone.utc).isoformat(),
                  partido, pick, linea, cuota, edge))
            print(f"   ✅ Guardado: {pick} {linea:+.1f} @ {cuota:.2f} (Edge: {edge:.1f}%)")
    except Exception as e:
        print(f"   ❌ Error guardando: {e}")

def auditar_predicciones():
    try:
        with db() as con:
            pendientes = con.execute(
                "SELECT id, event_id, pick, linea, fecha "
                "FROM predicciones_allbets WHERE status='PENDING'"
            ).fetchall()
        if not pendientes:
            return

        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15,
        )
        games = {ev.get("id"): ev for ev in r.json().get("events", [])}
        ahora = datetime.now(timezone.utc)

        for pred_id, event_id, pick, linea, fecha_str in pendientes:
            # Timeout: PENDING > N días → VOID
            try:
                fecha = datetime.fromisoformat(fecha_str)
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
                if ahora - fecha > timedelta(days=PENDING_TIMEOUT_DAYS):
                    with db() as con:
                        con.execute(
                            "UPDATE predicciones_allbets SET status='VOID' WHERE id=?",
                            (pred_id,),
                        )
                    continue
            except Exception:
                pass

            g = games.get(event_id)
            if not g:
                continue
            if g.get("status", {}).get("type", {}).get("state") != "post":
                continue

            comp = g.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            pts_home = int(home.get("score", 0))
            pts_away = int(away.get("score", 0))
            is_home_pick = get_team_key(pick) == get_team_key(home["team"]["displayName"])
            margen_final = (
                (pts_home - pts_away) + linea if is_home_pick
                else (pts_away - pts_home) + linea
            )
            status = "WON" if margen_final > 0 else "LOST" if margen_final < 0 else "PUSH"

            with db() as con:
                con.execute(
                    "UPDATE predicciones_allbets SET status=?, puntos_home=?, puntos_away=? WHERE id=?",
                    (status, pts_home, pts_away, pred_id),
                )
    except Exception as e:
        print(f"⚠️ Error auditoría: {e}")

# ==========================================
# LÓGICA DEL BOT
# ==========================================
def procesar_mercado():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Analizando jornada NBA (1xBet)...")
    try:
        _procesar_mercado_inner()
    finally:
        auditar_predicciones()

def _procesar_mercado_inner():
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15,
        )
        games_raw = r.json().get("events", [])
    except Exception:
        return

    partidos_hoy = [
        g for g in games_raw
        if g.get("status", {}).get("type", {}).get("state") == "pre"
    ]
    if not partidos_hoy:
        print("💤 No hay partidos próximos. 0 créditos gastados.")
        return

    odds = _cached("odds", CACHE_ODDS_TTL, get_all_nba_odds)
    if not odds:
        print("⚠️ 1xBet aún no abre mercado para estos partidos.")
        return

    stats = _cached("stats", CACHE_STATS_TTL, get_nba_stats)
    injuries_global = _cached("injuries", CACHE_INJURIES_TTL, _fetch_injuries_global)
    stats_norm = {norm(k): v for k, v in stats.items()}

    for ev in partidos_hoy:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2:
            continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])

        h_name = home.get("team", {}).get("displayName", "")
        a_name = away.get("team", {}).get("displayName", "")
        event_id = ev.get("id")

        if norm(h_name) not in stats_norm or norm(a_name) not in stats_norm:
            print(f"   ⚠️ {a_name} vs {h_name}: falta stats, skip")
            continue

        h = stats_norm[norm(h_name)]
        a = stats_norm[norm(a_name)]

        pen_home, txt_home = get_injury_penalty(h["id"], injuries_global)
        pen_away, txt_away = get_injury_penalty(a["id"], injuries_global)

        proy_home = (h["pf"] + a["pa"]) / 2 + 1.25 + pen_home
        proy_away = (a["pf"] + h["pa"]) / 2 - 1.25 + pen_away
        margen = proy_home - proy_away

        key = f"{get_team_key(a_name)}_vs_{get_team_key(h_name)}"
        cuotas = odds.get(key)
        if not cuotas or not cuotas.get("spread"):
            continue

        sp = cuotas["spread"]
        home_line, home_odds = sp["home_line"], sp["home_odds"]
        away_line, away_odds = sp["away_line"], sp["away_odds"]

        fair_home_pct, fair_away_pct = implied_fair_pcts(home_odds, away_odds)
        prob_home = normal_cdf((margen + home_line) / SIGMA) * 100
        prob_away = normal_cdf((-margen + away_line) / SIGMA) * 100

        edge_home = prob_home - fair_home_pct
        edge_away = prob_away - fair_away_pct

        print(f"🏀 {a_name} vs {h_name} | Lesiones: {txt_home or 'Ninguna'} / {txt_away or 'Ninguna'}")

        if edge_home > EDGE_MIN and edge_away > EDGE_MIN:
            print(f"   ⚠️ Ambos lados con edge ({edge_home:.1f}% / {edge_away:.1f}%) → probable ruido, skip")
        elif edge_home > EDGE_MIN:
            guardar_prediccion(event_id, f"{a_name} vs {h_name}",
                               h_name, home_line, home_odds, edge_home)
        elif edge_away > EDGE_MIN:
            guardar_prediccion(event_id, f"{a_name} vs {h_name}",
                               a_name, away_line, away_odds, edge_away)

# ==========================================
# RUTAS WEB
# ==========================================
@app.get("/")
def read_root():
    return {"mensaje": "Allbets — 1xBet + Lesiones. Ve a /predictor o /historial"}

@app.get("/predictor", response_class=HTMLResponse)
def predictor_html():
    stats = _cached("stats", CACHE_STATS_TTL, get_nba_stats)
    injuries_global = _cached("injuries", CACHE_INJURIES_TTL, _fetch_injuries_global)
    stats_norm = {norm(k): v for k, v in stats.items()}

    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15,
        )
        games_raw = r.json().get("events", [])
    except Exception:
        return HTMLResponse("<h1>Error cargando partidos</h1>")

    all_odds = _cached("odds", CACHE_ODDS_TTL, get_all_nba_odds)
    html_cards = ""

    for ev in games_raw:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2:
            continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])

        h_name = home.get("team", {}).get("displayName", "")
        a_name = away.get("team", {}).get("displayName", "")

        if norm(h_name) not in stats_norm or norm(a_name) not in stats_norm:
            continue

        h = stats_norm[norm(h_name)]
        a = stats_norm[norm(a_name)]

        pen_home, txt_home = get_injury_penalty(h["id"], injuries_global)
        pen_away, txt_away = get_injury_penalty(a["id"], injuries_global)

        proy_home = (h["pf"] + a["pa"]) / 2 + 1.25 + pen_home
        proy_away = (a["pf"] + h["pa"]) / 2 - 1.25 + pen_away
        margen = proy_home - proy_away

        prob_home = normal_cdf(margen / SIGMA) * 100
        prob_away = 100 - prob_home
        favorito = h_name if prob_home > prob_away else a_name
        prob_fav = prob_home if prob_home > prob_away else prob_away

        key = f"{get_team_key(a_name)}_vs_{get_team_key(h_name)}"
        cuotas = all_odds.get(key)
        valor_html = ""

        if cuotas and cuotas.get("spread"):
            sp = cuotas["spread"]
            home_line, home_odds = sp["home_line"], sp["home_odds"]
            away_line, away_odds = sp["away_line"], sp["away_odds"]

            fair_home_pct, fair_away_pct = implied_fair_pcts(home_odds, away_odds)
            prob_home_spread = normal_cdf((margen + home_line) / SIGMA) * 100
            prob_away_spread = normal_cdf((-margen + away_line) / SIGMA) * 100
            edge_home = prob_home_spread - fair_home_pct
            edge_away = prob_away_spread - fair_away_pct

            if edge_home > EDGE_MIN:
                valor_html = (
                    f'<div class="value-box" style="background: rgba(16,185,129,0.1); border-color: #10b981;">'
                    f'<div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET</div>'
                    f'<div class="value-bet">{escape(h_name)} {home_line:+.1f} @ {home_odds:.2f}</div>'
                    f'<div class="value-edge" style="color: #10b981;">Edge: +{edge_home:.1f}%</div></div>'
                )
            elif edge_away > EDGE_MIN:
                valor_html = (
                    f'<div class="value-box" style="background: rgba(16,185,129,0.1); border-color: #10b981;">'
                    f'<div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET</div>'
                    f'<div class="value-bet">{escape(a_name)} {away_line:+.1f} @ {away_odds:.2f}</div>'
                    f'<div class="value-edge" style="color: #10b981;">Edge: +{edge_away:.1f}%</div></div>'
                )
            else:
                valor_html = (
                    f'<div class="value-box" style="background: rgba(148,163,184,0.1); border-color: #94a3b8;">'
                    f'<div class="value-title" style="color: #94a3b8;">ANALIZADO (1xBet)</div>'
                    f'<div class="value-bet">{escape(h_name)} {home_line:+.1f} ({home_odds:.2f}) | '
                    f'{escape(a_name)} {away_line:+.1f} ({away_odds:.2f})</div>'
                    f'<div class="value-edge" style="color: #94a3b8;">Sin ventaja clara</div></div>'
                )
        else:
            valor_html = (
                f'<div class="value-box" style="background: rgba(148,163,184,0.1); border-color: #94a3b8;">'
                f'<div class="value-title" style="color: #94a3b8;">SIN CUOTAS</div>'
                f'<div class="value-bet">1xBet no ha abierto el mercado</div></div>'
            )

        injury_html = ""
        if txt_home or txt_away:
            injury_html = (
                f'<div style="font-size:11px; color:#ef4444; text-align:center; margin-top:10px;">'
                f'<b>Lesiones:</b> {escape(txt_home)} {escape(txt_away)}</div>'
            )

        html_cards += f"""
        <div class="card">
            <div class="card-header"><div class="league">🏀 NBA</div><div class="time">Hoy</div></div>
            <div class="matchup">
                <div class="team"><div class="team-name">{escape(a_name)}</div><div class="team-prob">{prob_away:.0f}%</div></div>
                <div class="vs"><div class="gauge"><div class="gauge-fill" style="width: {prob_fav}%;"></div></div><div class="fav-text">{escape(favorito)}<br>TO WIN</div></div>
                <div class="team"><div class="team-name">{escape(h_name)}</div><div class="team-prob">{prob_home:.0f}%</div></div>
            </div>
            <div class="projection"><div class="proj-box"><div class="proj-label">PROYECCIÓN ALLBETS</div><div class="proj-score">{proy_away:.0f} - {proy_home:.0f}</div></div></div>
            {valor_html}
            {injury_html}
        </div>
        """

    html_out = f"""<!DOCTYPE html><html><head><title>Allbets Predictor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0b1120; color: white; padding: 16px; max-width: 500px; margin: auto; }}
.card {{ background: #1e293b; border-radius: 16px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
.card-header {{ display: flex; justify-content: space-between; color: #94a3b8; font-size: 12px; font-weight: bold; margin-bottom: 20px; }}
.matchup {{ display: flex; justify-content: space-between; align-items: center; text-align: center; }}
.team {{ flex: 1; }}
.team-name {{ font-size: 14px; font-weight: bold; margin-bottom: 4px; line-height: 1.2; }}
.team-prob {{ font-size: 18px; font-weight: bold; color: #eab308; }}
.vs {{ flex: 1; padding: 0 10px; }}
.gauge {{ width: 100%; height: 8px; background: #334155; border-radius: 4px; overflow: hidden; margin-bottom: 8px; }}
.gauge-fill {{ height: 100%; background: #eab308; }}
.fav-text {{ font-size: 10px; color: #94a3b8; text-transform: uppercase; line-height: 1.2; }}
.projection {{ display: flex; gap: 10px; margin-top: 20px; border-top: 1px solid #334155; padding: 15px 0 0 0; }}
.proj-box {{ flex: 1; text-align: center; }}
.proj-label {{ font-size: 10px; color: #94a3b8; font-weight: bold; margin-bottom: 4px; }}
.proj-score {{ font-size: 20px; font-weight: bold; color: white; }}
.value-box {{ margin-top: 15px; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid transparent; }}
.value-title {{ font-size: 12px; font-weight: bold; margin-bottom: 4px; }}
.value-bet {{ font-size: 15px; font-weight: bold; color: white; }}
.value-edge {{ font-size: 12px; font-weight: bold; margin-top: 4px; }}
.nav {{ text-align: center; margin-bottom: 20px; }}
.nav a {{ color: #eab308; text-decoration: none; font-weight: bold; margin: 0 10px; }}
</style></head><body>
<div class="nav"><a href="/predictor">Predicciones</a> | <a href="/historial">Historial</a></div>
<h2 style="font-size: 18px; margin-bottom: 20px;">ALLBETS — Predicciones</h2>
{html_cards if html_cards else "<p style='text-align:center;'>No hay partidos programados para hoy.</p>"}
</body></html>"""
    return html_out

@app.get("/historial", response_class=HTMLResponse)
def historial_html():
    with db() as con:
        rows = con.execute(
            "SELECT fecha, partido, pick, linea, cuota, edge, status, puntos_home, puntos_away "
            "FROM predicciones_allbets ORDER BY id DESC LIMIT 50"
        ).fetchall()

    won = sum(1 for r in rows if r[6] == "WON")
    lost = sum(1 for r in rows if r[6] == "LOST")
    void = sum(1 for r in rows if r[6] == "VOID")

    html_cards = ""
    for r in rows:
        fecha, partido, pick, linea, cuota, edge, status, ph, pa = r
        color = {"WON": "#10b981", "LOST": "#ef4444", "VOID": "#64748b"}.get(status, "#eab308")
        score_txt = f"{ph} - {pa}" if ph is not None else "-"
        html_cards += (
            f'<div class="card" style="border-left: 4px solid {color};">'
            f'<div style="display:flex; justify-content:space-between; margin-bottom:10px;">'
            f'<span style="font-weight:bold;">{escape(partido)}</span>'
            f'<span style="color:{color}; font-weight:bold;">{escape(status)}</span></div>'
            f'<div style="display:flex; justify-content:space-between; font-size:14px; margin-bottom:5px;">'
            f'<span>💡 {escape(pick)} {linea:+.1f} @ {cuota:.2f}</span>'
            f'<span>Edge: +{edge:.1f}%</span></div>'
            f'<div style="font-size:12px; color:#94a3b8; text-align:center; margin-top:10px; padding-top:10px; border-top:1px solid #334155;">'
            f'Marcador Final: {score_txt}</div></div>'
        )

    html_out = f"""<!DOCTYPE html><html><head><title>Historial Allbets</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0b1120; color: white; padding: 16px; max-width: 500px; margin: auto; }}
.card {{ background: #1e293b; border-radius: 16px; padding: 20px; margin-bottom: 15px; border: 1px solid #334155; }}
.nav {{ text-align: center; margin-bottom: 20px; }}
.nav a {{ color: #eab308; text-decoration: none; font-weight: bold; margin: 0 10px; }}
.stats {{ display:flex; gap:10px; margin-bottom:20px; }}
.stat {{ flex:1; background:#1e293b; padding:15px; border-radius:10px; text-align:center; border:1px solid #334155; }}
.stat-val {{ font-size:24px; font-weight:bold; }}
</style></head><body>
<div class="nav"><a href="/predictor">Predicciones</a> | <a href="/historial">Historial</a></div>
<h2 style="font-size: 18px; margin-bottom: 20px;">Historial de Apuestas</h2>
<div class="stats">
  <div class="stat"><div style="font-size:12px; color:#94a3b8;">GANADAS</div><div class="stat-val" style="color:#10b981;">{won}</div></div>
  <div class="stat"><div style="font-size:12px; color:#94a3b8;">PERDIDAS</div><div class="stat-val" style="color:#ef4444;">{lost}</div></div>
  <div class="stat"><div style="font-size:12px; color:#94a3b8;">VOID</div><div class="stat-val" style="color:#64748b;">{void}</div></div>
</div>
{html_cards if html_cards else "<p style='text-align:center;'>No hay apuestas registradas aún.</p>"}
</body></html>"""
    return html_out

# ==========================================
# MODO DEV (opcional)
# ==========================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # En producción usar: uvicorn app:app --host 0.0.0.0 --port $PORT
    uvicorn.run("app:app", host="0.0.0.0", port=port)

@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}
