#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coordinación de insumos — backend (API + sirve el front).

Diseño:
- Una sola app Flask. Sirve el front estático en "/" y la API en "/api/...".
- Base de datos SQLite (sin servidor aparte). Persiste en DB_PATH.
- Contraseñas de centro y de administrador: hash con PBKDF2 (werkzeug). Nunca texto plano.
- Edición autorizada por token firmado (itsdangerous), enviado en "Authorization: Bearer ...".
- Pensado para correr aislado en Docker o en un PaaS gratuito.

Variables de entorno (todas opcionales salvo donde se indica):
  SECRET_KEY       Clave para firmar tokens. Si falta, se genera una y se guarda en la BD.
  DB_PATH          Ruta del archivo SQLite. Por defecto: ./data/insumos.db
  ADMIN_PASSWORD   Si se define, es la contraseña de administrador (recomendado en producción).
                   Si no se define, el primer acceso a "Administración" permite crearla.
  TOKEN_TTL_DAYS   Validez del token de sesión de un centro. Por defecto: 30.
  ALLOW_ORIGIN     Si el front se aloja en otro dominio, ponlo aquí para habilitar CORS.
"""

import os
import re
import json
import time
import sqlite3
import secrets
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "insumos.db"))
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_DAYS", "30")) * 86400
ADMIN_PASSWORD_ENV = os.environ.get("ADMIN_PASSWORD")  # puede ser None
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "")

SUPPLIES = {"agua", "alimentos", "medicamentos", "curacion",
            "abrigo", "higiene", "energia", "combustible"}
ESTADOS = {"suficiente", "bajo", "urgente"}
TIPOS = {"hospital", "refugio"}
MOTIVOS = {"no_existe", "duplicado", "falso", "otro"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

app = Flask(__name__, static_folder=None)

# --------------------------------------------------------------------------- #
# Base de datos
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA busy_timeout=5000;")
        g.db.execute("PRAGMA foreign_keys=ON;")
    return g.db


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS centros (
        id TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        tipo TEXT NOT NULL,
        zona TEXT,
        contacto TEXT,
        estado TEXT NOT NULL,
        necesidades TEXT NOT NULL DEFAULT '[]',
        nota TEXT,
        lat REAL,
        lng REAL,
        codigo TEXT UNIQUE NOT NULL,
        pw_hash TEXT,
        actualizado INTEGER NOT NULL,
        creado INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS verif (
        centro_id TEXT NOT NULL,
        uid TEXT NOT NULL,
        PRIMARY KEY (centro_id, uid),
        FOREIGN KEY (centro_id) REFERENCES centros(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS reportes (
        centro_id TEXT NOT NULL,
        uid TEXT NOT NULL,
        motivo TEXT NOT NULL,
        texto TEXT,
        creado INTEGER NOT NULL,
        PRIMARY KEY (centro_id, uid),
        FOREIGN KEY (centro_id) REFERENCES centros(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    db.commit()
    db.close()


def meta_get(key, default=None):
    row = get_db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key, value):
    db = get_db()
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


# --------------------------------------------------------------------------- #
# Clave secreta y firmadores de token
# --------------------------------------------------------------------------- #
def get_secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    # Sin SECRET_KEY en el entorno: generamos una y la persistimos en la BD,
    # así los tokens siguen siendo válidos entre reinicios.
    with app.app_context():
        existing = meta_get("secret_key")
        if existing:
            return existing
        new = secrets.token_urlsafe(48)
        meta_set("secret_key", new)
        return new


_SECRET = None
def signer(salt):
    global _SECRET
    if _SECRET is None:
        _SECRET = get_secret_key()
    return URLSafeTimedSerializer(_SECRET, salt=salt)


def make_token(salt, payload):
    return signer(salt).dumps(payload)


def read_token(salt, token, max_age):
    try:
        return signer(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def bearer():
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.startswith("Bearer ") else ""


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def clean_str(v, maxlen):
    if v is None:
        return ""
    return str(v).strip()[:maxlen]


def norm_name(s):
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    # quitar acentos básicos
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    return s


def gen_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def unique_code(db):
    for _ in range(20):
        c = gen_code()
        if not db.execute("SELECT 1 FROM centros WHERE codigo=?", (c,)).fetchone():
            return c
    return gen_code() + secrets.choice(CODE_ALPHABET)


def parse_necesidades(v):
    if not isinstance(v, list):
        return []
    return [k for k in v if k in SUPPLIES][:len(SUPPLIES)]


def parse_coord(v, lo, hi):
    try:
        f = float(v)
        if lo <= f <= hi:
            return f
    except (TypeError, ValueError):
        pass
    return None


def centro_public(row, with_verif=True):
    d = {
        "id": row["id"], "nombre": row["nombre"], "tipo": row["tipo"],
        "zona": row["zona"] or "", "contacto": row["contacto"] or "",
        "estado": row["estado"], "necesidades": json.loads(row["necesidades"] or "[]"),
        "nota": row["nota"] or "", "actualizado": row["actualizado"],
        "protegido": bool(row["pw_hash"]),
    }
    if row["lat"] is not None and row["lng"] is not None:
        d["lat"] = row["lat"]; d["lng"] = row["lng"]
    if with_verif:
        c = get_db().execute("SELECT COUNT(*) n FROM verif WHERE centro_id=?", (row["id"],)).fetchone()
        d["verif"] = c["n"]
    return d


# --------------------------------------------------------------------------- #
# Límite de tasa muy simple (disuasivo) en memoria
# --------------------------------------------------------------------------- #
_hits = {}
def rate_limit(bucket, limit, window):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    key = (bucket, ip)
    now = time.time()
    arr = [t for t in _hits.get(key, []) if now - t < window]
    if len(arr) >= limit:
        return False
    arr.append(now)
    _hits[key] = arr
    return True


# --------------------------------------------------------------------------- #
# Cabeceras de seguridad / CORS
# --------------------------------------------------------------------------- #
@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if ALLOW_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = ALLOW_ORIGIN
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


@app.route("/api/<path:_p>", methods=["OPTIONS"])
def cors_preflight(_p):
    return ("", 204)


# --------------------------------------------------------------------------- #
# API: salud
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# API: centros (público)
# --------------------------------------------------------------------------- #
@app.get("/api/centros")
def list_centros():
    rows = get_db().execute("SELECT * FROM centros ORDER BY actualizado DESC").fetchall()
    return jsonify(centros=[centro_public(r) for r in rows])


@app.post("/api/centros")
def register_centro():
    if not rate_limit("register", 8, 600):
        return jsonify(error="rate"), 429
    data = request.get_json(silent=True) or {}
    nombre = clean_str(data.get("nombre"), 80)
    tipo = data.get("tipo") if data.get("tipo") in TIPOS else "hospital"
    estado = data.get("estado") if data.get("estado") in ESTADOS else None
    if not nombre or not estado:
        return jsonify(error="datos"), 400

    db = get_db()
    if not data.get("permitirDuplicado"):
        dup = db.execute("SELECT id, nombre, zona FROM centros").fetchall()
        nn = norm_name(nombre)
        for r in dup:
            if norm_name(r["nombre"]) == nn:
                return jsonify(error="duplicado",
                               match={"id": r["id"], "nombre": r["nombre"], "zona": r["zona"] or ""}), 409

    cid = "c" + secrets.token_hex(8)
    codigo = unique_code(db)
    nec = parse_necesidades(data.get("necesidades")) if estado != "suficiente" else []
    lat = parse_coord(data.get("lat"), -90, 90)
    lng = parse_coord(data.get("lng"), -180, 180)
    pw = data.get("password") or ""
    pw_hash = generate_password_hash(pw) if len(pw) >= 1 else None
    now = int(time.time() * 1000)

    db.execute("""INSERT INTO centros
        (id,nombre,tipo,zona,contacto,estado,necesidades,nota,lat,lng,codigo,pw_hash,actualizado,creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, nombre, tipo, clean_str(data.get("zona"), 60), clean_str(data.get("contacto"), 40),
         estado, json.dumps(nec), clean_str(data.get("nota"), 240),
         lat, lng, codigo, pw_hash, now, now))
    db.commit()

    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    token = make_token("centro", {"cid": cid})
    return jsonify(centro=centro_public(row), code=codigo, token=token), 201


@app.post("/api/centros/login")
def login_centro():
    if not rate_limit("login", 20, 600):
        return jsonify(error="rate"), 429
    data = request.get_json(silent=True) or {}
    code = clean_str(data.get("code"), 12).upper()
    row = get_db().execute("SELECT * FROM centros WHERE codigo=?", (code,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    if row["pw_hash"]:
        pw = data.get("password") or ""
        if not pw:
            return jsonify(error="password_required"), 401
        if not check_password_hash(row["pw_hash"], pw):
            return jsonify(error="password"), 401
    token = make_token("centro", {"cid": row["id"]})
    return jsonify(centro=centro_public(row), token=token)


def auth_centro(cid):
    """Devuelve True si el portador puede editar este centro (token de centro o admin)."""
    tok = bearer()
    payload = read_token("centro", tok, TOKEN_TTL)
    if payload and payload.get("cid") == cid:
        return True
    return is_admin_token(tok)


@app.put("/api/centros/<cid>")
def update_centro(cid):
    db = get_db()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    if not auth_centro(cid):
        return jsonify(error="no_autorizado"), 401

    data = request.get_json(silent=True) or {}
    nombre = clean_str(data.get("nombre", row["nombre"]), 80) or row["nombre"]
    tipo = data.get("tipo") if data.get("tipo") in TIPOS else row["tipo"]
    estado = data.get("estado") if data.get("estado") in ESTADOS else row["estado"]
    nec = parse_necesidades(data.get("necesidades")) if estado != "suficiente" else []
    zona = clean_str(data.get("zona", row["zona"]), 60)
    contacto = clean_str(data.get("contacto", row["contacto"]), 40)
    nota = clean_str(data.get("nota", row["nota"]), 240)

    lat, lng = row["lat"], row["lng"]
    if "lat" in data or "lng" in data:
        lat = parse_coord(data.get("lat"), -90, 90)
        lng = parse_coord(data.get("lng"), -180, 180)
        if lat is None or lng is None:
            lat = lng = None

    pw_hash = row["pw_hash"]
    new_pw = data.get("newPassword")
    if new_pw is not None and len(str(new_pw)) >= 1:
        pw_hash = generate_password_hash(str(new_pw))

    now = int(time.time() * 1000)
    db.execute("""UPDATE centros SET nombre=?,tipo=?,zona=?,contacto=?,estado=?,
        necesidades=?,nota=?,lat=?,lng=?,pw_hash=?,actualizado=? WHERE id=?""",
        (nombre, tipo, zona, contacto, estado, json.dumps(nec), nota, lat, lng, pw_hash, now, cid))
    db.commit()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    return jsonify(centro=centro_public(row))


@app.post("/api/centros/<cid>/verify")
def verify_centro(cid):
    if not rate_limit("verify", 60, 600):
        return jsonify(error="rate"), 429
    db = get_db()
    if not db.execute("SELECT 1 FROM centros WHERE id=?", (cid,)).fetchone():
        return jsonify(error="no_existe"), 404
    data = request.get_json(silent=True) or {}
    uid = clean_str(data.get("uid"), 40)
    if not uid:
        return jsonify(error="uid"), 400
    exists = db.execute("SELECT 1 FROM verif WHERE centro_id=? AND uid=?", (cid, uid)).fetchone()
    if exists:
        db.execute("DELETE FROM verif WHERE centro_id=? AND uid=?", (cid, uid))
        verified = False
    else:
        db.execute("INSERT OR IGNORE INTO verif(centro_id, uid) VALUES(?,?)", (cid, uid))
        verified = True
    db.commit()
    n = db.execute("SELECT COUNT(*) n FROM verif WHERE centro_id=?", (cid,)).fetchone()["n"]
    return jsonify(count=n, verified=verified)


@app.post("/api/centros/<cid>/report")
def report_centro(cid):
    if not rate_limit("report", 30, 600):
        return jsonify(error="rate"), 429
    db = get_db()
    if not db.execute("SELECT 1 FROM centros WHERE id=?", (cid,)).fetchone():
        return jsonify(error="no_existe"), 404
    data = request.get_json(silent=True) or {}
    uid = clean_str(data.get("uid"), 40)
    motivo = data.get("motivo") if data.get("motivo") in MOTIVOS else None
    if not uid or not motivo:
        return jsonify(error="datos"), 400
    texto = clean_str(data.get("texto"), 240)
    now = int(time.time() * 1000)
    # Un reporte por dispositivo y centro; si reporta otra vez, se reemplaza.
    db.execute("INSERT INTO reportes(centro_id, uid, motivo, texto, creado) VALUES(?,?,?,?,?) "
               "ON CONFLICT(centro_id, uid) DO UPDATE SET motivo=excluded.motivo, "
               "texto=excluded.texto, creado=excluded.creado",
               (cid, uid, motivo, texto, now))
    db.commit()
    n = db.execute("SELECT COUNT(*) n FROM reportes WHERE centro_id=?", (cid,)).fetchone()["n"]
    return jsonify(count=n)


# --------------------------------------------------------------------------- #
# API: administración
# --------------------------------------------------------------------------- #
def admin_configured():
    return bool(ADMIN_PASSWORD_ENV) or bool(meta_get("admin_hash"))


def check_admin_password(pw):
    if ADMIN_PASSWORD_ENV:
        return secrets.compare_digest(str(pw), str(ADMIN_PASSWORD_ENV))
    h = meta_get("admin_hash")
    return bool(h) and check_password_hash(h, str(pw))


def is_admin_token(tok):
    payload = read_token("admin", tok, TOKEN_TTL)
    return bool(payload and payload.get("admin"))


def require_admin(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if not is_admin_token(bearer()):
            return jsonify(error="no_autorizado"), 401
        return fn(*a, **kw)
    return inner


@app.get("/api/admin/state")
def admin_state():
    return jsonify(configured=admin_configured(), envManaged=bool(ADMIN_PASSWORD_ENV))


@app.post("/api/admin/setup")
def admin_setup():
    if admin_configured():
        return jsonify(error="ya_configurado"), 409
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password") or "")
    if len(pw) < 4:
        return jsonify(error="corta"), 400
    meta_set("admin_hash", generate_password_hash(pw))
    return jsonify(token=make_token("admin", {"admin": True}))


@app.post("/api/admin/login")
def admin_login():
    if not rate_limit("admin", 10, 600):
        return jsonify(error="rate"), 429
    if not admin_configured():
        return jsonify(error="setup_required"), 409
    data = request.get_json(silent=True) or {}
    if not check_admin_password(data.get("password") or ""):
        return jsonify(error="password"), 401
    return jsonify(token=make_token("admin", {"admin": True}))


@app.get("/api/admin/centros")
@require_admin
def admin_list():
    db = get_db()
    rows = db.execute("SELECT * FROM centros ORDER BY actualizado DESC").fetchall()
    out = []
    for r in rows:
        d = centro_public(r)
        d["creado"] = r["creado"]
        reps = db.execute("SELECT motivo, texto, creado FROM reportes WHERE centro_id=? "
                          "ORDER BY creado DESC", (r["id"],)).fetchall()
        d["reportes"] = len(reps)
        d["reportes_detalle"] = [
            {"motivo": x["motivo"], "texto": x["texto"] or "", "creado": x["creado"]} for x in reps
        ]
        out.append(d)
    # Centros con reportes primero, luego por actualización
    out.sort(key=lambda c: (-(c["reportes"]), -c["actualizado"]))
    return jsonify(centros=out)


@app.delete("/api/admin/centros/<cid>/reportes")
@require_admin
def admin_clear_reports(cid):
    db = get_db()
    db.execute("DELETE FROM reportes WHERE centro_id=?", (cid,))
    db.commit()
    return jsonify(ok=True)


@app.delete("/api/admin/centros/<cid>")
@require_admin
def admin_delete(cid):
    db = get_db()
    db.execute("DELETE FROM verif WHERE centro_id=?", (cid,))
    db.execute("DELETE FROM centros WHERE id=?", (cid,))
    db.commit()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Front estático
# --------------------------------------------------------------------------- #
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:fname>")
def static_files(fname):
    if fname.startswith("api/"):
        return jsonify(error="no_existe"), 404
    full = os.path.join(STATIC_DIR, fname)
    if os.path.isfile(full):
        return send_from_directory(STATIC_DIR, fname)
    # cualquier otra ruta vuelve al front (app de una sola página)
    return send_from_directory(STATIC_DIR, "index.html")


# Inicializa la BD al importar (también bajo gunicorn)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("DEBUG")))
