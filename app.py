# ==========================================
# ALLBETS — Value Betting Bot (Triple Media + Poisson + ML)
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
# CONFIGURACIÓN
# ==========================================
EDGE_MIN             = 3.0    # % mínimo para considerar que hay "Valor"
CACHE_STATS_TTL      = 86400  # 24 h
CACHE_ODDS_TTL       = 300    # 5 min
CACHE_INJURIES_TTL   = 1800   # 30 min
PENDING_TIMEOUT_DAYS = 3

TEAM_KEYS = {
    "atlanta hawks": "ATL", "boston celtics": "BOS", "brooklyn nets": "BKN",
    "charlotte hornets": "CHA", "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL", "denver nuggets": "DEN", "detroit pistons": "DET",
    "golden state warriors": "GSW", "houston rockets": "HOU", "indiana pacers": "IND",
    "la clippers": "LAC", "los angeles clippers": "LAC", "la lakers": "LAL", "los angeles lakers": "LAL",
    "memphis grizzlies": "MEM", "miami heat": "MIA", "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN", "new orleans pelicans": "NOP", "new york knicks": "NY",
    "oklahoma city thunder": "OKC", "orlando magic": "ORL", "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX", "portland trail blazers": "POR", "sacramento kings": "SAC",
    "san antonio spurs": "SAS", "toronto raptors": "TOR", "utah jazz": "UTA", "washington wizards": "WAS",
}

def get_team_key(name):
    return TEAM_KEYS.get(str(name).strip().lower(), str(name).strip().lower())

def norm(name):
    return str(name).strip().lower()

# ==========================================
# CACHÉ
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

# ==========================================
# SCHEDULER Y LIFESPAN
# ==========================================
scheduler = BackgroundScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(procesar_mercado, "interval", minutes=60, id="allbets_job", coalesce=True, max_instances=1)
    scheduler.start()
    print("🚀 Scheduler iniciado: guardando historial de predicciones cada 60 minutos.")
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

init_db()

# ==========================================
# MATEMÁTICA (POISSON CORREGIDO)
# ==========================================
def generar_poisson_pmf(lam, max_puntos=250):
    if lam <= 0:
        return [1.0] + [0.0] * (max_puntos - 1)
    probs = []
    p = math.exp(-lam)
    probs.append(p)
    for k in range(1, max_puntos):
        p = p * lam / k
        probs.append(p)
    return probs

def calcular_probabilidades_poisson(proj_home, proj_away, total_line):
    max_puntos = 250
    prob_home_gana, prob_away_gana = 0.0, 0.0
    prob_over, prob_under = 0.0, 0.0

    total_lambda = proj_home + proj_away
    p_total_vals = generar_poisson_pmf(total_lambda, max_puntos)
    
    for total_pts in range(max_puntos):
        if total_pts > total_line: prob_over += p_total_vals[total_pts]
        else: prob_under += p_total_vals[total_pts]

    p_h_vals = generar_poisson_pmf(proj_home, max_puntos)
    p_a_vals = generar_poisson_pmf(proj_away, max_puntos)

    for h_pts in range(max_puntos):
        if p_h_vals[h_pts] < 1e-10: continue
        for a_pts in range(max_puntos):
            if p_a_vals[a_pts] < 1e-10: continue
            prob = p_h_vals[h_pts] * p_a_vals[a_pts]

            if h_pts > a_pts: prob_home_gana += prob
            elif a_pts > h_pts: prob_away_gana += prob

    return prob_home_gana, prob_away_gana, prob_over, prob_under

def implied_fair_pcts(odds_a, odds_b):
    if odds_a <= 1.0 or odds_b <= 1.0: return 0.0, 0.0
    ia, ib = 1.0 / odds_a, 1.0 / odds_b
    t = ia + ib
    return 100.0 * ia / t, 100.0 * ib / t

# ==========================================
# ESTADÍSTICAS AVANZADAS (TRIPLE MEDIA)
# ==========================================
def get_advanced_stats():
    try:
        r_teams = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams", timeout=15)
        teams = r_teams.json().get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        
        stats_dict = {}
        total_home_pts, total_away_pts, total_games = 0, 0, 0
        processed_games = set()

        for t in teams:
            team_id = t["team"]["id"]
            team_name = norm(t["team"]["displayName"])
            
            pf_gen, pa_gen, gp_gen = 0, 0, 0
            pf_loc, pa_loc, gp_loc = 0, 0, 0
            pf_vis, pa_vis, gp_vis = 0, 0, 0
            last5_pf, last5_pa, last5_gp = 0, 0, 0
            recent_games = []

            r_sched = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/schedule", timeout=15)
            events = r_sched.json().get("events", [])

            for ev in events:
                comp = ev.get("competitions", [{}])[0]
                if not comp.get("status", {}).get("type", {}).get("completed"): continue
                
                game_id = ev["id"]
                is_home = False
                team_pts, opp_pts = 0, 0

                for c in comp.get("competitors", []):
                    score = int(c.get("score", {}).get("value", 0) or 0)
                    if str(c["team"]["id"]) == str(team_id):
                        team_pts = score
                        is_home = c["homeAway"] == "home"
                    else:
                        opp_pts = score

                if game_id not in processed_games:
                    if is_home: total_home_pts += team_pts
                    else: total_away_pts += team_pts
                    total_games += 1
                    processed_games.add(game_id)

                pf_gen += team_pts; pa_gen += opp_pts; gp_gen += 1
                if is_home: pf_loc += team_pts; pa_loc += opp_pts; gp_loc += 1
                else: pf_vis += team_pts; pa_vis += opp_pts; gp_vis += 1
                recent_games.append({"pf": team_pts, "pa": opp_pts})

            for g in recent_games[-5:]:
                last5_pf += g["pf"]; last5_pa += g["pa"]; last5_gp += 1

            stats_dict[team_name] = {
                "id": team_id,
                "pf_gen": pf_gen / max(1, gp_gen), "pa_gen": pa_gen / max(1, gp_gen),
                "pf_loc": pf_loc / max(1, gp_loc), "pa_loc": pa_loc / max(1, gp_loc),
                "pf_vis": pf_vis / max(1, gp_vis), "pa_vis": pa_vis / max(1, gp_vis),
                "pf_l5": last5_pf / max(1, last5_gp), "pa_l5": last5_pa / max(1, last5_gp),
            }

        liga_home_avg = total_home_pts / max(1, total_games / 2)
        liga_away_avg = total_away_pts / max(1, total_games / 2)

        return {"teams": stats_dict, "liga_home": liga_home_avg, "liga_away": liga_away_avg}
    except Exception as e:
        print(f"⚠️ Error advanced stats: {e}")
        return {}

# ==========================================
# LESIONES
# ==========================================
def _fetch_injuries_global():
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries", timeout=10)
        by_team = {}
        for team in r.json().get("injuries", []):
            by_team[str(team.get("team", {}).get("id", ""))] = team.get("injuries", [])
        return by_team
    except Exception:
        return {}

def get_injury_penalty(team_id, injuries_global):
    team_injuries = injuries_global.get(str(team_id), [])
    if not team_injuries: return 0.0, ""
    
    penalty = 0.0
    txt = ""
    for inj in team_injuries:
        status = inj.get("status", "")
        athlete = inj.get("athlete", {}).get("displayName", "")
        short = athlete.split()[-1] if athlete else ""
        
        if status in ("Out", "Doubtful"):
            penalty -= 2.5
            txt += f"❌ {short} "
        elif status == "Day-To-Day":
            penalty -= 1.0
            txt += f"❓ {short} "
    return penalty, txt.strip()

# ==========================================
# CUOTAS EN 1XBET (MONEYLINE + TOTALS)
# ==========================================
def get_all_nba_odds():
    if not ODDS_API_KEY: return {}
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "eu,us", "markets": "h2h,totals", "oddsFormat": "decimal"}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200: return {}
        games = r.json()
        odds_dict = {}

        for game in games:
            h_api, a_api = game.get("home_team", ""), game.get("away_team", "")
            key = f"{get_team_key(a_api)}_vs_{get_team_key(h_api)}"
            bookmakers = game.get("bookmakers", [])
            book_1xbet = next((b for b in bookmakers if b.get("key") == "onexbet"), None)
            if not book_1xbet: continue

            data = {"moneyline": None, "total": None}
            for market in book_1xbet.get("markets", []):
                if market.get("key") == "h2h":
                    ml = {}
                    for out in market.get("outcomes", []):
                        if get_team_key(out.get("name", "")) == get_team_key(h_api):
                            ml["home_odds"] = float(out.get("price", 0))
                        else:
                            ml["away_odds"] = float(out.get("price", 0))
                    if len(ml) == 2: data["moneyline"] = ml
                
                elif market.get("key") == "totals":
                    total = {}
                    for out in market.get("outcomes", []):
                        if out.get("name", "").lower() == "over":
                            total["line"] = float(out.get("point", 0))
                            total["over_odds"] = float(out.get("price", 0))
                        else:
                            total["under_odds"] = float(out.get("price", 0))
                    if len(total) == 3: data["total"] = total

            odds_dict[key] = data
        return odds_dict
    except Exception as e:
        print(f"⚠️ Error odds: {e}")
        return {}

# ==========================================
# PERSISTENCIA Y AUDITORÍA
# ==========================================
def guardar_prediccion(event_id, partido, pick, linea, cuota, edge):
    try:
        with db() as con:
            existing = con.execute("SELECT id FROM predicciones_allbets WHERE event_id=? AND pick=?", (event_id, pick)).fetchone()
            if existing: return
            con.execute("""
                INSERT INTO predicciones_allbets (event_id, fecha, partido, pick, linea, cuota, edge, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (event_id, datetime.now(timezone.utc).isoformat(), partido, pick, linea, cuota, edge))
            print(f"   ✅ Guardado: {pick} {linea:+.1f} @ {cuota:.2f} (Edge: {edge:.1f}%)")
    except Exception as e:
        print(f"   ❌ Error guardando: {e}")

def auditar_predicciones():
    try:
        with db() as con:
            pendientes = con.execute("SELECT id, event_id, pick, linea, fecha FROM predicciones_allbets WHERE status='PENDING'").fetchall()
        if not pendientes: return
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games = {ev.get("id"): ev for ev in r.json().get("events", [])}
        ahora = datetime.now(timezone.utc)

        for pred_id, event_id, pick, linea, fecha_str in pendientes:
            try:
                fecha = datetime.fromisoformat(fecha_str)
                if ahora - fecha > timedelta(days=PENDING_TIMEOUT_DAYS):
                    with db() as con: con.execute("UPDATE predicciones_allbets SET status='VOID' WHERE id=?", (pred_id,))
                    continue
            except: pass

            g = games.get(event_id)
            if not g or g.get("status", {}).get("type", {}).get("state") != "post": continue

            comp = g.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away: continue

            pts_home = int(home.get("score", 0))
            pts_away = int(away.get("score", 0))
            
            status = "LOST"
            if "OVER" in pick or "UNDER" in pick:
                total_final = pts_home + pts_away
                if "OVER" in pick and total_final > linea: status = "WON"
                if "UNDER" in pick and total_final < linea: status = "WON"
                if total_final == linea: status = "PUSH"
            else:
                is_home_pick = get_team_key(pick) == get_team_key(home["team"]["displayName"])
                if (is_home_pick and pts_home > pts_away) or (not is_home_pick and pts_away > pts_home):
                    status = "WON"
                elif pts_home == pts_away:
                    status = "PUSH"

            with db() as con:
                con.execute("UPDATE predicciones_allbets SET status=?, puntos_home=?, puntos_away=? WHERE id=?", (status, pts_home, pts_away, pred_id))
    except Exception as e:
        print(f"⚠️ Error auditoría: {e}")

# ==========================================
# LÓGICA DEL BOT (BACKGROUND)
# ==========================================
def procesar_mercado():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Analizando jornada NBA y guardando historial...")
    try:
        _procesar_mercado_inner()
    finally:
        auditar_predicciones()

def _procesar_mercado_inner():
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games_raw = r.json().get("events", [])
    except: return

    partidos_hoy = [g for g in games_raw if g.get("status", {}).get("type", {}).get("state") == "pre"]
    if not partidos_hoy: return

    odds = _cached("odds", CACHE_ODDS_TTL, get_all_nba_odds)
    if not odds: return

    adv_stats = _cached("adv_stats", CACHE_STATS_TTL, get_advanced_stats)
    if not adv_stats or "teams" not in adv_stats: return
    
    injuries_global = _cached("injuries", CACHE_INJURIES_TTL, _fetch_injuries_global)
    stats_norm = adv_stats["teams"]
    liga_home = adv_stats["liga_home"]
    liga_away = adv_stats["liga_away"]

    for ev in partidos_hoy:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2: continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name, a_name = norm(home.get("team", {}).get("displayName", "")), norm(away.get("team", {}).get("displayName", ""))
        event_id = ev.get("id")

        if h_name not in stats_norm or a_name not in stats_norm: continue

        h = stats_norm[h_name]
        a = stats_norm[a_name]

        pen_home, _ = get_injury_penalty(h["id"], injuries_global)
        pen_away, _ = get_injury_penalty(a["id"], injuries_global)

        ataque_local = (h["pf_gen"] + h["pf_loc"] + h["pf_l5"]) / 3
        defensa_visita = (a["pa_gen"] + a["pa_vis"] + a["pa_l5"]) / 3
        proy_home = (ataque_local + defensa_visita + liga_home) / 3 + pen_home

        ataque_visita = (a["pf_gen"] + a["pf_vis"] + a["pf_l5"]) / 3
        defensa_local = (h["pa_gen"] + h["pa_loc"] + h["pa_l5"]) / 3
        proy_away = (ataque_visita + defensa_local + liga_away) / 3 + pen_away

        key = f"{get_team_key(a_name)}_vs_{get_team_key(h_name)}"
        cuotas = odds.get(key)
        if not cuotas: continue

        ml_data = cuotas.get("moneyline")
        total_data = cuotas.get("total")

        total_l = total_data["line"] if total_data else 220

        p_h_win, p_a_win, p_over, p_under = calcular_probabilidades_poisson(
            proy_home, proy_away, total_l
        )

        if ml_data:
            fair_h, fair_a = implied_fair_pcts(ml_data["home_odds"], ml_data["away_odds"])
            edge_h = (p_h_win * 100) - fair_h
            edge_a = (p_a_win * 100) - fair_a
            
            if p_h_win >= p_a_win:
                guardar_prediccion(event_id, f"{h_name.title()} vs {a_name.title()}", h_name.title(), 0, ml_data["home_odds"], edge_h)
            else:
                guardar_prediccion(event_id, f"{h_name.title()} vs {a_name.title()}", a_name.title(), 0, ml_data["away_odds"], edge_a)

        if total_data:
            fair_o, fair_u = implied_fair_pcts(total_data["over_odds"], total_data["under_odds"])
            edge_o = (p_over * 100) - fair_o
            edge_u = (p_under * 100) - fair_u
            
            if p_over >= p_under:
                guardar_prediccion(event_id, f"{h_name.title()} vs {a_name.title()}", "OVER", total_l, total_data["over_odds"], edge_o)
            else:
                guardar_prediccion(event_id, f"{h_name.title()} vs {a_name.title()}", "UNDER", total_l, total_data["under_odds"], edge_u)

# ==========================================
# RUTAS WEB
# ==========================================
@app.get("/")
def read_root():
    return {"mensaje": "Allbets — ML + Totales. Ve a /predictor o /historial"}

@app.get("/predictor", response_class=HTMLResponse)
def predictor_html():
    adv_stats = _cached("adv_stats", CACHE_STATS_TTL, get_advanced_stats)
    if not adv_stats or "teams" not in adv_stats:
        return HTMLResponse("<h1>Calculando estadísticas avanzadas... Recarga en unos segundos.</h1>")
    
    injuries_global = _cached("injuries", CACHE_INJURIES_TTL, _fetch_injuries_global)
    stats_norm = adv_stats["teams"]
    liga_home = adv_stats["liga_home"]
    liga_away = adv_stats["liga_away"]

    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games_raw = r.json().get("events", [])
    except Exception:
        return HTMLResponse("<h1>Error cargando partidos</h1>")

    all_odds = _cached("odds", CACHE_ODDS_TTL, get_all_nba_odds)
    html_cards = ""

    for ev in games_raw:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2: continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        h_name, a_name = norm(home.get("team", {}).get("displayName", "")), norm(away.get("team", {}).get("displayName", ""))

        if h_name not in stats_norm or a_name not in stats_norm: continue

        h = stats_norm[h_name]
        a = stats_norm[a_name]

        pen_home, txt_home = get_injury_penalty(h["id"], injuries_global)
        pen_away, txt_away = get_injury_penalty(a["id"], injuries_global)

        ataque_local = (h["pf_gen"] + h["pf_loc"] + h["pf_l5"]) / 3
        defensa_visita = (a["pa_gen"] + a["pa_vis"] + a["pa_l5"]) / 3
        proy_home = (ataque_local + defensa_visita + liga_home) / 3 + pen_home

        ataque_visita = (a["pf_gen"] + a["pf_vis"] + a["pf_l5"]) / 3
        defensa_local = (h["pa_gen"] + h["pa_loc"] + h["pa_l5"]) / 3
        proy_away = (ataque_visita + defensa_local + liga_away) / 3 + pen_away

        key = f"{get_team_key(a_name)}_vs_{get_team_key(h_name)}"
        cuotas = all_odds.get(key)
        valor_html = ""
        
        # INICIALIZAMOS VARIABLES PARA EVITAR ERRORES
        ml_data = None
        total_data = None

        if cuotas:
            ml_data = cuotas.get("moneyline")
            total_data = cuotas.get("total")
            
            total_l = total_data["line"] if total_data else 220

            p_h_win, p_a_win, p_over, p_under = calcular_probabilidades_poisson(
                proy_home, proy_away, total_l
            )

            prob_home = p_h_win * 100
            prob_away = p_a_win * 100

            if ml_data:
                fair_h, fair_a = implied_fair_pcts(ml_data["home_odds"], ml_data["away_odds"])
                edge_h = (p_h_win * 100) - fair_h
                edge_a = (p_a_win * 100) - fair_a

                if p_h_win >= p_a_win and edge_h > EDGE_MIN:
                    valor_html = (f'<div class="value-box" style="background: rgba(16,185,129,0.1); border-color: #10b981;">'
                                  f'<div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET (ML)</div>'
                                  f'<div class="value-bet">{escape(h_name.title())} (Gana) @ {ml_data["home_odds"]:.2f}</div>'
                                  f'<div class="value-edge" style="color: #10b981;">Edge: +{edge_h:.1f}%</div></div>')
                elif p_a_win > p_h_win and edge_a > EDGE_MIN:
                    valor_html = (f'<div class="value-box" style="background: rgba(16,185,129,0.1); border-color: #10b981;">'
                                  f'<div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET (ML)</div>'
                                  f'<div class="value-bet">{escape(a_name.title())} (Gana) @ {ml_data["away_odds"]:.2f}</div>'
                                  f'<div class="value-edge" style="color: #10b981;">Edge: +{edge_a:.1f}%</div></div>')
            
            if total_data:
                fair_o, fair_u = implied_fair_pcts(total_data["over_odds"], total_data["under_odds"])
                edge_o = (p_over * 100) - fair_o
                edge_u = (p_under * 100) - fair_u

                if edge_o > EDGE_MIN:
                    valor_html += (f'<div class="value-box" style="background: rgba(16,185,129,0.1); border-color: #10b981; margin-top: 10px;">'
                                   f'<div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET (Total)</div>'
                                   f'<div class="value-bet">OVER {total_l} @ {total_data["over_odds"]:.2f}</div>'
                                   f'<div class="value-edge" style="color: #10b981;">Edge: +{edge_o:.1f}%</div></div>')
                elif edge_u > EDGE_MIN:
                    valor_html += (f'<div class="value-box" style="background: rgba(16,185,129,0.1); border-color: #10b981; margin-top: 10px;">'
                                   f'<div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET (Total)</div>'
                                   f'<div class="value-bet">UNDER {total_l} @ {total_data["under_odds"]:.2f}</div>'
                                   f'<div class="value-edge" style="color: #10b981;">Edge: +{edge_u:.1f}%</div></div>')

            if not valor_html:
                valor_html = (f'<div class="value-box" style="background: rgba(148,163,184,0.1); border-color: #94a3b8;">'
                              f'<div class="value-title" style="color: #94a3b8;">ANALIZADO (1xBet)</div>'
                              f'<div class="value-bet">Sin ventaja clara</div></div>')
        else:
            prob_home = 0
            prob_away = 0
            valor_html = (f'<div class="value-box" style="background: rgba(148,163,184,0.1); border-color: #94a3b8;">'
                          f'<div class="value-title" style="color: #94a3b8;">SIN CUOTAS</div>'
                          f'<div class="value-bet">1xBet no ha abierto el mercado</div></div>')

        total_proyectado = proy_home + proy_away
        if total_data:
            linea_pre = total_data["line"]
            rec_ou = "OVER" if total_proyectado > linea_pre else "UNDER"
            color_ou = "#10b981" if rec_ou == "OVER" else "#ef4444"
            ou_html = (f'<div style="margin-top: 10px; text-align: center; font-size: 13px;">'
                       f'Proyección Total: <b>{total_proyectado:.1f}</b> vs Línea Pregame: <b>{linea_pre}</b><br>'
                       f'Recomendación: <b style="color:{color_ou};">{rec_ou}</b></div>')
        else:
            ou_html = ""

        injury_html = ""
        if txt_home or txt_away:
            injury_html = (f'<div style="font-size:11px; color:#ef4444; text-align:center; margin-top:10px;">'
                           f'<b>Lesiones:</b> {escape(txt_home)} {escape(txt_away)}</div>')

        html_cards += f"""
        <div class="card">
            <div class="card-header"><div class="league">🏀 NBA</div><div class="time">Hoy</div></div>
            <div class="matchup">
                <div class="team"><div class="team-name">{escape(a_name.title())}</div><div class="team-prob">{prob_away:.0f}%</div></div>
                <div class="vs"><div class="gauge"><div class="gauge-fill" style="width: {prob_home if prob_home > prob_away else prob_away}%;"></div></div><div class="fav-text">{escape(h_name.title() if prob_home > prob_away else a_name.title())}<br>TO WIN</div></div>
                <div class="team"><div class="team-name">{escape(h_name.title())}</div><div class="team-prob">{prob_home:.0f}%</div></div>
            </div>
            <div class="projection"><div class="proj-box"><div class="proj-label">PROYECCIÓN TRIPLE MEDIA</div><div class="proj-score">{proy_away:.0f} - {proy_home:.0f}</div></div></div>
            {valor_html}
            {ou_html}
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
        rows = con.execute("SELECT fecha, partido, pick, linea, cuota, edge, status, puntos_home, puntos_away FROM predicciones_allbets ORDER BY id DESC LIMIT 100").fetchall()

    won = sum(1 for r in rows if r[6] == "WON")
    lost = sum(1 for r in rows if r[6] == "LOST")
    void = sum(1 for r in rows if r[6] == "VOID")

    html_cards = ""
    for r in rows:
        fecha, partido, pick, linea, cuota, edge, status, ph, pa = r
        
        is_value = edge >= EDGE_MIN
        value_badge = '<span style="color:#10b981; font-size:10px; font-weight:bold;">💎 VALUE</span>' if is_value else '<span style="color:#64748b; font-size:10px;">NO VALUE</span>'
        
        color = {"WON": "#10b981", "LOST": "#ef4444", "VOID": "#64748b"}.get(status, "#eab308")
        score_txt = f"{ph} - {pa}" if ph is not None else "-"
        
        linea_txt = f" {linea:+.1f}" if linea != 0 else ""
        
        html_cards += (
            f'<div class="card" style="border-left: 4px solid {color};">'
            f'<div style="display:flex; justify-content:space-between; margin-bottom:5px;">'
            f'<span style="font-weight:bold;">{escape(partido)}</span>'
            f'<span style="color:{color}; font-weight:bold;">{escape(status)}</span></div>'
            f'<div style="display:flex; justify-content:space-between; align-items:center; font-size:14px; margin-bottom:5px;">'
            f'<span>💡 {escape(pick)}{linea_txt} @ {cuota:.2f}</span>'
            f'{value_badge}</div>'
            f'<div style="display:flex; justify-content:space-between; font-size:11px; color:#94a3b8;">'
            f'<span>Edge: {edge:+.1f}%</span>'
            f'<span>Final: {score_txt}</span></div>'
            f'</div>'
        )

    html_out = f"""<!DOCTYPE html><html><head><title>Historial Allbets</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0b1120; color: white; padding: 16px; max-width: 500px; margin: auto; }}
.card {{ background: #1e293b; border-radius: 12px; padding: 15px; margin-bottom: 10px; border: 1px solid #334155; }}
.nav {{ text-align: center; margin-bottom: 20px; }}
.nav a {{ color: #eab308; text-decoration: none; font-weight: bold; margin: 0 10px; }}
.stats {{ display:flex; gap:10px; margin-bottom:20px; }}
.stat {{ flex:1; background:#1e293b; padding:15px; border-radius:10px; text-align:center; border:1px solid #334155; }}
.stat-val {{ font-size:24px; font-weight:bold; }}
</style></head><body>
<div class="nav"><a href="/predictor">Predicciones</a> | <a href="/historial">Historial</a></div>
<h2 style="font-size: 18px; margin-bottom: 20px;">Historial de Predicciones</h2>
<div class="stats">
  <div class="stat"><div style="font-size:12px; color:#94a3b8;">GANADAS</div><div class="stat-val" style="color:#10b981;">{won}</div></div>
  <div class="stat"><div style="font-size:12px; color:#94a3b8;">PERDIDAS</div><div class="stat-val" style="color:#ef4444;">{lost}</div></div>
  <div class="stat"><div style="font-size:12px; color:#94a3b8;">VOID</div><div class="stat-val" style="color:#64748b;">{void}</div></div>
</div>
{html_cards if html_cards else "<p style='text-align:center;'>No hay predicciones registradas aún.</p>"}
</body></html>"""
    return html_out

@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
