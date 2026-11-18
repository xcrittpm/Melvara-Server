# Melvara Server

Servicio opcional que se instala junto a tu **Navidrome** para dar a la app Melvara:

- **Last.fm sin claves para nadie**: con la clave de quien lo instala, todas las personas de tu Navidrome
  tienen la búsqueda en Last.fm, las listas de éxitos y pueden conectar su perfil de Last.fm (para apuntar lo
  que escuchan) solo con su usuario y contraseña. La clave y el secreto no salen del servidor; la contraseña
  de Last.fm solo se reenvía a Last.fm una vez para obtener la sesión, y no se guarda.
- **Búsqueda en Last.fm**: encontrar canciones que no tienes en tu servidor.
- **Lista de deseos**: guardar esas canciones (título, artista, álbum y año; nunca música) para conseguirlas
  después. Cada persona ve solo la suya.
- **Importar en el servidor**: traer tu biblioteca de Apple Music, Spotify o Plex aunque cierres la app.
- **Listas extra** de Last.fm (por etiqueta, usuario o artista) que elige quien administra.
- **Social**: denuncias que llegan a los administradores, la lista de personas que usan Melvara en tu servidor y
  lo que escuchan tus seguidores mutuos (solo si lo compartís).
- **Escuchar en grupo**: tus amigos añaden canciones a tu cola escaneando un QR.

Detalle de cada llamada en [API.md](API.md).

## Seguridad

Solo responde a quien tenga cuenta en tu Navidrome: la app envía el mismo acceso que usa con Navidrome
(usuario, token y sal; la contraseña nunca viaja) y el servidor lo comprueba con el propio Navidrome. Los
accesos no se guardan. Hay un límite de peticiones por minuto. La única ruta pública es `/api/v1/status`, que
no da ningún dato.

## Dónde publicarlo

La app lo busca sola, sin configurar nada, en una dirección derivada de la de Navidrome:

| Navidrome en | Melvara Server en |
|---|---|
| `ejemplo.com` (dominio principal) | `melvara-api.ejemplo.com` |
| `musica.ejemplo.com` (subdominio) | `musica-api.ejemplo.com` |
| una IP, `localhost` o un nombre sin dominio | no disponible |

Siempre por **https** y dentro de tu mismo dominio. Si en esa dirección no responde un Melvara Server, la app
no le envía nada.

## Instalar (Docker)

1. Copia `.env.example` como `.env` y rellena:
   - `NAVIDROME_URL`: tu Navidrome visto **desde el contenedor** (p. ej. `http://192.168.1.10:4533`).
   - `LASTFM_API_KEY` y `LASTFM_SHARED_SECRET`: clave y secreto gratuitos de
     [Last.fm](https://www.last.fm/api/account/create). Sin la clave hay lista de deseos pero no Last.fm; sin el
     secreto no se puede conectar el perfil de Last.fm desde la app.
2. `docker compose up -d --build`
3. Publica el puerto 8095 con tu proxy (Caddy, Nginx…) en la dirección de la tabla, con https.
4. Comprueba: `curl https://<tu-dirección>/api/v1/status` → `{"service":"melvara-api",…}`.

## API

Todas las llamadas, con ejemplos (lista de deseos de cada persona y ruta de servicio): [API.md](API.md).

## Ruta de servicio (opcional, para otras aplicaciones)

Con `SERVICE_TOKEN` (32 caracteres o más) se activa una ruta con los deseos de todos los usuarios, por páginas,
y otra para marcar cómo va cada uno:

```
GET /api/v1/service/wishlist?after=<id>&limit=<n>
Authorization: Bearer <SERVICE_TOKEN>
```

- `after` ≥ 0 (por defecto 0); `limit` de 1 a 500 (por defecto 200).
- Devuelve `{"items": [{"id", "user", "title", "artist", "album", "year", "addedAt", "status", "statusNote",
  "statusAt"}], "next": <último id o null>}`, ordenado por `id`. Para seguir, pide la siguiente página con
  `after=next`.
- `PATCH /api/v1/service/wishlist/{id}` con `{"status": "pending"|"downloading"|"in_library"|"not_found"|"done",
  "note": "…"}` cambia el estado del deseo. La app lo recibe en su lista (`status`, `statusNote`, `statusAt`).
- Sin token o con uno incorrecto: 401. Sin `SERVICE_TOKEN` configurado, la ruta no existe (404).
- `/api/v1/status` indica `"serviceApi": true` cuando está activa.
- Detalles y ejemplo para leerlo todo: [API.md](API.md#ruta-de-servicio-las-listas-de-todos).

## Legal

Solo se guardan y se piden datos de canciones (título, artista, álbum), no música ni imágenes, y con la clave
de API de quien lo instala, para uso personal y no comercial, como permiten las condiciones de Last.fm.
