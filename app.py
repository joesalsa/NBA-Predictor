import math
import os
import requests
import libsql
from datetime import datetime, timezone
from contextlib import contextmanager, asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

# ==========================================
# VARIABLES DE ENTORNO
# ==========================================
TURSO_URL     = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN")
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY")

# ==========================================
# SCHEDULER Y LIFESPAN
# ==========================================
scheduler = BackgroundScheduler(timezone="UTC")

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(procesar_mercado, "interval", minutes=60, id="zcode_job", coalesce=True, max_instances=1)
    scheduler.start()
    print("🚀 Scheduler iniciado: Buscando valor en 1xBet cada 60 minutos.")
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
            CREATE TABLE IF NOT EXISTS predicciones_zcode (
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
# MATEMÁTICA Y NORMALIZADOR
# ==========================================
def normal_cdf(x, mu=0, sigma=1):
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

def get_team_key(name):
    n = name.lower()
    if "hawks" in n or "atl" in n: return "ATL"
    if "celtics" in n or "bos" in n: return "BOS"
    if "nets" in n or "bkn" in n: return "BKN"
    if "hornets" in n or "cha" in n: return "CHA"
    if "bulls" in n or "chi" in n: return "CHI"
    if "cavaliers" in n or "cavs" in n or "cle" in n: return "CLE"
    if "mavericks" in n or "mavs" in n or "dal" in n: return "DAL"
    if "nuggets" in n or "den" in n: return "DEN"
    if "pistons" in n or "det" in n: return "DET"
    if "warriors" in n or "gsw" in n or "golden state" in n: return "GSW"
    if "rockets" in n or "hou" in n: return "HOU"
    if "pacers" in n or "ind" in n: return "IND"
    if "clippers" in n or "lac" in n: return "LAC"
    if "lakers" in n or "lal" in n: return "LAL"
    if "grizzlies" in n or "mem" in n: return "MEM"
    if "heat" in n or "mia" in n: return "MIA"
    if "bucks" in n or "mil" in n: return "MIL"
    if "timberwolves" in n or "wolves" in n or "min" in n: return "MIN"
    if "pelicans" in n or "nop" in n: return "NOP"
    if "knicks" in n or "nyk" in n: return "NY"
    if "thunder" in n or "okc" in n: return "OKC"
    if "magic" in n or "orl" in n: return "ORL"
    if "76ers" in n or "sixers" in n or "phi" in n: return "PHI"
    if "suns" in n or "phx" in n or "pho" in n: return "PHX"
    if "blazers" in n or "trail blazers" in n or "por" in n: return "POR"
    if "kings" in n or "sac" in n: return "SAC"
    if "spurs" in n or "sas" in n: return "SAS"
    if "raptors" in n or "tor" in n: return "TOR"
    if "jazz" in n or "uta" in n: return "UTA"
    if "wizards" in n or "was" in n: return "WAS"
    return n

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
                stats = entry.get("stats", [])
                pts_for = 110.0
                pts_against = 110.0
                for s in stats:
                    if s.get("name") == "avgPointsFor": pts_for = float(s.get("value", 110.0))
                    elif s.get("name") == "avgPointsAgainst": pts_against = float(s.get("value", 110.0))
                stats_dict[team_name] = {"pf": pts_for, "pa": pts_against, "id": team_id}
    except Exception as e:
        print(f"Error stats: {e}")
    return stats_dict

# ==========================================
# LESIONES Y TITULARES (ESPN)
# ==========================================
def get_injury_penalty(team_id):
    """Devuelve cuántos puntos restar a la proyección por lesiones."""
    penalty = 0.0
    lesionados_txt = ""
    
    try:
        # 1. Obtener lista global de lesionados
        url_inj = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
        r = requests.get(url_inj, timeout=10)
        data = r.json()
        
        team_injuries = []
        for team in data.get("injuries", []):
            if str(team.get("team", {}).get("id")) == str(team_id):
                team_injuries = team.get("injuries", [])
                break
                
        if not team_injuries: return 0.0, ""

        # 2. Obtener Depth Chart (Quiénes son titulares)
        url_depth = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/depthchart"
        depth_data = requests.get(url_depth, timeout=10).json()
        
        titulares = set()
        for group in depth_data.get("depthChart", {}).get("positions", []):
            # El primer jugador de cada posición es el titular
            if group.get("athletes"):
                titulares.add(group["athletes"][0]["displayName"])
                
        # 3. Cruzar lesiones con titulares
        for inj in team_injuries:
            status = inj.get("status", "")
            athlete = inj.get("athlete", {}).get("displayName", "")
            short_name = athlete.split()[-1] # Solo el apellido para la UI
            
            if status in ["Out", "Doubtful"]:
                if athlete in titulares:
                    penalty -= 3.5
                    lesionados_txt += f"❌ {short_name} (Titular) "
                else:
                    penalty -= 1.0
                    lesionados_txt += f"⚠️ {short_name} (Banca) "
            elif status == "Day-To-Day":
                if athlete in titulares:
                    penalty -= 1.5
                    lesionados_txt += f"❓ {short_name} (Titular) "
                else:
                    penalty -= 0.5
                    lesionados_txt += f"❓ {short_name} (Banca) "
                    
    except Exception as e:
        pass # Si falla ESPN, no penalizamos y seguimos
        
    return penalty, lesionados_txt.strip()

# ==========================================
# CUOTAS EN 1XBET (THE ODDS API)
# ==========================================
def get_all_nba_odds():
    if not ODDS_API_KEY: return {}
    
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    # Usamos "eu" para que aparezca 1xBet
    params = {"apiKey": ODDS_API_KEY, "regions": "eu,us", "markets": "spreads", "oddsFormat": "decimal"}
    
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200: return {}
        
        games = r.json()
        odds_dict = {}
        
        for game in games:
            h_api, a_api = game.get("home_team", ""), game.get("away_team", "")
            key = f"{get_team_key(a_api)}_vs_{get_team_key(h_api)}"
            
            bookmakers = game.get("bookmakers", [])
            # FILTRO: Buscar exclusivamente 1xBet
            book_1xbet = next((b for b in bookmakers if b.get("key") == "onexbet"), None)
            
            if not book_1xbet: continue 
            
            for market in book_1xbet.get("markets", []):
                if market.get("key") == "spreads":
                    spread_data = {}
                    for out in market.get("outcomes", []):
                        if get_team_key(out.get("name", "")) == get_team_key(h_api):
                            spread_data["home_line"] = float(out.get("point", 0))
                            spread_data["home_odds"] = float(out.get("price", 0))
                        elif get_team_key(out.get("name", "")) == get_team_key(a_api):
                            spread_data["away_line"] = float(out.get("point", 0))
                            spread_data["away_odds"] = float(out.get("price", 0))
                    if len(spread_data) == 4: 
                        odds_dict[key] = {"spread": spread_data}
        return odds_dict
    except Exception:
        return {}

# ==========================================
# LÓGICA DEL BOT EN SEGUNDO PLANO
# ==========================================
def guardar_prediccion(event_id, partido, pick, linea, cuota, edge):
    try:
        with db() as con:
            cur = con.execute("""
                INSERT OR IGNORE INTO predicciones_zcode 
                (event_id, fecha, partido, pick, linea, cuota, edge, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (event_id, datetime.now(timezone.utc).isoformat(), partido, pick, linea, cuota, edge))
            if cur.rowcount > 0:
                print(f"   ✅ Guardado: {pick} {linea:+.1f} (Edge: {edge:.1f}%)")
    except Exception: pass

def auditar_predicciones():
    try:
        with db() as con:
            pendientes = con.execute("SELECT id, event_id, pick, linea FROM predicciones_zcode WHERE status='PENDING'").fetchall()
        if not pendientes: return

        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games = {ev.get("id"): ev for ev in r.json().get("events", [])}
        
        for pred_id, event_id, pick, linea in pendientes:
            g = games.get(event_id)
            if not g: continue
            if g.get("status", {}).get("type", {}).get("state") == "post":
                comp = g.get("competitions", [{}])[0]
                cs = comp.get("competitors", [])
                home = next((c for c in cs if c.get("homeAway") == "home"), None)
                away = next((c for c in cs if c.get("homeAway") == "away"), None)
                if not home or not away: continue
                
                pts_home, pts_away = int(home.get("score", 0)), int(away.get("score", 0))
                is_home_pick = get_team_key(pick) == get_team_key(home["team"]["displayName"])
                margen_final = (pts_home - pts_away) + linea if is_home_pick else (pts_away - pts_home) + linea
                    
                status = "WON" if margen_final > 0 else "LOST" if margen_final < 0 else "PUSH"
                with db() as con:
                    con.execute("UPDATE predicciones_zcode SET status=?, puntos_home=?, puntos_away=? WHERE id=?", (status, pts_home, pts_away, pred_id))
    except Exception: pass

def procesar_mercado():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Analizando jornada NBA (1xBet)...")
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games_raw = r.json().get("events", [])
    except Exception: return

    partidos_hoy = [g for g in games_raw if g.get("status", {}).get("type", {}).get("state") == "pre"]
    if not partidos_hoy:
        print("💤 No hay partidos próximos. 0 créditos gastados.")
        auditar_predicciones()
        return

    all_odds = get_all_nba_odds()
    if not all_odds: 
        print("⚠️ 1xBet aún no abre mercado para estos partidos.")
        return

    stats = get_nba_stats()
    
    for ev in partidos_hoy:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2: continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        
        h_name, a_name = home.get("team", {}).get("displayName", ""), away.get("team", {}).get("displayName", "")
        event_id = ev.get("id")
        
        if h_name not in stats or a_name not in stats: continue
            
        h_pf, h_pa, h_id = stats[h_name]['pf'], stats[h_name]['pa'], stats[h_name]['id']
        a_pf, a_pa, a_id = stats[a_name]['pf'], stats[a_name]['pa'], stats[a_name]['id']
        
        # 1. Calcular impacto de lesiones
        pen_home, txt_home = get_injury_penalty(h_id)
        pen_away, txt_away = get_injury_penalty(a_id)
        
        # 2. Proyección ajustada por lesiones
        proy_home = (h_pf + a_pa) / 2 + 1.25 + pen_home
        proy_away = (a_pf + h_pa) / 2 - 1.25 + pen_away
        margen = proy_home - proy_away
        
        key = f"{get_team_key(a_name)}_vs_{get_team_key(h_name)}"
        cuotas = all_odds.get(key)
        
        if cuotas and cuotas.get("spread"):
            home_line, home_odds = cuotas["spread"]["home_line"], cuotas["spread"]["home_odds"]
            away_line, away_odds = cuotas["spread"]["away_line"], cuotas["spread"]["away_odds"]
            
            prob_home_spread = normal_cdf((margen + home_line) / 11.5) * 100
            prob_away_spread = normal_cdf((-margen + away_line) / 11.5) * 100
            
            edge_home = prob_home_spread - (100 / home_odds) if home_odds > 1.0 else -100
            edge_away = prob_away_spread - (100 / away_odds) if away_odds > 1.0 else -100
            
            # Imprimir análisis en consola
            print(f"🏀 {a_name} vs {h_name} | Lesiones: {txt_home or 'Ninguna'} / {txt_away or 'Ninguna'}")
            
            if edge_home > 3.0: guardar_prediccion(event_id, f"{a_name} vs {h_name}", h_name, home_line, home_odds, edge_home)
            elif edge_away > 3.0: guardar_prediccion(event_id, f"{a_name} vs {h_name}", a_name, away_line, away_odds, edge_away)
                
    auditar_predicciones()

# ==========================================
# RUTAS WEB
# ==========================================
@app.get("/")
def read_root(): return {"mensaje": "Bot usando 1xBet + Lesiones. Ve a /predictor o /historial"}

@app.get("/predictor", response_class=HTMLResponse)
def predictor_html():
    stats = get_nba_stats()
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games_raw = r.json().get("events", [])
    except Exception:
        return HTMLResponse("<h1>Error cargando partidos</h1>")

    all_odds = get_all_nba_odds()
    html_cards = ""
    
    for ev in games_raw:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2: continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        
        h_name, a_name = home.get("team", {}).get("displayName", ""), away.get("team", {}).get("displayName", "")
        if h_name not in stats or a_name not in stats: continue
            
        h_pf, h_pa, h_id = stats[h_name]['pf'], stats[h_name]['pa'], stats[h_name]['id']
        a_pf, a_pa, a_id = stats[a_name]['pf'], stats[a_name]['pa'], stats[a_name]['id']
        
        pen_home, txt_home = get_injury_penalty(h_id)
        pen_away, txt_away = get_injury_penalty(a_id)
        
        proy_home = (h_pf + a_pa) / 2 + 1.25 + pen_home
        proy_away = (a_pf + h_pa) / 2 - 1.25 + pen_away
        margen = proy_home - proy_away
        
        prob_home = normal_cdf(margen / 11.5) * 100
        prob_away = 100 - prob_home
        favorito = h_name if prob_home > prob_away else a_name
        prob_fav = prob_home if prob_home > prob_away else prob_away
        
        key = f"{get_team_key(a_name)}_vs_{get_team_key(h_name)}"
        cuotas = all_odds.get(key)
        valor_html = ""
        
        if cuotas and cuotas.get("spread"):
            home_line, home_odds = cuotas["spread"]["home_line"], cuotas["spread"]["home_odds"]
            away_line, away_odds = cuotas["spread"]["away_line"], cuotas["spread"]["away_odds"]
            
            prob_home_spread = normal_cdf((margen + home_line) / 11.5) * 100
            prob_away_spread = normal_cdf((-margen + away_line) / 11.5) * 100
            edge_home = prob_home_spread - (100 / home_odds) if home_odds > 1.0 else -100
            edge_away = prob_away_spread - (100 / away_odds) if away_odds > 1.0 else -100
            
            if edge_home > 3.0:
                valor_html = f"""<div class="value-box" style="background: rgba(16, 185, 129, 0.1); border-color: #10b981;"><div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET</div><div class="value-bet">{h_name} {home_line:+.1f} @ {home_odds:.2f}</div><div class="value-edge" style="color: #10b981;">Edge: +{edge_home:.1f}%</div></div>"""
            elif edge_away > 3.0:
                valor_html = f"""<div class="value-box" style="background: rgba(16, 185, 129, 0.1); border-color: #10b981;"><div class="value-title" style="color: #10b981;">💎 VALOR EN 1XBET</div><div class="value-bet">{a_name} {away_line:+.1f} @ {away_odds:.2f}</div><div class="value-edge" style="color: #10b981;">Edge: +{edge_away:.1f}%</div></div>"""
            else:
                valor_html = f"""<div class="value-box" style="background: rgba(148, 163, 184, 0.1); border-color: #94a3b8;"><div class="value-title" style="color: #94a3b8;">ANALIZADO (1xBet)</div><div class="value-bet">{h_name} {home_line:+.1f} ({home_odds:.2f}) | {a_name} {away_line:+.1f} ({away_odds:.2f})</div><div class="value-edge" style="color: #94a3b8;">Sin ventaja clara</div></div>"""
        else:
            valor_html = f"""<div class="value-box" style="background: rgba(148, 163, 184, 0.1); border-color: #94a3b8;"><div class="value-title" style="color: #94a3b8;">SIN CUOTAS</div><div class="value-bet">1xBet no ha abierto el mercado</div></div>"""

        # Mostrar lesiones en la UI
        injury_html = ""
        if txt_home or txt_away:
            injury_html = f"""<div style="font-size:11px; color:#ef4444; text-align:center; margin-top:10px;"><b>Lesiones:</b> {txt_home} {txt_away}</div>"""

        html_cards += f"""
        <div class="card">
            <div class="card-header"><div class="league">🏀 NBA</div><div class="time">Hoy</div></div>
            <div class="matchup">
                <div class="team"><div class="team-name">{a_name}</div><div class="team-prob">{prob_away:.0f}%</div></div>
                <div class="vs"><div class="gauge"><div class="gauge-fill" style="width: {prob_fav}%;"></div></div><div class="fav-text">{favorito}<br>TO WIN</div></div>
                <div class="team"><div class="team-name">{h_name}</div><div class="team-prob">{prob_home:.0f}%</div></div>
            </div>
            <div class="projection"><div class="proj-box"><div class="proj-label">PROYECCIÓN ZCODE</div><div class="proj-score">{proy_away:.0f} - {proy_home:.0f}</div></div></div>
            {valor_html}
            {injury_html}
        </div>
        """

    html_out = f"""<!DOCTYPE html><html><head><title>Zcode Predictor</title><meta name="viewport" content="width=device-width, initial-scale=1"><style>body {{ font-family: -apple-system, sans-serif; background: #0b1120; color: white; padding: 16px; max-width: 500px; margin: auto; }}.card {{ background: #1e293b; border-radius: 16px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}.card-header {{ display: flex; justify-content: space-between; color: #94a3b8; font-size: 12px; font-weight: bold; margin-bottom: 20px; }}.matchup {{ display: flex; justify-content: space-between; align-items: center; text-align: center; }}.team {{ flex: 1; }}.team-name {{ font-size: 14px; font-weight: bold; margin-bottom: 4px; line-height: 1.2; }}.team-prob {{ font-size: 18px; font-weight: bold; color: #eab308; }}.vs {{ flex: 1; padding: 0 10px; }}.gauge {{ width: 100%; height: 8px; background: #334155; border-radius: 4px; overflow: hidden; margin-bottom: 8px; }}.gauge-fill {{ height: 100%; background: #eab308; }}.fav-text {{ font-size: 10px; color: #94a3b8; text-transform: uppercase; line-height: 1.2; }}.projection {{ display: flex; gap: 10px; margin-top: 20px; border-top: 1px solid #334155; padding: 15px 0 0 0; }}.proj-box {{ flex: 1; text-align: center; }}.proj-label {{ font-size: 10px; color: #94a3b8; font-weight: bold; margin-bottom: 4px; }}.proj-score {{ font-size: 20px; font-weight: bold; color: white; }}.value-box {{ margin-top: 15px; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid transparent; }}.value-title {{ font-size: 12px; font-weight: bold; margin-bottom: 4px; }}.value-bet {{ font-size: 15px; font-weight: bold; color: white; }}.value-edge {{ font-size: 12px; font-weight: bold; margin-top: 4px; }}.nav {{ text-align: center; margin-bottom: 20px; }}.nav a {{ color: #eab308; text-decoration: none; font-weight: bold; margin: 0 10px; }}</style></head><body><div class="nav"><a href="/predictor">Predicciones</a> | <a href="/historial">Historial</a></div><h2 style="font-size: 18px; margin-bottom: 20px;">ZCODE AI - Predicciones</h2>{html_cards if html_cards else "<p style='text-align:center;'>No hay partidos programados para hoy.</p>"}</body></html>"""
    return html_out

@app.get("/historial", response_class=HTMLResponse)
def historial_html():
    with db() as con:
        rows = con.execute("SELECT fecha, partido, pick, linea, cuota, edge, status, puntos_home, puntos_away FROM predicciones_zcode ORDER BY id DESC LIMIT 50").fetchall()
        
    html_cards = ""
    won = sum(1 for r in rows if r[6] == 'WON')
    lost = sum(1 for r in rows if r[6] == 'LOST')
    
    for r in rows:
        fecha, partido, pick, linea, cuota, edge, status, ph, pa = r
        if status == 'WON': color = "#10b981"
        elif status == 'LOST': color = "#ef4444"
        else: color = "#eab308"
            
        score_txt = f"{ph} - {pa}" if ph is not None else "-"
        html_cards += f"""<div class="card" style="border-left: 4px solid {color};"><div style="display:flex; justify-content:space-between; margin-bottom:10px;"><span style="font-weight:bold;">{partido}</span><span style="color:{color}; font-weight:bold;">{status}</span></div><div style="display:flex; justify-content:space-between; font-size:14px; margin-bottom:5px;"><span>💡 {pick} {linea:+.1f} @ {cuota:.2f}</span><span>Edge: +{edge:.1f}%</span></div><div style="font-size:12px; color:#94a3b8; text-align:center; margin-top:10px; padding-top:10px; border-top:1px solid #334155;">Marcador Final: {score_txt}</div></div>"""

    html_out = f"""<!DOCTYPE html><html><head><title>Historial Zcode</title><meta name="viewport" content="width=device-width, initial-scale=1"><style>body {{ font-family: -apple-system, sans-serif; background: #0b1120; color: white; padding: 16px; max-width: 500px; margin: auto; }}.card {{ background: #1e293b; border-radius: 16px; padding: 20px; margin-bottom: 15px; border: 1px solid #334155; }}.nav {{ text-align: center; margin-bottom: 20px; }}.nav a {{ color: #eab308; text-decoration: none; font-weight: bold; margin: 0 10px; }}.stats {{ display:flex; gap:10px; margin-bottom:20px; }}.stat {{ flex:1; background:#1e293b; padding:15px; border-radius:10px; text-align:center; border:1px solid #334155; }}.stat-val {{ font-size:24px; font-weight:bold; }}</style></head><body><div class="nav"><a href="/predictor">Predicciones</a> | <a href="/historial">Historial</a></div><h2 style="font-size: 18px; margin-bottom: 20px;">Historial de Apuestas</h2><div class="stats"><div class="stat"><div style="font-size:12px; color:#94a3b8;">GANADAS</div><div class="stat-val" style="color:#10b981;">{won}</div></div><div class="stat"><div style="font-size:12px; color:#94a3b8;">PERDIDAS</div><div class="stat-val" style="color:#ef4444;">{lost}</div></div></div>{html_cards if html_cards else "<p style='text-align:center;'>No hay apuestas registradas aún.</p>"}</body></html>"""
    return html_out

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
