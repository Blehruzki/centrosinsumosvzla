# API pública — Centros de Insumos VZla

Esta API expone, **solo lectura**, la información de centros para que otras aplicaciones puedan mostrarla y colaborar en una red unificada de información durante la emergencia.

- **Base URL:** `https://refugiosvzla.duckdns.org`.
- **Formato:** JSON (`Content-Type: application/json`).
- **CORS:** los endpoints de esta página están abiertos a **cualquier origen** (`Access-Control-Allow-Origin: *`), por lo que se pueden consumir directamente desde el navegador.
- **Autenticación:** no se requiere para lectura. Los endpoints de administración y de escritura **no** son públicos.
- **Costo / licencia:** el proyecto es de código abierto bajo licencia MIT. El uso de los datos es libre; se agradece atribuir la fuente.

> Aviso de integridad: estos datos son aportados por la comunidad y pueden contener errores o estar desactualizados. Usa siempre el campo `actualizado` para saber qué tan reciente es cada dato, y considera mostrar el conteo de verificaciones (`verif`).

---

## Endpoints

### GET /api/health
Verifica que el servicio está activo.

Respuesta:
```json
{ "ok": true }
```

---

### GET /api/centros
Devuelve **todos los centros** registrados, sin datos sensibles (nunca incluye códigos de acceso ni contraseñas). Es el endpoint principal de la red.

Respuesta:
```json
{
  "centros": [
    {
      "id": "c1a2b3c4d5e6f7a8",
      "nombre": "Hospital Central",
      "tipo": "hospital",
      "zona": "Centro",
      "contacto": "0414-1234567",
      "estado": "urgente",
      "necesidades": ["agua", "medicamentos"],
      "disponibilidad": {},
      "nota": "Prioridad analgésicos",
      "actualizado": 1782600000000,
      "protegido": true,
      "fotos": [],
      "lat": 10.6545,
      "lng": -71.6125,
      "verif": 3
    }
  ]
}
```

#### Campos de un centro

| Campo            | Tipo            | Descripción |
|------------------|-----------------|-------------|
| `id`             | string          | Identificador único del centro. |
| `nombre`         | string          | Nombre del centro. |
| `tipo`           | string          | Uno de: `hospital`, `refugio`, `acopio`. |
| `zona`           | string          | Zona o sector (puede venir vacío). |
| `contacto`       | string          | Teléfono u otro contacto (puede venir vacío). |
| `estado`         | string          | Para `hospital`/`refugio`: `suficiente`, `bajo` o `urgente`. En `acopio` no es significativo. |
| `necesidades`    | string[]        | Insumos que el centro **necesita** (solo `hospital`/`refugio`). Ver catálogo de insumos. |
| `disponibilidad` | objeto          | Solo en `acopio`: insumo → nivel. Ver más abajo. |
| `nota`           | string          | Nota libre (puede venir vacía). |
| `actualizado`    | número          | Marca de tiempo de la última actualización, en **milisegundos desde época Unix (UTC)**. |
| `protegido`      | booleano        | `true` si el centro tiene contraseña (no afecta la lectura). |
| `fotos`          | string[]        | Rutas relativas a las fotos del centro (solo `acopio`). Antepón la Base URL para obtener la imagen. |
| `lat`            | número          | Latitud. **Solo está presente si el centro tiene ubicación.** |
| `lng`            | número          | Longitud. **Solo está presente si el centro tiene ubicación.** |
| `verif`          | número          | Cantidad de voluntarios que verificaron que el centro es real. |

#### Catálogo de insumos (claves)

Las claves usadas en `necesidades` y `disponibilidad` son:

| Clave          | Significado            |
|----------------|------------------------|
| `agua`         | Agua potable           |
| `alimentos`    | Alimentos              |
| `medicamentos` | Medicamentos           |
| `curacion`     | Material de curación   |
| `abrigo`       | Mantas y abrigo        |
| `higiene`      | Higiene                |
| `energia`      | Energía / baterías     |
| `combustible`  | Combustible            |

#### Niveles de `disponibilidad` (centros de acopio)

`disponibilidad` es un objeto que mapea cada insumo a un nivel:

```json
"disponibilidad": { "agua": "mucho", "medicamentos": "poco", "abrigo": "agotado" }
```

| Nivel     | Significado |
|-----------|-------------|
| `mucho`   | Disponible en abundancia. |
| `medio`   | Disponibilidad media. |
| `poco`    | Queda poco. |
| `agotado` | Sin existencias por ahora (no cuenta como disponible). |

Para "quién tiene un insumo X disponible", filtra los `acopio` cuyo `disponibilidad[X]` exista y sea distinto de `agotado`.

---

### GET /api/fotos/{archivo}
Devuelve una imagen (JPEG/PNG/WebP) de un centro de acopio. Los nombres de archivo provienen del arreglo `fotos` de cada centro.

Ejemplo: `GET /api/fotos/c1a2b3c4d5e6f7a8-3cc34b04.jpg`

---

### GET /api/ayuda
Devuelve el contenido de la sección de ayuda (editable por el administrador).

Respuesta:
```json
{
  "ayuda": {
    "intro": "Texto introductorio…",
    "centro": ["Paso 1…", "Paso 2…"],
    "voluntario": ["Paso 1…", "Paso 2…"],
    "faqs": [ { "q": "Pregunta", "a": "Respuesta" } ]
  }
}
```

---

### GET /api/banner
Devuelve el banner informativo actual.

Respuesta:
```json
{
  "banner": {
    "activo": true,
    "texto": "Mensaje del aviso",
    "tipo": "info",
    "cerrable": true,
    "version": 3
  }
}
```

| Campo      | Tipo     | Descripción |
|------------|----------|-------------|
| `activo`   | booleano | Si el banner debe mostrarse. |
| `texto`    | string   | Mensaje. |
| `tipo`     | string   | `info`, `advertencia` o `urgente`. |
| `cerrable` | booleano | Si el usuario puede replegarlo. |
| `version`  | número   | Sube cuando cambia el mensaje o el tipo. |

---

### GET /api/active
Devuelve cuántas personas están viendo la página en este momento (actividad en los últimos ~45 segundos).

Respuesta:
```json
{ "activos": 12 }
```

---

## Ejemplos de consumo

### JavaScript (navegador)
```javascript
const res = await fetch("https://refugiosvzla.duckdns.org/api/centros");
const { centros } = await res.json();

// Centros que necesitan agua (hospitales/refugios):
const necesitanAgua = centros.filter(c =>
  c.tipo !== "acopio" && c.estado !== "suficiente" && c.necesidades.includes("agua"));

// Centros de acopio que TIENEN agua disponible:
const tienenAgua = centros.filter(c =>
  c.tipo === "acopio" && c.disponibilidad.agua && c.disponibilidad.agua !== "agotado");
```

### cURL
```bash
curl https://refugiosvzla.duckdns.org/api/centros
```

### Python
```python
import requests
centros = requests.get("https://refugiosvzla.duckdns.org/api/centros").json()["centros"]
```

---

## Notas de uso responsable

- **Frescura:** ordena o filtra por `actualizado` para priorizar información reciente.
- **Cacheo:** evita pedir `/api/centros` con demasiada frecuencia; unas pocas veces por minuto es suficiente. Hay límites de tasa por IP.
- **Estabilidad:** estos campos son los actuales. Si en el futuro se versiona la API (por ejemplo `/api/v1/...`), se anunciará para no romper integraciones.
- **Atribución:** se agradece enlazar a la fuente cuando muestres estos datos.

Contacto: danielquintanasanz@gmail.com
