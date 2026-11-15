"""Normalización de texto. Debe comportarse igual que `TextNormalizer` en la app."""

import re
import unicodedata

_APOSTROPHES = str.maketrans("", "", "'’‘`´")
_WORD = re.compile(r"[^\W_]+")


def fold(s: str) -> str:
    """Minúsculas y sin tildes: «Beyoncé» → «beyonce»."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def strip_apostrophes(s: str) -> str:
    """«Don't» → «Dont», para que «dont» encuentre «Don't»."""
    return s.translate(_APOSTROPHES)


def tokens(s: str) -> list[str]:
    """Palabras normalizadas: «AC/DC – Back in Black» → ['ac', 'dc', 'back', 'in', 'black']."""
    return _WORD.findall(strip_apostrophes(fold(s)))


def norm(s: str) -> str:
    return " ".join(tokens(s))


def compact(s: str) -> str:
    """Sin espacios ni signos: «AC/DC» → «acdc». Se usa para las erratas."""
    return "".join(tokens(s))
