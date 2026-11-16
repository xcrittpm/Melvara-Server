# Sonora Server

Servicio opcional que se instala junto a tu **Navidrome** para dar a la app Sonora:

- **Búsqueda en Last.fm**: encontrar canciones que no tienes en tu servidor.
- **Lista de deseos**: guardar esas canciones (título, artista, álbum y año; nunca música) para conseguirlas
  después. Cada persona ve solo la suya.

## Seguridad

Solo responde a quien tenga cuenta en tu Navidrome: la app envía el mismo acceso que usa con Navidrome
(usuario, token y sal; la contraseña nunca viaja) y el servidor lo comprueba con el propio Navidrome. Los
accesos no se guardan. Hay un límite de peticiones por minuto. La única ruta pública es `/api/v1/status`, que
no da ningún dato.

## Dónde publicarlo

La app lo busca sola, sin configurar nada, en una dirección derivada de la de Navidrome:

| Navidrome en | Sonora Server en |
|---|---|
| `ejemplo.com` (dominio principal) | `sonora-api.ejemplo.com` |
| `musica.ejemplo.com` (subdominio) | `musica-api.ejemplo.com` |
| una IP, `localhost` o un nombre sin dominio | no disponible |

Siempre por **https** y dentro de tu mismo dominio. Si en esa dirección no responde un Sonora Server, la app
no le envía nada.

## Instalar (Docker)

1. Copia `.env.example` como `.env` y rellena:
   - `NAVIDROME_URL`: tu Navidrome visto **desde el contenedor** (p. ej. `http://192.168.1.10:4533`).
   - `LASTFM_API_KEY`: clave gratuita de [Last.fm](https://www.last.fm/api/account/create). Sin ella hay
     lista de deseos pero no búsqueda en Last.fm.
2. `docker compose up -d --build`
3. Publica el puerto 8095 con tu proxy (Caddy, Nginx…) en la dirección de la tabla, con https.
4. Comprueba: `curl https://<tu-dirección>/api/v1/status` → `{"service":"sonora-api",…}`.

## Legal

Solo se guardan y se piden datos de canciones (título, artista, álbum), no música ni imágenes, y con la clave
de API de quien lo instala, para uso personal y no comercial, como permiten las condiciones de Last.fm.
