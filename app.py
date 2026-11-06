import math
import os
import requests
import libsql
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

# ==========================================
# VARIABLES DE ENTORNO
# ==========================================
TURSO_URL     = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN")
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY") # <-- Nueva variable

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
            CREATE TABLE IF NOT EXISTS pregame_lines (
                event_id TEXT PRIMARY KEY,
                matchup TEXT,
                line REAL,
                kickoff_time TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS alertas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                matchup TEXT,
                regla INTEGER,
                checkpoint TEXT,
                pregame_line REAL,
                proyeccion REAL,
                target_line REAL,
                decision TEXT,
                live_line_at_alert REAL,
                cuota REAL,
                status TEXT DEFAULT 'PENDING',
                final_total INTEGER,
                created_at TEXT,
                UNIQUE(event_id, regla)
            );
        """)

# ==========================================
# MATEMÁTICA Y ESTADÍSTICAS
# ==========================================
def normal_cdf(x, mu=0, sigma=1):
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

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
                stats = entry.get("stats", [])
                pts_for = 110.0
                pts_against = 110.0
                for s in stats:
                    if s.get("name") == "avgPointsFor":
                        pts_for = float(s.get("value", 110.0))
                    elif s.get("name") == "avgPointsAgainst":
                        pts_against = float(s.get("value", 110.0))
                stats_dict[team_name] = {"pf": pts_for, "pa": pts_against}
    except Exception as e:
        print(f"Error stats: {e}")
    return stats_dict

def get_team_key(name):
    n = name.lower()
    if "hawks" in n: return "atlanta"
    if "celtics" in n: return "boston"
    if "nets" in n: return "brooklyn"
    if "hornets" in n: return "charlotte"
    if "bulls" in n: return "chicago"
    if "cavaliers" in n or "cavs" in n: return "cleveland"
    if "mavericks" in n or "mavs" in n: return "dallas"
    if "nuggets" in n: return "denver"
    if "pistons" in n: return "detroit"
    if "warriors" in n or "warrior" in n or "gsw" in n: return "goldenstate"
    if "rockets" in n: return "houston"
    if "pacers" in n: return "indiana"
    if "clippers" in n or "lac" in n: return "clippers"
    if "lakers" in n or "lal" in n: return "lakers"
    if "grizzlies" in n: return "memphis"
    if "heat" in n: return "miami"
    if "bucks" in n: return "milwaukee"
    if "timberwolves" in n or "wolves" in n or "min" in n: return "minnesota"
    if "pelicans" in n or "nop" in n: return "neworleans"
    if "knicks" in n or "nyk" in n: return "newyork"
    if "thunder" in n or "okc" in n: return "oklahomacity"
    if "magic" in n or "orl" in n: return "orlando"
    if "76ers" in n or "sixers" in n or "phi" in n: return "philadelphia"
    if "suns" in n or "pho" in n: return "phoenix"
    if "blazers" in n or "trail blazers" in n or "por" in n: return "portland"
    if "kings" in n or "sac" in n: return "sacramento"
    if "spurs" in n or "sas" in n: return "sanantonio"
    if "raptors" in n or "tor" in n: return "toronto"
    if "jazz" in n or "uta" in n: return "utah"
    if "wizards" in n or "was" in n: return "washington"
    return n

# ==========================================
# BUSCAR CUOTAS EN THE ODDS API
# ==========================================
def buscar_cuotas_oddsapi(away_name, home_name):
    if not ODDS_API_KEY: return None
    
    # Endpoint de The Odds API para NBA
    url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads,h2h", # Spreads (Hándicap) y H2H (Moneyline)
        "oddsFormat": "decimal"
    }
    
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200: 
            print(f"Error OddsAPI: {r.status_code} - {r.text}")
            return None
            
        games = r.json()
        
        # Buscar el partido que coincida con los nombres
        for game in games:
            h_api = game.get("home_team", "")
            a_api = game.get("away_team", "")
            
            # Comparamos usando las llaves para evitar problemas con nombres completos
            if get_team_key(h_api) == get_team_key(home_name) and get_team_key(a_api) == get_team_key(away_name):
                
                bookmakers = game.get("bookmakers", [])
                if not bookmakers:
                    return None
                
                # Tomamos la primera casa de apuestas disponible (usualmente DraftKings o FanDuel)
                book = bookmakers[0]
                markets = book.get("markets", [])
                
                cuotas = {"spread": None, "moneyline": None}
                
                for market in markets:
                    if market.get("key") == "spreads":
                        outcomes = market.get("outcomes", [])
                        # The Odds API devuelve los outcomes con el nombre del equipo
                        for out in outcomes:
                            if get_team_key(out.get("name", "")) == get_team_key(home_name):
                                cuotas["spread"] = {
                                    "line": float(out.get("point", 0)),
                                    "home_odds": float(out.get("price", 0))
                                }
                            elif get_team_key(out.get("name", "")) == get_team_key(away_name):
                                if cuotas["spread"] is None:
                                    cuotas["spread"] = {}
                                cuotas["spread"]["line"] = float(out.get("point", 0))
                                cuotas["spread"]["away_odds"] = float(out.get("price", 0))
                                
                    elif market.get("key") == "h2h":
                        outcomes = market.get("outcomes", [])
                        for out in outcomes:
                            if get_team_key(out.get("name", "")) == get_team_key(home_name):
                                cuotas["moneyline"] = {"home_odds": float(out.get("price", 0))}
                            elif get_team_key(out.get("name", "")) == get_team_key(away_name):
                                if cuotas["moneyline"] is None:
                                    cuotas["moneyline"] = {}
                                cuotas["moneyline"]["away_odds"] = float(out.get("price", 0))
                
                # Aseguramos que el spread tenga ambas cuotas
                if cuotas["spread"] and "home_odds" in cuotas["spread"] and "away_odds" in cuotas["spread"]:
                    return cuotas
                elif cuotas["moneyline"]:
                    # Si no hay spread pero sí moneyline, lo devolvemos (podríamos adaptarlo)
                    pass
                
                return None # Si el partido existe pero no tiene los mercados completos
                
    except Exception as e:
        print(f"Error cuotas OddsAPI: {e}")
    return None

# ==========================================
# RUTAS
# ==========================================
@app.get("/")
def read_root():
    try:
        init_db()
        return {"mensaje": "Conectado a Turso y tablas creadas"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/predictor", response_class=HTMLResponse)
def predictor_html():
    stats = get_nba_stats()
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games_raw = r.json().get("events", [])
    except Exception:
        return HTMLResponse("<h1>Error cargando partidos</h1>")

    html_cards = ""
    
    for ev in games_raw:
        comp = ev.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2: continue
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        
        h_name = home.get("team", {}).get("displayName", "")
        a_name = away.get("team", {}).get("displayName", "")
        
        if h_name not in stats or a_name not in stats:
            continue
            
        h_pf = stats[h_name]['pf']
        h_pa = stats[h_name]['pa']
        a_pf = stats[a_name]['pf']
        a_pa = stats[a_name]['pa']
        
        # 1. Cálculos del Modelo
        proy_home = (h_pf + a_pa) / 2 + 1.25
        proy_away = (a_pf + h_pa) / 2 - 1.25
        margen = proy_home - proy_away
        
        prob_home = normal_cdf(margen / 11.5) * 100
        prob_away = 100 - prob_home
        
        favorito = h_name if prob_home > prob_away else a_name
        prob_fav = prob_home if prob_home > prob_away else prob_away
        
        # 2. Buscar cuotas reales en The Odds API
        cuotas = buscar_cuotas_oddsapi(a_name, h_name)
        
        # 3. Evaluar si hay Valor (Edge)
        valor_html = ""
        if cuotas and cuotas.get("spread"):
            spread_line = cuotas["spread"].get("line", 0)
            home_odds = cuotas["spread"].get("home_odds", 0)
            away_odds = cuotas["spread"].get("away_odds", 0)
            
            # Probabilidad real de cubrir el spread
            prob_home_spread = normal_cdf((margen - spread_line) / 11.5) * 100
            prob_away_spread = 100 - prob_home_spread
            
            # Calcular Edge (Valor) vs Casa
            edge_home = prob_home_spread - (100 / home_odds) if home_odds > 1.0 else -100
            edge_away = prob_away_spread - (100 / away_odds) if away_odds > 1.0 else -100
            
            if edge_home > 3.0:
                valor_html = f"""
                <div class="value-box" style="background: rgba(16, 185, 129, 0.1); border-color: #10b981;">
                    <div class="value-title" style="color: #10b981;">💎 VALOR DETECTADO</div>
                    <div class="value-bet">{h_name} {spread_line:+.1f} @ {home_odds:.2f}</div>
                    <div class="value-edge" style="color: #10b981;">Edge: +{edge_home:.1f}%</div>
                </div>
                """
            elif edge_away > 3.0:
                valor_html = f"""
                <div class="value-box" style="background: rgba(16, 185, 129, 0.1); border-color: #10b981;">
                    <div class="value-title" style="color: #10b981;">💎 VALOR DETECTADO</div>
                    <div class="value-bet">{a_name} {-spread_line:+.1f} @ {away_odds:.2f}</div>
                    <div class="value-edge" style="color: #10b981;">Edge: +{edge_away:.1f}%</div>
                </div>
                """
            else:
                valor_html = f"""
                <div class="value-box" style="background: rgba(148, 163, 184, 0.1); border-color: #94a3b8;">
                    <div class="value-title" style="color: #94a3b8;">MERCADO ANALIZADO</div>
                    <div class="value-bet">Línea: {spread_line:+.1f} | Cuotas: {home_odds:.2f} / {away_odds:.2f}</div>
                    <div class="value-edge" style="color: #94a3b8;">Sin ventaja clara (Edge: {max(edge_home, edge_away):.1f}%)</div>
                </div>
                """
        else:
            valor_html = f"""
            <div class="value-box" style="background: rgba(148, 163, 184, 0.1); border-color: #94a3b8;">
                <div class="value-title" style="color: #94a3b8;">SIN CUOTAS AÚN</div>
                <div class="value-bet">Esperando que la casa abra el mercado</div>
            </div>
            """

        # 4. Imprimir tarjeta
        html_cards += f"""
        <div class="card">
            <div class="card-header">
                <div class="league">🏀 NBA</div>
                <div class="time">Hoy</div>
            </div>
            
            <div class="matchup">
                <div class="team">
                    <div class="team-name">{a_name}</div>
                    <div class="team-prob">{prob_away:.0f}%</div>
                </div>
                
                <div class="vs">
                    <div class="gauge">
                        <div class="gauge-fill" style="width: {prob_fav}%;"></div>
                    </div>
                    <div class="fav-text">{favorito}<br>TO WIN</div>
                </div>
                
                <div class="team">
                    <div class="team-name">{h_name}</div>
                    <div class="team-prob">{prob_home:.0f}%</div>
                </div>
            </div>
            
            <div class="projection">
                <div class="proj-box">
                    <div class="proj-label">PROYECCIÓN ZCODE</div>
                    <div class="proj-score">{proy_away:.0f} - {proy_home:.0f}</div>
                </div>
            </div>
            
            {valor_html}
        </div>
        """

    html_out = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Zcode Predictor</title>
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
            
            /* Estilos para el cuadro de Valor */
            .value-box {{ margin-top: 15px; border-radius: 8px; padding: 12px; text-align: center; }}
            .value-title {{ font-size: 12px; font-weight: bold; margin-bottom: 4px; }}
            .value-bet {{ font-size: 16px; font-weight: bold; color: white; }}
            .value-edge {{ font-size: 12px; font-weight: bold; margin-top: 4px; }}
        </style>
    </head>
    <body>
        <h2 style="font-size: 18px; margin-bottom: 20px;">ZCODE AI - Predicciones</h2>
        {html_cards if html_cards else "<p style='text-align:center;'>No hay partidos programados para hoy.</p>"}
    </body>
    </html>
    """
    return html_out

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
