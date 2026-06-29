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
import base64
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
FOTOS_DIR = os.environ.get("FOTOS_DIR", os.path.join(os.path.dirname(DB_PATH), "fotos"))
FOTO_MAX_BYTES = 700 * 1024   # tope tras el encogido en el teléfono (~0.7 MB)
FOTO_MAX_N = 3                 # máximo de fotos por centro de acopio
FOTO_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_DAYS", "30")) * 86400
ADMIN_PASSWORD_ENV = os.environ.get("ADMIN_PASSWORD")  # puede ser None
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "")

SUPPLIES = {"agua", "alimentos", "medicamentos", "curacion",
            "abrigo", "higiene", "energia", "combustible", "materiales"}
ESTADOS = {"suficiente", "bajo", "urgente"}
TIPOS = {"hospital", "refugio", "acopio", "particular"}
NIVELES = {"mucho", "medio", "poco", "agotado"}
MOTIVOS = {"no_existe", "duplicado", "falso", "otro"}

# Contenido por defecto de la sección de Ayuda (editable desde el panel de admin).
DEFAULT_AYUDA = {
    "intro": ("Conecta a quien necesita insumos con quien los tiene durante la emergencia. "
              "No requiere instalar nada ni crear cuenta, y funciona desde el teléfono aunque la señal sea baja."),
    "centro": [
        "Toca \u201cSoy un centro\u201d y elige Hospital, Refugio o Centro de acopio.",
        "Hospital o refugio: marca tu estado (Suficiente, Bajo o Urgente) y qué insumos necesitas. "
        "Centro de acopio: marca qué insumos tienes disponibles y en qué nivel.",
        "Opcional pero recomendado: agrega tu ubicación, un teléfono de contacto y una contraseña "
        "para que solo tú puedas modificar tu centro.",
        "Guarda. Recibirás un código para volver a actualizar tu centro desde otro teléfono.",
    ],
    "voluntario": [
        "Toca \u201cSoy voluntario\u201d para ver el mapa y la lista de centros.",
        "Usa la barra de búsqueda para encontrar un centro por nombre o un insumo por palabra "
        "(por ejemplo \u201cjeringas\u201d).",
        "Refina con los filtros: por tipo (hospitales, refugios, acopios) o por insumo.",
        "Usa \u201cCómo llegar\u201d para la ruta, y ayuda a la comunidad verificando o reportando centros.",
        "En un centro de acopio, toca \u201cCoordinar entrega\u201d para contactarlo y acordar el envío.",
    ],
    "faqs": [
        {"q": "¿Necesito instalar algo o crear una cuenta?",
         "a": "No. Funciona directamente en el navegador del teléfono o la computadora."},
        {"q": "¿Tiene costo?", "a": "No, el uso de la aplicación es gratuito."},
        {"q": "Olvidé mi código o mi contraseña. ¿Qué hago?",
         "a": "Escríbele al administrador usando el enlace de Contacto al final de la página. "
              "Puede recuperarte el código o restablecer tu contraseña."},
        {"q": "¿Quién puede ver lo que publico?",
         "a": "Todos los voluntarios que usan la app. Por eso no debes incluir datos personales sensibles."},
        {"q": "¿Qué fotos puedo subir a un acopio?",
         "a": "Solo fotos de los insumos. Evita rostros, documentos o direcciones visibles, por privacidad "
              "y seguridad. Se permiten hasta 3 fotos."},
        {"q": "¿Cómo coordino una entrega con un centro de acopio?",
         "a": "Abre su tarjeta y toca \u201cCoordinar entrega\u201d. Podrás llamar o escribir por WhatsApp, "
              "y abrir Yummy si necesitas un envío."},
        {"q": "Vi un centro falso o duplicado. ¿Cómo lo reporto?",
         "a": "En la tarjeta del centro toca \u201cReportar este centro\u201d, elige el motivo y, si quieres, "
              "agrega un comentario. El administrador lo revisará."},
        {"q": "¿Funciona con mala señal?",
         "a": "Sí. La app está diseñada para consumir pocos datos y cargar incluso con conexión limitada."},
    ],
}
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
        necesita INTEGER NOT NULL DEFAULT 0,
        ofrece INTEGER NOT NULL DEFAULT 0,
        nota TEXT,
        nota_necesita TEXT,
        nota_ofrece TEXT,
        fotos_necesita TEXT NOT NULL DEFAULT '[]',
        fotos_ofrece TEXT NOT NULL DEFAULT '[]',
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
    CREATE TABLE IF NOT EXISTS presencia (
        uid TEXT PRIMARY KEY,
        visto INTEGER NOT NULL
    );
    """)
    # Migración: añadir columnas nuevas si la base es de una versión anterior
    cols = [r[1] for r in db.execute("PRAGMA table_info(centros)").fetchall()]
    if "disponibilidad" not in cols:
        db.execute("ALTER TABLE centros ADD COLUMN disponibilidad TEXT NOT NULL DEFAULT '{}'")
    if "foto" not in cols:
        db.execute("ALTER TABLE centros ADD COLUMN foto TEXT")
    if "fotos" not in cols:
        db.execute("ALTER TABLE centros ADD COLUMN fotos TEXT NOT NULL DEFAULT '[]'")
        # Pasar la foto única anterior (si existía) a la nueva lista
        for r in db.execute("SELECT id, foto FROM centros WHERE foto IS NOT NULL AND foto<>''").fetchall():
            db.execute("UPDATE centros SET fotos=? WHERE id=?", (json.dumps([r[1]]), r[0]))
    if "necesita" not in cols:
        db.execute("ALTER TABLE centros ADD COLUMN necesita INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE centros ADD COLUMN ofrece INTEGER NOT NULL DEFAULT 0")
        # Rol según el tipo previo: hospitales/refugios necesitaban; acopios/particulares ofrecían
        db.execute("UPDATE centros SET necesita=1 WHERE tipo IN ('hospital','refugio')")
        db.execute("UPDATE centros SET ofrece=1 WHERE tipo IN ('acopio','particular')")
    if "nota_necesita" not in cols:
        db.execute("ALTER TABLE centros ADD COLUMN nota_necesita TEXT")
        db.execute("ALTER TABLE centros ADD COLUMN nota_ofrece TEXT")
        db.execute("ALTER TABLE centros ADD COLUMN fotos_necesita TEXT NOT NULL DEFAULT '[]'")
        db.execute("ALTER TABLE centros ADD COLUMN fotos_ofrece TEXT NOT NULL DEFAULT '[]'")
        # Repartir la nota y las fotos actuales al rol que corresponda
        for r in db.execute("SELECT id, nota, fotos, necesita, ofrece FROM centros").fetchall():
            rid, nota, fotos, nec, ofr = r[0], (r[1] or ""), (r[2] or "[]"), r[3], r[4]
            if ofr and not nec:
                db.execute("UPDATE centros SET nota_ofrece=?, fotos_ofrece=? WHERE id=?", (nota, fotos, rid))
            else:   # necesita, o ambos, o ninguno: por defecto al lado de necesita
                db.execute("UPDATE centros SET nota_necesita=?, fotos_necesita=? WHERE id=?", (nota, fotos, rid))
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


def parse_disponibilidad(v):
    # Espera un objeto {insumo: nivel}; valida claves y niveles.
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, lvl in v.items():
        if k in SUPPLIES and lvl in NIVELES:
            out[k] = lvl
    return out


def parse_coord(v, lo, hi):
    try:
        f = float(v)
        if lo <= f <= hi:
            return f
    except (TypeError, ValueError):
        pass
    return None


def _rowget(row, key, default=None):
    return row[key] if key in row.keys() else default


def centro_public(row, with_verif=True, verif_count=None):
    d = {
        "id": row["id"], "nombre": row["nombre"], "tipo": row["tipo"],
        "zona": row["zona"] or "", "contacto": row["contacto"] or "",
        "estado": row["estado"], "necesidades": json.loads(row["necesidades"] or "[]"),
        "disponibilidad": json.loads((row["disponibilidad"] if "disponibilidad" in row.keys() else "{}") or "{}"),
        "necesita": bool(row["necesita"]) if "necesita" in row.keys() else (row["tipo"] in ("hospital", "refugio")),
        "ofrece": bool(row["ofrece"]) if "ofrece" in row.keys() else (row["tipo"] in ("acopio", "particular")),
        "notaNecesita": _rowget(row, "nota_necesita") or "",
        "notaOfrece": _rowget(row, "nota_ofrece") or "",
        "actualizado": row["actualizado"],
        "protegido": bool(row["pw_hash"]),
    }
    # Nota combinada (compatibilidad con la API pública anterior)
    d["nota"] = d["notaNecesita"] or d["notaOfrece"] or (_rowget(row, "nota") or "")
    v = str(row["actualizado"])
    def urls(raw):
        try: lst = json.loads(raw or "[]")
        except Exception: lst = []
        return ["/api/fotos/" + f + "?v=" + v for f in lst if f]
    d["fotosNecesita"] = urls(_rowget(row, "fotos_necesita", "[]"))
    d["fotosOfrece"] = urls(_rowget(row, "fotos_ofrece", "[]"))
    # Lista combinada (compatibilidad)
    d["fotos"] = (d["fotosNecesita"] + d["fotosOfrece"]) or urls(_rowget(row, "fotos", "[]"))
    if row["lat"] is not None and row["lng"] is not None:
        d["lat"] = row["lat"]; d["lng"] = row["lng"]
    if with_verif:
        if verif_count is not None:
            d["verif"] = verif_count
        else:
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
        _hits[key] = arr
        return False
    arr.append(now)
    _hits[key] = arr
    # Poda ocasional: elimina cubetas ya vencidas para que el dict no crezca sin fin.
    if len(_hits) > 512:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            _hits.pop(k, None)
    return True


# --------------------------------------------------------------------------- #
# Cabeceras de seguridad / CORS
# --------------------------------------------------------------------------- #
# Rutas públicas que cualquier app puede consumir desde cualquier origen (solo lectura).
PUBLIC_CORS_PATHS = {"/api/health", "/api/centros", "/api/ayuda", "/api/banner", "/api/active"}

def _es_cors_publico(path, method):
    if path.startswith("/api/fotos/"):
        return True
    if path in PUBLIC_CORS_PATHS:
        return True
    if path == "/api/ping":   # latido del contador de visitas
        return True
    return False


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if _es_cors_publico(request.path, request.method):
        # Lectura abierta: cualquier app/origen puede consumir estos datos.
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
    elif ALLOW_ORIGIN:
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


# Contador de personas viendo la página ahora mismo.
PRESENCIA_VENTANA_MS = 45 * 1000   # se considera "activo" si dio señal en los últimos 45 s

def contar_activos(db):
    corte = int(time.time() * 1000) - PRESENCIA_VENTANA_MS
    row = db.execute("SELECT COUNT(*) n FROM presencia WHERE visto > ?", (corte,)).fetchone()
    return row["n"] if row else 0


@app.post("/api/ping")
def ping():
    if not rate_limit("ping", 120, 600):
        return jsonify(error="rate"), 429
    data = request.get_json(silent=True) or {}
    uid = clean_str(data.get("uid"), 40)
    db = get_db()
    now = int(time.time() * 1000)
    if uid:
        db.execute("INSERT INTO presencia(uid, visto) VALUES(?,?) "
                   "ON CONFLICT(uid) DO UPDATE SET visto=excluded.visto", (uid, now))
        # Limpieza ocasional de registros viejos para que la tabla no crezca
        if now % 17 == 0:
            db.execute("DELETE FROM presencia WHERE visto < ?", (now - 10 * 60 * 1000,))
        db.commit()
    return jsonify(activos=contar_activos(db))


@app.get("/api/active")
def active():
    return jsonify(activos=contar_activos(get_db()))


def get_ayuda():
    raw = meta_get("ayuda")
    if not raw:
        return DEFAULT_AYUDA
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else DEFAULT_AYUDA
    except Exception:
        return DEFAULT_AYUDA


def sanitize_ayuda(data):
    """Limpia y acota el contenido de ayuda enviado por el admin."""
    if not isinstance(data, dict):
        data = {}
    intro = clean_str(data.get("intro"), 1200) or ""
    def lista_pasos(v):
        out = []
        if isinstance(v, list):
            for s in v[:20]:
                s = clean_str(s, 400)
                if s:
                    out.append(s)
        return out
    centro = lista_pasos(data.get("centro"))
    voluntario = lista_pasos(data.get("voluntario"))
    faqs = []
    if isinstance(data.get("faqs"), list):
        for item in data["faqs"][:40]:
            if not isinstance(item, dict):
                continue
            q = clean_str(item.get("q"), 240)
            a = clean_str(item.get("a"), 1500)
            if q and a:
                faqs.append({"q": q, "a": a})
    return {"intro": intro, "centro": centro, "voluntario": voluntario, "faqs": faqs}


@app.get("/api/ayuda")
def api_ayuda():
    return jsonify(ayuda=get_ayuda())


# Banner informativo (editable desde el panel de admin).
DEFAULT_BANNER = {"activo": False, "texto": "", "tipo": "info", "cerrable": True, "version": 0}
BANNER_TIPOS = {"info", "advertencia", "urgente"}


def get_banner():
    raw = meta_get("banner")
    if not raw:
        return dict(DEFAULT_BANNER)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else dict(DEFAULT_BANNER)
    except Exception:
        return dict(DEFAULT_BANNER)


def sanitize_banner(data, anterior):
    if not isinstance(data, dict):
        data = {}
    texto = clean_str(data.get("texto"), 280)
    tipo = data.get("tipo") if data.get("tipo") in BANNER_TIPOS else "info"
    activo = bool(data.get("activo")) and bool(texto)
    cerrable = bool(data.get("cerrable"))
    # La "versión" sube cuando cambia el mensaje o el tipo, para que reaparezca
    # aunque el usuario lo haya cerrado antes.
    prev = anterior or {}
    version = int(prev.get("version", 0) or 0)
    if texto != prev.get("texto", "") or tipo != prev.get("tipo", "info"):
        version += 1
    return {"activo": activo, "texto": texto, "tipo": tipo, "cerrable": cerrable, "version": version}


@app.get("/api/banner")
def api_banner():
    return jsonify(banner=get_banner())


# --------------------------------------------------------------------------- #
# API: centros (público)
# --------------------------------------------------------------------------- #
@app.get("/api/centros")
def list_centros():
    db = get_db()
    rows = db.execute("SELECT * FROM centros ORDER BY actualizado DESC").fetchall()
    # Conteo de verificaciones de todos los centros en una sola consulta (evita N+1)
    counts = {r["centro_id"]: r["n"] for r in
              db.execute("SELECT centro_id, COUNT(*) n FROM verif GROUP BY centro_id").fetchall()}
    return jsonify(centros=[centro_public(r, verif_count=counts.get(r["id"], 0)) for r in rows])


@app.post("/api/centros")
def register_centro():
    if not rate_limit("register", 8, 600):
        return jsonify(error="rate"), 429
    data = request.get_json(silent=True) or {}
    nombre = clean_str(data.get("nombre"), 80)
    tipo = data.get("tipo") if data.get("tipo") in TIPOS else "hospital"
    # Roles independientes del tipo: un centro puede necesitar, ofrecer, o ambos.
    necesita = bool(data.get("necesita"))
    ofrece = bool(data.get("ofrece"))
    if not nombre:
        return jsonify(error="datos"), 400
    if not (necesita or ofrece):
        return jsonify(error="rol"), 400   # al menos uno de los dos roles

    if necesita:
        estado = data.get("estado") if data.get("estado") in ESTADOS else None
        if not estado:
            return jsonify(error="datos"), 400
    else:
        estado = "suficiente"   # relleno; no se usa si no necesita

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
    nec = (parse_necesidades(data.get("necesidades")) if (necesita and estado != "suficiente") else []) if necesita else []
    disp = parse_disponibilidad(data.get("disponibilidad")) if ofrece else {}
    nota_nec = clean_str(data.get("notaNecesita"), 240) if necesita else ""
    nota_ofr = clean_str(data.get("notaOfrece"), 240) if ofrece else ""
    lat = parse_coord(data.get("lat"), -90, 90)
    lng = parse_coord(data.get("lng"), -180, 180)
    pw = data.get("password") or ""
    pw_hash = generate_password_hash(pw) if len(pw) >= 1 else None
    now = int(time.time() * 1000)

    db.execute("""INSERT INTO centros
        (id,nombre,tipo,zona,contacto,estado,necesidades,disponibilidad,necesita,ofrece,
         nota,nota_necesita,nota_ofrece,fotos_necesita,fotos_ofrece,lat,lng,codigo,pw_hash,actualizado,creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, nombre, tipo, clean_str(data.get("zona"), 60), clean_str(data.get("contacto"), 40),
         estado, json.dumps(nec), json.dumps(disp), 1 if necesita else 0, 1 if ofrece else 0,
         "", nota_nec, nota_ofr, "[]", "[]", lat, lng, codigo, pw_hash, now, now))
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
    # Roles: si vienen en la petición se usan; si no, se conservan los actuales
    row_nec = bool(row["necesita"]) if "necesita" in row.keys() else (row["tipo"] in ("hospital", "refugio"))
    row_ofr = bool(row["ofrece"]) if "ofrece" in row.keys() else (row["tipo"] in ("acopio", "particular"))
    necesita = bool(data.get("necesita")) if "necesita" in data else row_nec
    ofrece = bool(data.get("ofrece")) if "ofrece" in data else row_ofr
    if not (necesita or ofrece):
        return jsonify(error="rol"), 400

    if necesita:
        estado = data.get("estado") if data.get("estado") in ESTADOS else (row["estado"] if row["estado"] in ESTADOS else None)
        if not estado:
            return jsonify(error="datos"), 400
    else:
        estado = "suficiente"
    nec = (parse_necesidades(data.get("necesidades")) if "necesidades" in data else json.loads(row["necesidades"] or "[]")) if (necesita and estado != "suficiente") else []

    # disponibilidad: si viene en la petición se reemplaza; si no, se conserva la actual
    if "disponibilidad" in data:
        disp = parse_disponibilidad(data.get("disponibilidad")) if ofrece else {}
    else:
        try:
            disp = json.loads((row["disponibilidad"] if "disponibilidad" in row.keys() else "{}") or "{}")
        except Exception:
            disp = {}
        if not ofrece:
            disp = {}
    zona = clean_str(data.get("zona", row["zona"]), 60)
    contacto = clean_str(data.get("contacto", row["contacto"]), 40)
    # Notas por rol: si vienen se usan; si no, se conservan. Se vacía la del rol desactivado.
    nota_nec = clean_str(data.get("notaNecesita"), 240) if "notaNecesita" in data else (_rowget(row, "nota_necesita") or "")
    nota_ofr = clean_str(data.get("notaOfrece"), 240) if "notaOfrece" in data else (_rowget(row, "nota_ofrece") or "")
    if not necesita: nota_nec = ""
    if not ofrece: nota_ofr = ""
    # Fotos por rol: se conservan; se vacían las del rol desactivado (los archivos se limpian aparte si hace falta)
    fotos_nec = _rowget(row, "fotos_necesita", "[]") if necesita else "[]"
    fotos_ofr = _rowget(row, "fotos_ofrece", "[]") if ofrece else "[]"

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
        necesidades=?,disponibilidad=?,necesita=?,ofrece=?,nota_necesita=?,nota_ofrece=?,
        fotos_necesita=?,fotos_ofrece=?,lat=?,lng=?,pw_hash=?,actualizado=? WHERE id=?""",
        (nombre, tipo, zona, contacto, estado, json.dumps(nec), json.dumps(disp),
         1 if necesita else 0, 1 if ofrece else 0, nota_nec, nota_ofr,
         fotos_nec, fotos_ofr, lat, lng, pw_hash, now, cid))
    db.commit()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    return jsonify(centro=centro_public(row))


@app.post("/api/centros/<cid>/foto")
def upload_foto(cid):
    if not rate_limit("foto", 20, 600):
        return jsonify(error="rate"), 429
    db = get_db()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    if not auth_centro(cid):
        return jsonify(error="no_autorizado"), 401

    data = request.get_json(silent=True) or {}
    rol = data.get("rol") if data.get("rol") in ("necesita", "ofrece") else "necesita"
    col = "fotos_necesita" if rol == "necesita" else "fotos_ofrece"
    try:
        fotos = json.loads((_rowget(row, col, "[]")) or "[]")
    except Exception:
        fotos = []
    if len(fotos) >= FOTO_MAX_N:
        return jsonify(error="limite", max=FOTO_MAX_N), 409

    raw = data.get("imagen") or ""
    mime = data.get("mime")
    if isinstance(raw, str) and raw.startswith("data:"):
        try:
            head, raw = raw.split(",", 1)
            mime = head.split(";")[0].split(":")[1].strip().lower()
        except (ValueError, IndexError):
            return jsonify(error="formato"), 400
    if mime not in FOTO_MIMES:
        return jsonify(error="tipo_no_permitido"), 400
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:
        return jsonify(error="formato"), 400
    if not blob or len(blob) > FOTO_MAX_BYTES:
        return jsonify(error="tamano"), 413

    os.makedirs(FOTOS_DIR, exist_ok=True)
    fname = cid + "-" + secrets.token_hex(4) + FOTO_MIMES[mime]
    with open(os.path.join(FOTOS_DIR, fname), "wb") as f:
        f.write(blob)
    fotos.append(fname)
    now = int(time.time() * 1000)
    db.execute("UPDATE centros SET " + col + "=?, actualizado=? WHERE id=?", (json.dumps(fotos), now, cid))
    db.commit()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    return jsonify(centro=centro_public(row))


@app.delete("/api/centros/<cid>/foto/<fname>")
def delete_foto(cid, fname):
    db = get_db()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    if not auth_centro(cid):
        return jsonify(error="no_autorizado"), 401
    # Buscar la foto en cualquiera de los dos rol-buckets
    target_col = None
    for col in ("fotos_necesita", "fotos_ofrece"):
        try:
            lst = json.loads((_rowget(row, col, "[]")) or "[]")
        except Exception:
            lst = []
        if fname in lst:
            target_col = col
            fotos = [f for f in lst if f != fname]
            break
    if target_col is None:
        return jsonify(error="no_existe"), 404
    p = os.path.join(FOTOS_DIR, fname)
    if os.path.exists(p):
        try: os.remove(p)
        except OSError: pass
    now = int(time.time() * 1000)
    db.execute("UPDATE centros SET " + target_col + "=?, actualizado=? WHERE id=?", (json.dumps(fotos), now, cid))
    db.commit()
    row = db.execute("SELECT * FROM centros WHERE id=?", (cid,)).fetchone()
    return jsonify(centro=centro_public(row))


@app.get("/api/fotos/<fname>")
def serve_foto(fname):
    # Solo nombres seguros: <id>.<ext>, sin rutas
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(jpg|png|webp)", fname or ""):
        return jsonify(error="no_existe"), 404
    if not os.path.exists(os.path.join(FOTOS_DIR, fname)):
        return jsonify(error="no_existe"), 404
    resp = send_from_directory(FOTOS_DIR, fname, max_age=86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


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


@app.put("/api/admin/ayuda")
@require_admin
def admin_save_ayuda():
    data = request.get_json(silent=True) or {}
    contenido = sanitize_ayuda(data.get("ayuda") if "ayuda" in data else data)
    meta_set("ayuda", json.dumps(contenido, ensure_ascii=False))
    return jsonify(ayuda=contenido)


@app.delete("/api/admin/ayuda")
@require_admin
def admin_reset_ayuda():
    db = get_db()
    db.execute("DELETE FROM meta WHERE key=?", ("ayuda",))
    db.commit()
    return jsonify(ayuda=DEFAULT_AYUDA)


@app.put("/api/admin/banner")
@require_admin
def admin_save_banner():
    data = request.get_json(silent=True) or {}
    contenido = sanitize_banner(data.get("banner") if "banner" in data else data, get_banner())
    meta_set("banner", json.dumps(contenido, ensure_ascii=False))
    return jsonify(banner=contenido)


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


@app.get("/api/admin/centros/<cid>/codigo")
@require_admin
def admin_get_code(cid):
    row = get_db().execute("SELECT codigo FROM centros WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    return jsonify(codigo=row["codigo"])


@app.post("/api/admin/centros/<cid>/codigo")
@require_admin
def admin_regen_code(cid):
    db = get_db()
    row = db.execute("SELECT 1 FROM centros WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    nuevo = unique_code(db)
    db.execute("UPDATE centros SET codigo=? WHERE id=?", (nuevo, cid))
    db.commit()
    return jsonify(codigo=nuevo)


@app.post("/api/admin/centros/<cid>/password")
@require_admin
def admin_set_password(cid):
    db = get_db()
    row = db.execute("SELECT 1 FROM centros WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="no_existe"), 404
    data = request.get_json(silent=True) or {}
    pw = data.get("password")
    # password vacío o nulo => quitar la contraseña; si trae texto => fijar nueva
    if pw is None or str(pw) == "":
        db.execute("UPDATE centros SET pw_hash=NULL WHERE id=?", (cid,))
        protegido = False
    else:
        db.execute("UPDATE centros SET pw_hash=? WHERE id=?", (generate_password_hash(str(pw)), cid))
        protegido = True
    db.commit()
    return jsonify(protegido=protegido)


@app.delete("/api/admin/centros/<cid>")
@require_admin
def admin_delete(cid):
    db = get_db()
    fr = db.execute("SELECT fotos FROM centros WHERE id=?", (cid,)).fetchone()
    if fr and ("fotos" in fr.keys()) and fr["fotos"]:
        try:
            for f in json.loads(fr["fotos"] or "[]"):
                p = os.path.join(FOTOS_DIR, f)
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass
        except Exception:
            pass
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
