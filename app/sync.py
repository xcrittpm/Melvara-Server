"""Mantiene el índice al día: la primera vez lo descarga todo (y puede continuar si se corta);
después, cada cierto tiempo, solo lo nuevo o cambiado."""

from __future__ import annotations

import logging
import threading
import time

from .catalog import Catalog
from .plex import Plex, album_from, artist_from, track_from

log = logging.getLogger("sonora.sync")

# Tipo de Plex, tamaño de página, función de conversión, método de inserción, tramo de progreso.
KINDS = [
    ("8", 2000, artist_from, "insert_artists", 0.00, 0.04),
    ("9", 2000, album_from, "insert_albums", 0.04, 0.12),
    ("10", 3000, track_from, "insert_tracks", 0.12, 1.00),
]


class Syncer(threading.Thread):
    def __init__(self, catalog: Catalog, plex: Plex, interval_minutes: int):
        super().__init__(daemon=True, name="sonora-sync")
        self.catalog = catalog
        self.plex = plex
        self.interval = max(interval_minutes, 5) * 60
        self.syncing = False
        self.progress = 0.0
        self.last_error: str | None = None
        self._wake = threading.Event()
        self._full_requested = False

    def request(self, full: bool = False) -> None:
        self._full_requested = self._full_requested or full
        self._wake.set()

    def run(self) -> None:
        while True:
            try:
                synced = self.catalog.meta("syncedAt")
                resuming = self.catalog.meta("fullSync") == "running"
                full = synced is None or resuming or self._full_requested
                self._full_requested = False
                self.sync(full)
            except Exception as exc:  # seguimos intentándolo en la siguiente vuelta
                self.last_error = str(exc)
                log.exception("Error al sincronizar")
            finally:
                self.syncing = False
            self._wake.wait(self.interval)
            self._wake.clear()

    def sync(self, full: bool) -> None:
        cat = self.catalog
        self.syncing = True
        self.progress = 0.0
        self.last_error = None
        started = int(time.time()) - 120
        since = None if full else int(float(cat.meta("syncedAt") or 0))
        resuming = full and cat.meta("fullSync") == "running"
        if full and not resuming:
            if cat.track_count() > 0:
                cat.wipe()
            cat.set_meta("fullSync", "running")
        log.info("Sincronización %s%s", "completa" if full else "incremental",
                 " (continuando)" if resuming else "")

        sections = self.plex.music_sections()
        cat.set_meta("sections", ",".join(map(str, sections)))
        filters = {} if since is None else {"updatedAt>>": str(since)}
        n = max(len(sections), 1)

        for i, section in enumerate(sections):
            base = i / n
            path = f"/library/sections/{section}/all"
            for plex_type, page_size, convert, insert, lo, hi in KINDS:
                resume_key = f"resume-{section}-{plex_type}"
                start = int(cat.meta(resume_key) or 0) if full else 0
                fresh = full and not resuming and start == 0
                for items, fraction, nxt in self.plex.pages(path, {**filters, "type": plex_type}, page_size, start):
                    rows = [r for r in (convert(m, section) for m in items) if r]
                    getattr(cat, insert)(rows, fresh)
                    if full:
                        cat.set_meta(resume_key, str(nxt))
                    self.progress = base + (lo + (hi - lo) * fraction) / n

        cat.clear_resume_state()
        cat.set_meta("syncedAt", str(started))
        if full:
            cat.checkpoint()
        self.progress = 1.0
        log.info("Índice actualizado: %s", cat.counts())
