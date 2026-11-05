import math
import os
import requests
import libsql
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse  # <--- 1. ASEGÚRATE DE TENER ESTO ARRIBA

app = FastAPI()

# ... (Tus variables de Turso y la función db()) ...
# ... (La función init_db()) ...
# ... (La función normal_cdf()) ...
# ... (La función get_nba_stats()) ...

@app.get("/")
def read_root():
    try:
        init_db()
        return {"mensaje": "Conectado a Turso y tablas creadas"}
    except Exception as e:
        return {"error": str(e)}

# =========================================================================
# 2. AQUI ABAJO VA EL CÓDIGO NUEVO (Borra el viejo predictor_json y pega este)
# =========================================================================
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
        
        proy_home = (h_pf + a_pa) / 2 + 1.25
        proy_away = (a_pf + h_pa) / 2 - 1.25
        margen = proy_home - proy_away
        
        prob_home = normal_cdf(margen / 11.5) * 100
        prob_away = 100 - prob_home
        
        favorito = h_name if prob_home > prob_away else a_name
        prob_fav = prob_home if prob_home > prob_away else prob_away
        
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
        </style>
    </head>
    <body>
        <h2 style="font-size: 18px; margin-bottom: 20px;">ZCODE AI - Predicciones</h2>
        {html_cards if html_cards else "<p style='text-align:center;'>No hay partidos programados para hoy.</p>"}
    </body>
    </html>
    """
    return html_out

# =========================================================================
# FIN DEL CODIGO NUEVO
# =========================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
