# Coordinación de insumos — backend

App web para que hospitales y refugios marquen qué insumos necesitan y los
voluntarios sepan a dónde llevarlos. Una sola app **Flask + SQLite** que sirve
la página y la API; **Caddy** delante pone HTTPS automático; todo en **Docker**.

## Estructura

```
backend/
├── app.py              API + sirve el front (Flask)
├── requirements.txt    dependencias (Flask, gunicorn)
├── static/index.html   la interfaz (cliente que llama a la API)
├── Dockerfile          imagen; corre como usuario sin privilegios
├── docker-compose.yml  app + Caddy (HTTPS), datos en volumen aislado
├── Caddyfile           HTTPS automático + cabeceras de seguridad
├── .env.example        plantilla de configuración (cópiala a .env)
└── DEPLOY.md           guía paso a paso (servidor propio y hosting gratuito)
```

## Arranque rápido (en tu servidor con Docker)

```bash
cp .env.example .env      # y edita DOMAIN, EMAIL, SECRET_KEY, ADMIN_PASSWORD
docker compose up -d --build
```

Luego abre `https://TU-DOMINIO`.

**Lee `DEPLOY.md`** para los detalles: aislamiento de archivos, cortafuegos,
DuckDNS/HTTPS, respaldos y las opciones de hosting gratuito (Oracle Always Free,
Koyeb, Render) con sus ventajas y límites.

## Probar en local sin Docker

```bash
pip install -r requirements.txt
SECRET_KEY=prueba ADMIN_PASSWORD=admin123 python3 app.py
# abre http://127.0.0.1:8000
```

## Seguridad (resumen)

A diferencia de las versiones que solo corrían en el navegador, aquí la
seguridad es real del lado del servidor: contraseñas con hash PBKDF2 (nunca en
texto plano ni expuestas en la lista pública), edición autorizada por token
firmado, y administración validada en el servidor.
