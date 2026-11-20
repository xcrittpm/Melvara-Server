"""Países de las listas de éxitos: código, nombre en inglés (como lo manda la app a Last.fm) y en español."""

from __future__ import annotations

# Los que muestra la app (Charts.swift), en el mismo orden, más alguno por si se añaden.
COUNTRIES: dict[str, tuple[str, str]] = {
    "es": ("spain", "España"),
    "mx": ("mexico", "México"),
    "ar": ("argentina", "Argentina"),
    "co": ("colombia", "Colombia"),
    "cl": ("chile", "Chile"),
    "pe": ("peru", "Perú"),
    "us": ("united states", "Estados Unidos"),
    "gb": ("united kingdom", "Reino Unido"),
    "fr": ("france", "Francia"),
    "it": ("italy", "Italia"),
    "de": ("germany", "Alemania"),
    "br": ("brazil", "Brasil"),
    "pt": ("portugal", "Portugal"),
    "jp": ("japan", "Japón"),
    "ve": ("venezuela", "Venezuela"),
    "ec": ("ecuador", "Ecuador"),
    "uy": ("uruguay", "Uruguay"),
    "ca": ("canada", "Canadá"),
    "nl": ("netherlands", "Países Bajos"),
    "be": ("belgium", "Bélgica"),
    "ch": ("switzerland", "Suiza"),
    "at": ("austria", "Austria"),
    "ie": ("ireland", "Irlanda"),
    "se": ("sweden", "Suecia"),
    "no": ("norway", "Noruega"),
    "dk": ("denmark", "Dinamarca"),
    "fi": ("finland", "Finlandia"),
    "pl": ("poland", "Polonia"),
    "au": ("australia", "Australia"),
}

# Los que salen en la app (y en la web de administración).
APP_LISTS = ("world", "es", "mx", "ar", "co", "cl", "pe", "us", "gb", "fr", "it", "de", "br", "pt", "jp")

# Playlists «Top <país>» de la cuenta oficial «Deezer Charts» (se actualizan solas cada día).
DEEZER_CHARTS_USER = 637006841
DEEZER_PLAYLISTS: dict[str, int] = {
    "world": 3155776842, "es": 1116190041, "mx": 1111142361, "ar": 1279119721, "co": 1116188451,
    "cl": 1279119121, "pe": 1362518525, "us": 1313621735, "gb": 1111142221, "fr": 1109890291,
    "it": 1116187241, "de": 1111143121, "br": 1111141961, "pt": 1362519755, "jp": 1362508955,
}
# Cómo llama Deezer a la playlist cuando no coincide con el nombre en inglés.
DEEZER_NAMES = {"us": "USA", "gb": "UK"}

_BY_NAME = {name: code for code, (name, _) in COUNTRIES.items()}


def parse(country: str | None) -> str | None:
    """«spain», «Spain» o «es» → «es». None o vacío → None (mundial). Desconocido → ValueError."""
    if not country:
        return None
    value = country.strip().lower()
    if value in ("world", "mundial", "global"):
        return None
    if value in COUNTRIES:
        return value
    if value in _BY_NAME:
        return _BY_NAME[value]
    raise ValueError("País no válido")


def english(code: str) -> str:
    return COUNTRIES[code][0]


def spanish(key: str) -> str:
    if key == "world":
        return "Mundial"
    return COUNTRIES.get(key, (key, key.upper()))[1]
