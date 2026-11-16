# API de Sonora Server

Llamadas HTTP de Sonora Server, en especial las de la **lista de deseos**. Todas las respuestas son JSON.

En los ejemplos, el servidor está en `https://musica-api.ejemplo.com` (Navidrome en `https://musica.ejemplo.com`;
ver [dónde publicarlo](README.md#dónde-publicarlo)).

## Resumen

| Método | Ruta | Acceso | Para qué |
|---|---|---|---|
| GET | `/api/v1/status` | público | Saber si hay un Sonora Server y qué tiene activado |
| GET | `/api/v1/wishlist` | usuario de Navidrome | Ver tu lista de deseos |
| POST | `/api/v1/wishlist` | usuario de Navidrome | Añadir una canción a tu lista |
| DELETE | `/api/v1/wishlist/{id}` | usuario de Navidrome | Quitar una canción de tu lista |
| GET | `/api/v1/service/wishlist` | token de servicio | Leer las listas de **todos** (otras aplicaciones) |
| PATCH | `/api/v1/service/wishlist/{id}` | token de servicio | Cambiar el estado de un deseo (p. ej. «en la biblioteca») |
| GET | `/api/v1/lastfm/search?q=` | usuario de Navidrome | Buscar canciones en Last.fm |
| GET | `/api/v1/lastfm/charts?country=` | usuario de Navidrome | Listas de éxitos de Last.fm |
| POST | `/api/v1/lastfm/call` | usuario de Navidrome | Llamada de Last.fm firmada por el servidor (usa la app) |
| GET | `/health` | público | Responde `ok` (para Docker) |

## Estado

```bash
curl https://musica-api.ejemplo.com/api/v1/status
```

```json
{"service": "sonora-api", "version": "2.1.0", "lastfm": true, "lastfmAuth": true, "serviceApi": true}
```

- `service`: siempre `sonora-api`. Si no sale esto, no es un Sonora Server.
- `lastfm`: hay búsqueda en Last.fm y listas de éxitos. `lastfmAuth`: se puede conectar el perfil de Last.fm.
- `serviceApi`: la ruta de servicio está activa.

## Acceso de usuario (cabeceras `X-Sonora-*`)

Las rutas de usuario usan **el mismo acceso que Navidrome** (el de la API de Subsonic), en tres cabeceras:

| Cabecera | Valor |
|---|---|
| `X-Sonora-User` | usuario de Navidrome |
| `X-Sonora-Salt` | un texto al azar (la «sal») |
| `X-Sonora-Token` | `md5(contraseña + sal)`, en hexadecimal |

La contraseña nunca viaja. El servidor comprueba el acceso con el propio Navidrome y recuerda el resultado
unos minutos (sin guardar el token). Cada persona solo ve y cambia **su** lista.

Ejemplo para preparar las cabeceras en la terminal:

```bash
U=ana; P='su-contraseña'; S=$(openssl rand -hex 6)
T=$(printf '%s' "$P$S" | md5)          # en Linux: md5sum | cut -d' ' -f1
H=(-H "X-Sonora-User: $U" -H "X-Sonora-Salt: $S" -H "X-Sonora-Token: $T")
```

### Ver tu lista

```bash
curl "${H[@]}" https://musica-api.ejemplo.com/api/v1/wishlist
```

```json
{"items": [
  {"id": 12, "title": "Nicole Kidman", "artist": "ADÉLA", "album": null, "year": null, "addedAt": 1794836000,
   "status": "in_library", "statusNote": "En la biblioteca desde el 16/11/2026", "statusAt": 1794840000}
]}
```

De la más reciente a la más antigua. `album` y `year` pueden ser `null`. `addedAt` es la fecha en segundos
(Unix).

`status` dice cómo va cada deseo; lo pone otra aplicación con la ruta de servicio (ver más abajo):

| `status` | Significa |
|---|---|
| `pending` | Pendiente (lo que tiene todo deseo nuevo) |
| `downloading` | Pedida: se está consiguiendo |
| `in_library` | Ya está en la biblioteca |
| `not_found` | No se encuentra |
| `done` | Dada por hecha a mano |

`statusNote` (texto o `null`) lo explica y `statusAt` es cuándo cambió (segundos Unix o `null`).

### Añadir una canción

```bash
curl "${H[@]}" -X POST -H 'Content-Type: application/json' \
  -d '{"title": "Housewife", "artist": "Tate McRae", "album": "So Close to What", "year": 2025}' \
  https://musica-api.ejemplo.com/api/v1/wishlist
```

```json
{"item": {"id": 13, "title": "Housewife", "artist": "Tate McRae", "album": "So Close to What", "year": 2025, "addedAt": 1794836100}}
```

- Obligatorios: `title` y `artist`. Opcionales: `album` y `year` (número entre 1000 y 3000).
- Si la canción (mismo título y artista) ya estaba, no se duplica: se devuelve la que había, completando
  álbum y año si faltaban.
- Hasta 5000 canciones por persona; cada texto, hasta 300 caracteres.

### Añadir varias de una vez

Igual, pero con `items` (de 1 a 500 canciones por petición). Cuenta como una sola petición para el límite por
minuto; la app lo usa para guardar las no encontradas al importar.

```bash
curl "${H[@]}" -X POST -H 'Content-Type: application/json' \
  -d '{"items": [{"title": "Housewife", "artist": "Tate McRae"}, {"title": "90%", "artist": "C Marí", "album": "Sempre Cor (Deluxe)"}]}' \
  https://musica-api.ejemplo.com/api/v1/wishlist
```

```json
{"items": [{"id": 13, "title": "Housewife", …}, {"id": 14, "title": "90%", …}], "skipped": 0}
```

`skipped`: las que no se pudieron guardar (sin título o artista, o porque la lista se llenó).

### Quitar una canción

```bash
curl "${H[@]}" -X DELETE https://musica-api.ejemplo.com/api/v1/wishlist/13
```

```json
{"deleted": true}
```

Si ese `id` no existe o es de otra persona: 404.

## Ruta de servicio: las listas de todos

Para otra aplicación (por ejemplo, una que busque y consiga lo que la gente ha pedido). Se activa poniendo
`SERVICE_TOKEN` en el `.env` del servidor, con 32 caracteres o más:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

El token solo se pone por variable de entorno (en el servidor y en la otra aplicación), nunca en la app ni en
el código, y el servidor no lo escribe en los registros. La otra aplicación necesita:

| Variable | Ejemplo |
|---|---|
| `SONORA_URL` | `https://musica-api.ejemplo.com` |
| `SONORA_SERVICE_TOKEN` | el mismo valor que `SERVICE_TOKEN` |

### Leer por páginas

```bash
curl -H "Authorization: Bearer $SONORA_SERVICE_TOKEN" \
  "$SONORA_URL/api/v1/service/wishlist?after=0&limit=200"
```

```json
{"items": [
  {"id": 12, "user": "ana", "title": "Nicole Kidman", "artist": "ADÉLA", "album": null, "year": null, "addedAt": 1794836000,
   "status": "pending", "statusNote": null, "statusAt": null},
  {"id": 13, "user": "ana", "title": "Housewife", "artist": "Tate McRae", "album": "So Close to What", "year": 2025, "addedAt": 1794836100,
   "status": "downloading", "statusNote": "Pedida en la Gramola", "statusAt": 1794836400}
], "next": 13}
```

- `after`: devuelve las canciones con `id` mayor que este (≥ 0; por defecto 0).
- `limit`: cuántas como máximo (de 1 a 500; por defecto 200).
- Ordenadas por `id`, de menor a mayor. `user` va en minúsculas. `album` y `year` pueden ser `null`.
- `next`: el último `id` de la página; `null` cuando la página viene vacía. Para leerlo todo, pide páginas con
  `after=next` hasta que `next` sea `null`. Guardando el último `next`, la próxima vez solo recibes lo nuevo.
- Las canciones que una persona quita de su lista dejan de aparecer.

Ejemplo en Python (sin librerías extra):

```python
import json, os, urllib.request

def todos_los_deseos(after=0):
    while True:
        req = urllib.request.Request(
            f"{os.environ['SONORA_URL']}/api/v1/service/wishlist?after={after}&limit=500",
            headers={"Authorization": f"Bearer {os.environ['SONORA_SERVICE_TOKEN']}"})
        page = json.load(urllib.request.urlopen(req, timeout=30))
        yield from page["items"]
        if page["next"] is None:
            return
        after = page["next"]

for d in todos_los_deseos():
    print(d["user"], "-", d["artist"], "-", d["title"])
```

### Cambiar el estado de un deseo

```bash
curl -X PATCH -H "Authorization: Bearer $SONORA_SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"status": "in_library", "note": "En la biblioteca desde el 16/11/2026"}' \
  "$SONORA_URL/api/v1/service/wishlist/13"
```

```json
{"item": {"id": 13, "user": "ana", "title": "Housewife", "artist": "Tate McRae", "album": "So Close to What",
          "year": 2025, "addedAt": 1794836100, "status": "in_library",
          "statusNote": "En la biblioteca desde el 16/11/2026", "statusAt": 1794840000}}
```

- `status`: `pending`, `downloading`, `in_library`, `not_found` o `done`. `note` es opcional (hasta 300
  caracteres).
- 404 si ese deseo ya no existe (la persona lo quitó). Volver a añadir la misma canción no le quita el estado.

## Errores

Siempre con un mensaje: `{"error": "…"}`.

| Código | Cuándo |
|---|---|
| 400 | Datos no válidos (falta título o artista, `limit` fuera de rango, lista llena…) |
| 401 | Falta el acceso o no es válido en Navidrome; o el token de servicio falta o es incorrecto |
| 404 | No existe (una canción de la lista; o la ruta de servicio sin `SERVICE_TOKEN` configurado) |
| 429 | Demasiadas peticiones: hay un límite por minuto (`RATE_LIMIT_PER_MINUTE`, 120 por defecto), por usuario y, aparte, para la ruta de servicio |
