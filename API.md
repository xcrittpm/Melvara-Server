# API de Melvara Server

Llamadas HTTP de Melvara Server: lista de deseos, denuncias, listas extra, importaciones, Social y escuchar en
grupo. Todas las respuestas son JSON.

En los ejemplos, el servidor está en `https://musica-api.ejemplo.com` (Navidrome en `https://musica.ejemplo.com`;
ver [dónde publicarlo](README.md#dónde-publicarlo)).

## Resumen

| Método | Ruta | Acceso | Para qué |
|---|---|---|---|
| GET | `/api/v1/status` | público | Saber si hay un Melvara Server y qué tiene activado |
| GET | `/api/v1/wishlist` | usuario de Navidrome | Ver tu lista de deseos |
| POST | `/api/v1/wishlist` | usuario de Navidrome | Añadir una canción a tu lista |
| DELETE | `/api/v1/wishlist/{id}` | usuario de Navidrome | Quitar una canción de tu lista |
| POST | `/api/v1/reports` | usuario de Navidrome | Denunciar a una persona o una playlist compartida |
| GET | `/api/v1/reports` | administrador de Navidrome | Ver las denuncias sin revisar (`?all=1`: también las revisadas) |
| PATCH | `/api/v1/reports/{id}` | administrador de Navidrome | Marcar una denuncia como revisada |
| GET | `/api/v1/charts/extra` | usuario de Navidrome | Listas extra de Last.fm que ha añadido el administrador |
| POST | `/api/v1/charts/extra` | administrador de Navidrome | Añadir una lista extra (`kind`: `tag`, `user` o `artist`; `value`; `title`) |
| PATCH / DELETE | `/api/v1/charts/extra/{id}` | administrador de Navidrome | Cambiarla o quitarla |
| POST | `/api/v1/import/jobs` | usuario de Navidrome | Importar en el servidor (la app puede cerrarse) |
| GET | `/api/v1/import/jobs` · `/api/v1/import/jobs/{id}` | usuario de Navidrome | Tu última importación o una concreta: avance y resultado |
| DELETE | `/api/v1/import/jobs/{id}` | usuario de Navidrome | Cancelarla |
| GET | `/api/v1/social/people` | usuario de Navidrome | Quién usa Melvara en este servidor (sin ti ni los perfiles ocultos) |
| POST | `/api/v1/social/visibility` | usuario de Navidrome | `{"hidden": true/false}`: si tu perfil está oculto |
| POST | `/api/v1/social/listening/share` | usuario de Navidrome | `{"share": true/false}`: si compartes lo que escuchas (al quitarlo se borra) |
| POST | `/api/v1/social/listening` | usuario de Navidrome | `{"track": {…}}` al empezar una canción, o `{"stopped": true}` |
| GET | `/api/v1/social/listening` | usuario de Navidrome | Lo que escuchan tus seguidores mutuos que lo comparten |
| POST | `/api/v1/jam` | usuario de Navidrome | Abrir una sesión de «escuchar en grupo» |
| GET / DELETE | `/api/v1/jam/{code}` | quien está en la sesión / el anfitrión | Ver la sesión / cerrarla |
| POST | `/api/v1/jam/{code}/join` · `leave` · `add` · `control` | usuario de Navidrome | Unirse, salir, añadir canciones, cambiar de canción o mover la barra |
| GET | `/api/v1/jam/{code}/pending?after=` | el anfitrión | Lo que han añadido y pedido los demás |
| POST | `/api/v1/jam/{code}/state` · `perms` · `kick` | el anfitrión | Contar qué suena, qué pueden hacer los demás, quitar a alguien |
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
{"service": "melvara-api", "version": "2.10.0", "lastfm": true, "lastfmAuth": true, "serviceApi": true,
 "reports": true, "extraLists": true, "imports": true, "jam": true, "jamControl": true, "social": true}
```

- `service`: siempre `melvara-api` (hasta la 2.2, `sonora-api`). Si no sale esto, no es un Melvara Server.
- `lastfm`: hay búsqueda en Last.fm y listas de éxitos. `lastfmAuth`: se puede conectar el perfil de Last.fm.
- `serviceApi`: la ruta de servicio está activa.
- `reports`, `extraLists`, `imports`: denuncias, listas extra e importaciones en el servidor.
- `jam` (2.8): escuchar en grupo. `jamControl` (2.9): los demás pueden cambiar de canción o mover la barra.
- `social` (2.10): lista de personas y lo que escuchan tus seguidores mutuos.

## Acceso de usuario (cabeceras `X-Melvara-*`)

Las rutas de usuario usan **el mismo acceso que Navidrome** (el de la API de Subsonic), en tres cabeceras:

| Cabecera | Valor |
|---|---|
| `X-Melvara-User` | usuario de Navidrome |
| `X-Melvara-Salt` | un texto al azar (la «sal») |
| `X-Melvara-Token` | `md5(contraseña + sal)`, en hexadecimal |

Las versiones anteriores de la app mandan `X-Sonora-User`, `X-Sonora-Salt` y `X-Sonora-Token`: también se aceptan.

La contraseña nunca viaja. El servidor comprueba el acceso con el propio Navidrome y recuerda el resultado
unos minutos (sin guardar el token). Cada persona solo ve y cambia **su** lista.

Ejemplo para preparar las cabeceras en la terminal:

```bash
U=ana; P='su-contraseña'; S=$(openssl rand -hex 6)
T=$(printf '%s' "$P$S" | md5)          # en Linux: md5sum | cut -d' ' -f1
H=(-H "X-Melvara-User: $U" -H "X-Melvara-Salt: $S" -H "X-Melvara-Token: $T")
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

## Denuncias

Desde Melvara 1.2 (6) se puede denunciar el perfil de otra persona del servidor o una playlist que ha
compartido. La denuncia se guarda aquí y la ven las cuentas **administradoras** de Navidrome en Melvara
(Ajustes → Administración → Denuncias). El servidor pregunta a Navidrome (`getUser`) si la cuenta es
administradora.

```bash
curl "${H[@]}" -X POST -H 'Content-Type: application/json' \
  -d '{"reported":"pepe","reason":"offensive","details":"Foto ofensiva","playlistId":"abc","playlistName":"Lo mejor"}' \
  https://musica-api.ejemplo.com/api/v1/reports
```

`reason`: `offensive`, `spam`, `harassment`, `copyright` u `other`. Cada persona puede tener como mucho 50
denuncias sin revisar. Para verlas y marcarlas (solo administradores):

```bash
curl "${H[@]}" https://musica-api.ejemplo.com/api/v1/reports
curl "${H[@]}" -X PATCH https://musica-api.ejemplo.com/api/v1/reports/3
```

`/api/v1/status` dice `"reports": true` cuando el servidor las recoge.

## Listas extra de Last.fm

Además de los Top por país, quien administra Navidrome puede añadir listas para todas las personas del
servidor (Melvara → Ajustes → Administración → Listas extra). Solo se guarda qué lista es; las canciones se
piden a Last.fm al abrirla, por la ruta de siempre:

```bash
curl "${H[@]}" "https://musica-api.ejemplo.com/api/v1/lastfm/charts?kind=tag&value=reggaeton"
```

| `kind` | Qué es | Método de Last.fm |
|---|---|---|
| `tag` | Lo más escuchado con esa etiqueta («reggaeton», «80s», «indie»…) | `tag.gettoptracks` |
| `user` | Lo más escuchado por ese usuario de Last.fm en los últimos 3 meses | `user.gettoptracks` |
| `artist` | Lo más escuchado de ese artista | `artist.gettoptracks` |

Al añadir o cambiar una, el servidor comprueba que Last.fm la tiene y no está vacía. Como mucho 50.
`/api/v1/status` dice `"extraLists": true` cuando el servidor las admite (desde la 2.6).

## Importar en el servidor

Desde la 2.7. La app manda la lista de canciones de tu biblioteca de otro proveedor (Apple Music, Spotify,
Plex) y el servidor las busca en tu Navidrome, las añade a tu biblioteca de Melvara, crea o completa las
playlists y ordena tu biblioteca, aunque cierres la app.

```json
{"provider": "appleMusic",
 "songs": [{"key": "tusa|karol g|kg0516", "title": "Tusa", "artist": "KAROL G", "album": "KG0516"}],
 "library": ["tusa|karol g|kg0516"],
 "playlists": [{"name": "Favoritas", "keys": ["tusa|karol g|kg0516"]}],
 "reorderLibrary": true, "order": ["tusa|karol g|kg0516"], "previousMatches": {}}
```

- El servidor actúa **como tú** con el acceso que manda la app (usuario, token y sal; nunca la contraseña),
  **solo en memoria** mientras dura la importación. Si el servidor se reinicia a medias, queda «interrupted».
- Una importación a la vez por persona; como mucho 20.000 canciones.
- `GET /api/v1/import/jobs/{id}` → `status` (`running`, `done`, `failed`, `interrupted`, `cancelled`), `done` y
  `total`; al terminar, `result` con lo encontrado (`matches`: clave → id de la canción) y lo que falta.
- La búsqueda es la misma que la de la app (`find_track` en `app/importer.py` y `findTrack` en
  `Melvara/Catalog/Charts.swift`): si se cambia una, hay que cambiar la otra.

Si Navidrome responde «database is locked» (por ejemplo, escaneando la biblioteca), el servidor espera y repite
hasta unos 3 minutos: ese error quiere decir que no se ha guardado nada, así que repetir no duplica (2.9.1).

## Social

Desde la 2.10. La parte social de la app (perfiles, seguir, bloquear) se guarda en Navidrome, en una playlist
«Melvara · Perfil» de cada persona. Melvara Server añade dos cosas:

- **Personas**: apunta a quien usa Melvara con él (nombre de usuario y última vez; se guarda como mucho una vez
  por hora en cualquier llamada). `GET /api/v1/social/people` las devuelve sin quien ha ocultado su perfil
  (`POST /api/v1/social/visibility`).
- **Lo que escuchas**: si lo compartes (`POST /api/v1/social/listening/share`), la app cuenta cada canción que
  empieza y el servidor guarda la actual y las 5 últimas. `GET /api/v1/social/listening` solo devuelve a
  **seguidores mutuos** (cada uno en el `following` del otro, aceptado si el perfil es privado, sin bloqueos ni
  perfiles ocultos) que también compartan, y nada si quien pregunta no comparte. Los seguimientos se leen de los
  perfiles en Navidrome **con el acceso de quien pregunta** (un minuto en memoria), así que no se pueden falsear.
  Es solo de Melvara: Navidrome sigue recibiendo «lo que suena» como siempre.

```json
{"sharing": true, "friends": [{"user": "pepe", "nowPlaying": {"id": "s1", "title": "Tusa"}, "since": 1763400000,
  "recent": [{"id": "s1", "title": "Tusa"}]}]}
```

## Escuchar en grupo

Desde la 2.8. Quien abre la sesión (`POST /api/v1/jam` → `code`) enseña un QR `melvara://jam?code=…&server=…`.
Solo pueden unirse personas con cuenta en el mismo Navidrome. Todo vive en memoria y caduca tras 6 horas sin uso.

- Los demás añaden canciones (`add`) y el anfitrión las recoge (`pending`) y las mete en su cola.
- Desde la 2.9, si el anfitrión lo permite (`perms`: `{"skip": true, "seek": true}`), pueden pedir
  `control` con `{"action": "next" | "previous" | "seek", "position": 30}`; el anfitrión lo aplica.
- El anfitrión cuenta qué suena, por dónde va (`position`, `playing`) y qué viene (`state`); el estado lleva
  `now` (la hora del servidor) para calcular la posición sin depender del reloj de cada iPhone.

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
| `MELVARA_URL` | `https://musica-api.ejemplo.com` |
| `MELVARA_SERVICE_TOKEN` | el mismo valor que `SERVICE_TOKEN` |

### Leer por páginas

```bash
curl -H "Authorization: Bearer $MELVARA_SERVICE_TOKEN" \
  "$MELVARA_URL/api/v1/service/wishlist?after=0&limit=200"
```

```json
{"items": [
  {"id": 12, "user": "ana", "title": "Nicole Kidman", "artist": "ADÉLA", "album": null, "year": null, "addedAt": 1794836000,
   "status": "pending", "statusNote": null, "statusAt": null},
  {"id": 13, "user": "ana", "title": "Housewife", "artist": "Tate McRae", "album": "So Close to What", "year": 2025, "addedAt": 1794836100,
   "status": "downloading", "statusNote": "Pedida el 16/11/2026", "statusAt": 1794836400}
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
            f"{os.environ['MELVARA_URL']}/api/v1/service/wishlist?after={after}&limit=500",
            headers={"Authorization": f"Bearer {os.environ['MELVARA_SERVICE_TOKEN']}"})
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
curl -X PATCH -H "Authorization: Bearer $MELVARA_SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"status": "in_library", "note": "En la biblioteca desde el 16/11/2026"}' \
  "$MELVARA_URL/api/v1/service/wishlist/13"
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
