import os
import requests
import libsql
import math
from contextlib import contextmanager
from fastapi import FastAPI

app = FastAPI()

# ==========================================
# VARIABLES DE ENTORNO
# ==========================================
TURSO_URL     = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN")

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
# RUTAS
# ==========================================
@app.get("/")
def read_root():
    try:
        init_db()
        return {"mensaje": "Conectado a Turso y tablas creadas"}
    except Exception as e:
        return {"error": str(e)}

# ==========================================
# LÓGICA MATEMÁTICA (PREDICTOR)
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

@app.get("/predictor")
def predictor_json():
    stats = get_nba_stats()
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games_raw = r.json().get("events", [])
    except Exception:
        return {"error": "Error cargando partidos"}

    results = []
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
        
        proy_home = (h_pf + a_pa) / 2 + 1.25
        proy_away = (a_pf + h_pa) / 2 - 1.25
        margen = proy_home - proy_away
        
        prob_home = normal_cdf(margen / 11.5) * 100
        prob_away = 100 - prob_home
        
        results.append({
            "partido": f"{a_name} vs {h_name}",
            "proyeccion": f"{proy_away:.0f} - {proy_home:.0f}",
            "prob_local": f"{prob_home:.1f}%",
            "prob_visita": f"{prob_away:.1f}%"
        })
        
    return results
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
