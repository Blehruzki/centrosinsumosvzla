# Imagen pequeña y estable
FROM python:3.12-slim

# No generar .pyc y salida sin buffer (mejores logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/insumos.db

WORKDIR /app

# Instalar dependencias primero (mejor cacheo de capas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar la aplicación
COPY app.py .
COPY static/ ./static/

# Usuario sin privilegios: el contenedor NO corre como root
RUN useradd --system --uid 10001 --no-create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# La base de datos vive en /data, que se monta como volumen aislado
VOLUME ["/data"]

EXPOSE 8000

# Chequeo de salud: el orquestador sabe si la app está viva
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3).status==200 else 1)"

# 2 procesos x 4 hilos: suficiente para tráfico moderado.
# SQLite en WAL tolera esta concurrencia; para mucha carga, migrar a Postgres.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "4", "--worker-class", "gthread", \
     "--timeout", "60", "--access-logfile", "-", "app:app"]
