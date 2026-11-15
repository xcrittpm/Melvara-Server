# Sonora Server

Índice de búsqueda para la app **Sonora** (reproductor de música para Plex en iPhone).

En vez de que cada iPhone descargue y guarde el índice de tu biblioteca (con bibliotecas grandes,
cientos de MB y varios minutos la primera vez), Sonora Server lo mantiene en tu servidor y la app
le pregunta directamente. Es opcional: en la app eliges **Ajustes → Buscador → En este iPhone** o
**Servidor Sonora**.

## Seguridad

- **Solo responde a cuentas de Plex con acceso a tu servidor.** Cada petición lleva el token de
  Plex del usuario; Sonora Server pregunta a tu propio Plex qué bibliotecas puede ver ese token.
  Si Plex lo rechaza, la respuesta es «401 no autorizado». Tener la URL no basta.
- **Cada usuario solo ve las secciones de música que tiene compartidas** en Plex.
- **No se devuelven datos personales** (reproducciones, estrellas): el índice se crea con la cuenta
  del dueño, y esos datos los pide la app a Plex con la cuenta de cada usuario.
- Los tokens nunca se guardan en disco ni aparecen en los registros (solo un resumen en memoria).
- Límite de peticiones por minuto, contenedor sin permisos de root, solo lectura y sin capacidades.
- Úsalo siempre detrás de **HTTPS** (tu proxy inverso).

> No es posible garantizar que una petición venga «de la app» y no de otro programa: cualquier clave
> metida en una app se puede extraer. Por eso la seguridad se basa en tu cuenta de Plex.

## Requisitos

- Docker (por ejemplo con Portainer) en una máquina que llegue a tu Plex.
- Unos 400 MB de disco por cada millón de canciones.
- El token de la cuenta **dueña** del servidor Plex
  ([cómo encontrarlo](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)).

## Instalación con Portainer

> **Imagen ya hecha:** `ghcr.io/xcrittpm/sonora-server:latest` (amd64 y arm64), publicada por GitHub Actions
> con cada cambio. En Portainer puedes usar el *Web editor* con `image:` en lugar de `build: .`.

1. **Stacks → Add stack → Repository**, apuntando a este repositorio y con
   *Compose path* `server/docker-compose.yml`.
   (O bien **Web editor**: pega `docker-compose.yml` y cambia `build: .` por la imagen que hayas
   construido.)
2. En **Environment variables** añade:
   - `PLEX_URL`: tu Plex visto desde el contenedor, p. ej. `http://plex:32400` o `http://192.168.1.10:32400`.
   - `PLEX_TOKEN`: el token de la cuenta dueña.
3. **Deploy the stack.** La primera vez descarga la biblioteca entera (en una biblioteca de 1,1
   millones de canciones, unos 15-20 minutos). Si se reinicia, continúa donde iba. Después solo
   descarga lo nuevo cada 30 minutos.
4. Comprueba que responde: `http://IP-DEL-SERVIDOR:8095/health` → `ok`.

### Variables

| Variable | Por defecto | Para qué |
|---|---|---|
| `PLEX_URL` | — | Dirección de Plex desde el contenedor |
| `PLEX_TOKEN` | — | Token de la cuenta dueña (solo lectura de la biblioteca) |
| `SYNC_INTERVAL_MINUTES` | `30` | Cada cuánto busca novedades |
| `RATE_LIMIT_PER_MINUTE` | `600` | Peticiones por minuto y usuario |
| `LOG_LEVEL` | `INFO` | `DEBUG` para ver el detalle de la sincronización |

## Publicarlo con tu dominio (HTTPS)

Crea un subdominio (p. ej. `sonora.tudominio.com`) que apunte a tu proxy y reenvíe a
`sonora-server:8080` (misma red de Docker) o a `IP-DEL-SERVIDOR:8095`.

**Caddy**

```caddy
sonora.tudominio.com {
    reverse_proxy sonora-server:8080
}
```

**Nginx**

```nginx
server {
    server_name sonora.tudominio.com;
    listen 443 ssl;
    # ... tus certificados ...
    location / {
        proxy_pass http://sonora-server:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Conectar la app

En Sonora: **Ajustes → Buscador → Servidor Sonora** y escribe `https://sonora.tudominio.com`.
Pulsa **Probar conexión**: debe mostrar el número de canciones. Si el servidor no responde, la app
vuelve a buscar en Plex automáticamente.

## API

Todas las rutas (salvo `/health`) piden la cabecera `X-Plex-Token` (o `Authorization: Bearer …`).

| Ruta | Devuelve |
|---|---|
| `GET /api/v1/status` | Estado del índice y número de canciones |
| `GET /api/v1/search?q=` | Mejor resultado, canciones, álbumes y artistas |
| `GET /api/v1/tracks?ids=1,2` | Canciones por id |
| `GET /api/v1/tracks/random?n=` | Canciones al azar |
| `GET /api/v1/albums/{id}` · `/tracks` | Álbum y sus canciones |
| `GET /api/v1/albums/newest?limit=` · `/random?n=` | Novedades y álbumes al azar |
| `GET /api/v1/artists/{id}` · `/albums` · `/tracks` | Artista, sus álbumes y canciones |
| `GET /api/v1/artists/related?names=a\|b&genres=x\|y&exclude=` | Artistas parecidos |

## Desarrollo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
PLEX_URL=http://... PLEX_TOKEN=... DATA_DIR=./data .venv/bin/uvicorn app.main:app --port 8080
```
