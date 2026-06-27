# Guía de despliegue — Coordinación de insumos

Esta guía te lleva de los archivos a un sitio funcionando, con HTTPS y con tus
datos **aislados**. Hay dos caminos; elige uno:

- **Opción A — Tu servidor Ubuntu.** Control total y cero costo de hosting. Lo
  hacemos con Docker para que la app y su base de datos queden encerradas y no
  toquen el resto de tus archivos.
- **Opción B — Un proveedor gratuito.** Para que cualquiera en Venezuela acceda
  sin depender del ancho de banda ni del encendido de tu casa.

No tienes que elegir para siempre: los mismos archivos sirven para ambos.

---

## Qué es esto (en una línea)

Una sola app en **Flask + SQLite** que sirve la página y la API. Delante va
**Caddy**, que pone HTTPS automático y reenvía el tráfico. Todo corre en
**Docker**, así que se levanta con un comando y queda aislado.

```
Internet ──HTTPS──▶ Caddy ──interno──▶ app (Flask) ──▶ SQLite (volumen aislado)
```

---

## Por qué Docker resuelve tu preocupación de "que no revisen mis archivos"

Tu duda era si tu servidor está configurado para que nadie acceda a tus
archivos. Con esta configuración:

1. **La app vive dentro de un contenedor.** Solo ve su propio código y la
   carpeta de datos. **No** ve tu carpeta personal, ni los recursos de Samba,
   ni nada más del disco del host.
2. **La base de datos está en un volumen de Docker** (`insumos-data`), separado
   de tus carpetas. Quien entre al sitio web jamás navega tu sistema de archivos.
3. **El contenedor corre como un usuario sin privilegios** (no root). Si alguien
   lograra abusar de la app, no tendría permisos sobre el host.
4. **Solo se exponen los puertos 80 y 443** (web). Tus servicios privados —
   Samba, WireGuard— siguen donde estaban: en tu red local o tu VPN, nunca
   abiertos a internet.

En resumen: el sitio público y tus archivos personales quedan en compartimentos
separados.

---

# Opción A — Tu servidor Ubuntu (con Docker)

### 1. Instalar Docker (una sola vez)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# cierra sesión y vuelve a entrar para que el grupo tome efecto
```

Comprueba que quedó listo (incluye Docker Compose v2):

```bash
docker --version && docker compose version
```

### 2. Copiar el proyecto al servidor

Pon la carpeta `backend/` en tu servidor (por Samba, `scp`, o `git`). Entra a
ella:

```bash
cd backend
```

### 3. Configurar las variables

```bash
cp .env.example .env
# genera una clave secreta fuerte:
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Edita `.env` con `nano .env` y completa:

- `DOMAIN` → tu subdominio (por ejemplo `insumos-ve.duckdns.org`).
- `EMAIL` → tu correo (para avisos del certificado).
- `SECRET_KEY` → pega la clave que generaste.
- `ADMIN_PASSWORD` → la contraseña con la que entrarás a "Administración".

### 4. Apuntar el dominio y abrir el router

- En **DuckDNS**, deja tu subdominio apuntando a tu IP pública (su actualizador
  ya mantiene la IP al día, como en tu configuración actual).
- En tu **router**, reenvía los puertos **80** y **443** hacia la IP local de tu
  servidor. Esto es imprescindible para que Caddy consiga el certificado HTTPS.

> **Por qué HTTPS no es opcional aquí:** el navegador solo permite usar el GPS
> ("Usar mi ubicación") en sitios seguros (HTTPS). Sin certificado, esa función
> queda desactivada en los teléfonos.

### 5. Levantar el sitio

```bash
docker compose up -d --build
```

La primera vez Caddy tarda unos segundos en obtener el certificado. Luego abre
`https://TU-DOMINIO` y deberías ver la app. Para ver los registros:

```bash
docker compose logs -f
```

### 6. Cerrar el resto con el cortafuegos (UFW)

Esto deja entrar solo lo necesario y mantiene Samba/WireGuard fuera de internet:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp     # SSH (idealmente limítalo a tu IP o úsalo solo por WireGuard)
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Fíjate que **no** abrimos los puertos de Samba (445/139). Así tus archivos
compartidos siguen siendo accesibles solo desde tu red local o por la VPN, no
desde internet.

### 7. Sobre el ancho de banda (lo honesto)

Servir desde casa funciona, pero **tu velocidad de subida es el límite**. Si
muchos hospitales y voluntarios de todo el país entran a la vez, una conexión
doméstica puede saturarse y el sitio se pondrá lento para todos. La app es
ligera (sin mapas externos ni fuentes pesadas), lo que ayuda mucho, pero si
esperas uso a escala nacional, considera la **Opción B**. Una salida intermedia:
empezar en tu servidor y, si crece, mover el mismo proyecto a un proveedor.

### 8. Respaldos

La base de datos está en el volumen `insumos-data`. Para copiarla:

```bash
docker run --rm -v insumos-data:/data -v "$PWD":/backup alpine \
  sh -c "cp -a /data/. /backup/respaldo-$(date +%F)/"
```

Guarda esos respaldos fuera del servidor. Restaurar es copiar de vuelta el
contenido a `/data` con el contenedor detenido.

---

# Opción B — Un proveedor gratuito o de bajo costo

Para que el acceso no dependa de tu casa. Comparación honesta (a 2026):

| Proveedor | Cómo es | Persistencia (clave para SQLite) | Para esto |
|---|---|---|---|
| **Oracle Cloud — Always Free** | VPS real (ARM, 2 vCPU / 12 GB, 200 GB disco, 10 TB/mes de salida). No caduca. | Disco **persistente** de verdad. SQLite funciona sin trucos. | **Recomendado** para algo siempre encendido y que la gente use en serio. |
| **Koyeb (free)** | Despliegue fácil desde Git, sin "dormirse". Hospeda apps dinámicas. | Disco **efímero**: SQLite se borra al redeploy salvo volumen/BD gestionada. | Buena si aceptas migrar a una BD gestionada. |
| **Render (free)** | El más fácil (deploy desde Git, sin tarjeta). | Se **duerme** a los 15 min (arranque ~1 min); disco **efímero**; su Postgres free **caduca a los 30 días**; tope 750 h/mes. | Solo para **demostrar** el proyecto, no para datos que deben quedarse. |
| **Railway / Fly.io** | Por uso/crédito (Railway da ~US$1/mes gratis). | Variable. | No es "gratis" real para tenerlo encendido todo el día. |

### Recomendación para tu caso: Oracle Cloud Always Free

Es la única opción verdaderamente gratuita que da un **VPS real con disco
persistente**, así que tu SQLite sigue funcionando igual que en tu servidor y el
sitio queda encendido 24/7 con buena salida de datos. Una vez creada la máquina,
**los pasos son los mismos de la Opción A** (instalar Docker y `docker compose up`).

Detalles honestos que debes saber:

- Piden **tarjeta** solo para verificar identidad (no cobran si te quedas en el
  nivel gratuito). Conviene poner una **alerta de presupuesto** por si acaso.
- La capacidad ARM gratuita va por regiones y a veces sale "out of capacity";
  hay que **reintentar** crear la instancia o probar otra zona.
- Para **Venezuela**, elige una región cercana (São Paulo/Vinhedo en Brasil, o
  US-East) para mejor latencia.
- En Oracle, además de UFW, debes **abrir los puertos 80 y 443 en la "Security
  List" / NSG de la red virtual (VCN)** desde el panel; si no, no entrará tráfico
  aunque la app esté corriendo.

### Nota importante sobre persistencia

SQLite necesita un **disco que no se borre**. En un VPS (Oracle, o cualquier VPS
de bajo costo de ~US$4/mes) lo tienes y no hay que cambiar nada. En las
plataformas tipo Render/Koyeb el disco es efímero: cada redeploy reinicia la
base. Si insistes en una de esas, hay que **migrar de SQLite a Postgres** (su
base gestionada) — es posible, pero añade trabajo. Por eso, para no complicarte,
la ruta limpia es un VPS y seguir con SQLite.

---

## Primer uso y administración

1. Entra a `https://TU-DOMINIO`. Un centro se registra desde "Soy un centro" y
   recibe un **código** para actualizarse desde otro teléfono.
2. Para administrar, usa el enlace discreto **"Administración"** abajo en el
   inicio. Si definiste `ADMIN_PASSWORD` en `.env`, esa es la contraseña. Desde
   ahí revisas todos los centros y eliminas los que no sean reales.

## Qué cambia en seguridad respecto a la versión anterior

En las versiones que solo corrían en el navegador, las contraseñas y el panel de
administrador eran un **disuasivo**, no seguridad real. Con este backend **ya es
seguridad de verdad**:

- Las contraseñas se guardan **cifradas (hash PBKDF2)** en el servidor y nunca se
  envían al resto de usuarios. La lista pública **no** incluye los hashes.
- Editar un centro exige un **token firmado** por el servidor; sin él, la
  modificación se rechaza (probado: responde 401).
- Borrar centros y administrar solo lo puede hacer quien tenga la contraseña de
  administrador, validada en el servidor.

## Mantenimiento

```bash
docker compose logs -f          # ver registros
docker compose pull && docker compose up -d --build   # actualizar tras cambios
docker compose down             # detener (los datos del volumen se conservan)
```

---

## Alternativa sin Docker (si lo prefieres)

Si no quieres Docker, puedes correr la app directamente, aunque pierdes parte del
aislamiento:

```bash
pip install -r requirements.txt
export SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
export ADMIN_PASSWORD="tu-contraseña"
export DB_PATH="$HOME/insumos-datos/insumos.db"
gunicorn --bind 127.0.0.1:8000 --workers 2 --threads 4 --worker-class gthread app:app
```

Luego pon **Caddy o Nginx** delante para el HTTPS y, con `systemd`, haz que
arranque sola al encender. En este modo, encárgate tú de que `DB_PATH` y los
permisos queden donde solo el sitio pueda leerlos.
