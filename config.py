"""Configuración de Quietwatch. Sin dependencias: se importa desde todos los módulos."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path

APP_NAME = "Quietwatch"  # nombre visible (ventana, bandeja, mensajes)
DATA_DIR_NAME = "Huella"  # nombre histórico: define la carpeta de datos; no cambiarlo sin migrar
DB_NAME = "huella.db"
CONFIG_NAME = "huella.json"


def default_data_dir() -> Path:
    """Carpeta de datos del usuario. Nada se escribe fuera de ella."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / DATA_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / DATA_DIR_NAME.lower()


@dataclass(frozen=True)
class Config:
    # --- Muestreo ---------------------------------------------------------
    poll_busy_s: float = 10.0        # cadencia con el equipo ocupado
    poll_idle_s: float = 15.0        # cadencia con el equipo tranquilo
    busy_cpu_pct: float = 40.0       # CPU global desde la que se considera ocupado
    max_duty: float = 0.02           # tope de ciclo útil: tick / intervalo <= 2 %

    # --- Persistencia -----------------------------------------------------
    flush_every_s: float = 300.0     # lote a disco cada 5 minutos
    retention_days: int = 400        # se poda solo al arrancar
    min_app_cpu_s: float = 1.0       # por debajo de esto y de min_app_ram_mb,
    min_app_ram_mb: float = 50.0     # la app se pliega en "(otros)"

    # --- Proceso con CPU sostenida ("congelado") --------------------------
    hot_minutes: float = 5.0         # el N del requisito
    hot_core_pct: float = 90.0       # 100 = un núcleo lógico completo
    hot_tolerance: float = 0.10      # fracción de la racha que puede estar por debajo

    # --- Fuga de memoria --------------------------------------------------
    leak_window_min: float = 30.0    # ventana de análisis
    leak_sample_s: float = 60.0      # un punto por minuto, tomando el mínimo del minuto
    leak_track_min_mb: float = 50.0  # no se sigue a procesos más pequeños
    leak_min_growth_mb: float = 64.0 # crecimiento absoluto mínimo en la ventana
    leak_min_growth_pct: float = 10.0
    leak_min_tau: float = 0.70       # tendencia monótona (Kendall)
    leak_tail_ratio: float = 0.33    # pendiente del último tercio / pendiente total
    leak_clear_points: int = 5       # evaluaciones negativas seguidas para apagar la alerta

    # --- Escáner de archivos ----------------------------------------------
    scan_top_n: int = 500
    scan_min_mb: float = 100.0
    scan_duty: float = 0.5           # el escáner descansa la otra mitad del tiempo

    @property
    def leak_points(self) -> int:
        return max(3, int(self.leak_window_min * 60 / self.leak_sample_s))

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Config":
        """Lee huella.json si existe. Las claves desconocidas se ignoran."""
        path = (data_dir or default_data_dir()) / CONFIG_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        clean = {k: v for k, v in raw.items()
                 if k in known and isinstance(v, (int, float))
                 and not isinstance(v, bool) and v > 0}
        return replace(cls(), **clean)
