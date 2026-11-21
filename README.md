# Melvara Server

Servicio opcional que se instala junto a tu **Navidrome** para dar a la app Melvara:

- **Buscar fuera de tu servidor**: encontrar canciones que no tienes (en **Deezer**, gratis y sin clave, o en
  Last.fm).
- **Listas de éxitos** por país y mundial, de **Apple Music**, **Deezer**, **ListenBrainz** o **Last.fm** (se
  elige en la [web de administración](#web-de-administración), incluso una fuente distinta para cada lista).
  El servidor las descarga, las guarda y las renueva cada 6 horas: la app las recibe al momento.
- **Last.fm sin claves para nadie** (opcional): con la clave de quien lo instala, todas las personas de tu
  Navidrome pueden conectar su perfil de Last.fm (para apuntar lo que escuchan) solo con su usuario y
  contraseña. La clave y el secreto no salen del servidor; la contraseña de Last.fm solo se reenvía a Last.fm una
  vez para obtener la sesión, y no se guarda. **Desde el 1 de diciembre de 2026, la clave de API de Last.fm
  solo funciona si su cuenta tiene Last.fm Pro**: sin Pro, desactiva Last.fm en la web de administración y la
  búsqueda y las listas siguen con las otras fuentes.
- **Lista de deseos**: guardar esas canciones (título, artista, álbum y año; nunca música) para conseguirlas
  después. Cada persona ve solo la suya.
- **Importar en el servidor**: traer tu biblioteca de Apple Music, Spotify o Plex aunque cierres la app.
- **Listas extra** (por etiqueta o usuario de Last.fm, o de un artista) que elige quien administra.
- **Social**: denuncias que llegan a los administradores, la lista de personas que usan Melvara en tu servidor y
  lo que escuchan tus seguidores mutuos (solo si lo compartís).
- **Escuchar en grupo**: tus amigos añaden canciones a tu cola escaneando un QR.
- **Invitaciones por enlace** (2.12): desde Administración en la app se crea la cuenta y un enlace; quien lo abre
  en su iPhone elige su contraseña y entra sola. En el enlace solo va un código de un solo uso que caduca en 7
  días; la contraseña temporal se guarda cifrada con ese código y se borra al usarla.

Detalle de cada llamada en [API.md](API.md).

## Seguridad

Solo responde a quien tenga cuenta en tu Navidrome: la app envía el mismo acceso que usa con Navidrome
(usuario, token y sal; la contraseña nunca viaja) y el servidor lo comprueba con el propio Navidrome. Los
accesos no se guardan. Hay un límite de peticiones por minuto. La única ruta pública es `/api/v1/status`, que
no da ningún dato (y la página de entrada de `/admin`).

## Web de administración

En `https://<tu-dirección>/admin`, con una cuenta **administradora de Navidrome** (la contraseña solo se usa para
preguntarle a Navidrome si es válida; no se guarda). Desde ahí se elige:

- **Buscar canciones**: Deezer (predeterminado) o Last.fm.
- **Listas de éxitos**: la fuente predeterminada (Apple Music) y, si quieres, otra para cada lista. Se ve de
  dónde ha salido cada una, cuántas canciones tiene y cuándo se descargó; también se pueden renovar todas.
  Si una fuente falla, se usa la siguiente que tenga esa lista y, si fallan todas, la última buena. Apple
  Music no tiene lista mundial; ListenBrainz solo tiene la mundial.
- **Cuánto se guarda**: horas que una búsqueda igual se responde de memoria (12 por defecto) y cada cuántas
  horas se renuevan las listas (6).
- **Versión mínima de la app**: quien abra Melvara con una anterior ve un aviso para actualizar (se puede cerrar).
- **Personas**: quién usa Melvara con tu servidor, cuándo fue la última vez y con qué versión de la app.
- **Last.fm**: activarlo o no, y su clave y secreto (si los pones aquí, sustituyen a los del `.env`). Al
  guardar, dice si Last.fm acepta la clave.

La sesión dura 12 horas. Los ajustes se guardan en la base de datos (`/data/melvara.sqlite`, tabla `settings`).

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

1. Descarga el código y entra en la carpeta:
   `git clone https://github.com/xcrittpm/Melvara-Server.git && cd Melvara-Server`
2. Copia `.env.example` como `.env` y rellena:
   - `NAVIDROME_URL`: tu Navidrome visto **desde el contenedor** (p. ej. `http://192.168.1.10:4533`).
   - Opcional: `LASTFM_API_KEY` y `LASTFM_SHARED_SECRET`, de [Last.fm](https://www.last.fm/api/account/create)
     (desde el 1/12/2026, con Last.fm Pro). También se pueden poner luego en la web de administración. Sin
     clave, la búsqueda y las listas salen de Deezer, Apple Music y ListenBrainz; sin el secreto no se puede
     conectar el perfil de Last.fm desde la app.
3. `docker compose up -d --build` (para actualizar: `git pull` y otra vez lo mismo)
4. Publica el puerto 8095 con tu proxy (Caddy, Nginx…) en la dirección de la tabla, con https.
5. Comprueba: `curl https://<tu-dirección>/api/v1/status` → `{"service":"melvara-api",…}`.

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

Solo se piden y se guardan datos de canciones (título, artista, álbum): no música, ni imágenes, ni biografías.
Deezer, Apple Music (feed público de Apple Marketing Tools) y ListenBrainz se usan sin clave y la app dice de
dónde sale cada lista; Last.fm, con la clave de API de quien lo instala. Es para uso personal y no comercial:
revisa las condiciones de cada servicio si lo usas de otra forma. A estos servicios solo llega lo buscado y el
país de la lista, nunca quién lo pide.

## Licencia

[MIT](LICENSE). La app Melvara (iPhone, iPad, CarPlay y Android) está en
[github.com/xcrittpm/Melvara](https://github.com/xcrittpm/Melvara).
