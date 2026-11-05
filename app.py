import os
import requests
import libsql
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

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
