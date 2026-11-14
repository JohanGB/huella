"""Tasa de detección del criterio de fuga sobre series sintéticas de 30 minutos."""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from anomalies import is_leaking
from config import Config

CFG = Config()
N = CFG.leak_points


def ar1(phi=0.8, sigma=8.0):
    x, out = 0.0, []
    for _ in range(N):
        x = phi * x + random.gauss(0, sigma)
        out.append(x)
    return out


def walk(sigma):
    x, out = 800.0, []
    for _ in range(N):
        x += random.gauss(0, sigma)
        out.append(x)
    return out


def scenarios(rng_scale):
    s = rng_scale
    yield "fuga 4 MB/min + ruido", True, lambda: [500 + 4 * s * i + v for i, v in enumerate(ar1(sigma=4))]
    yield "fuga lenta 3 MB/min", True, lambda: [500 + 3 * i + v for i, v in enumerate(ar1(sigma=3))]
    yield "fuga con escalones", True, lambda: [500 + 45 * (i // 3) + random.gauss(0, 3) for i in range(N)]
    yield "fuga + GC en sierra", True, lambda: [500 + 6 * i + 25 * ((i % 4) / 4) + random.gauss(0, 3) for i in range(N)]
    yield "estable + ruido", False, lambda: [800 + v for v in ar1()]
    yield "paseo aleatorio σ=8 MB/min", False, lambda: walk(8)
    yield "paseo aleatorio σ=25 MB/min", False, lambda: walk(25)
    yield "caché que se aplana", False, lambda: [300 + 500 / (1 + math.exp(-(i - 10) / 2.5)) + random.gauss(0, 3) for i in range(N)]
    yield "sierra plana (GC)", False, lambda: [500 + 120 * ((i % 8) / 8) + random.gauss(0, 5) for i in range(N)]
    yield "un escalón y meseta", False, lambda: [500 + (300 if i > 12 else 0) + random.gauss(0, 3) for i in range(N)]


def main(trials=5000):
    random.seed(20261103)
    print(f"{'escenario':34} {'esperado':>9} {'alerta':>8}")
    for name, expected, make in scenarios(1):
        hits = sum(bool(is_leaking(make(), CFG)) for _ in range(trials))
        print(f"{name:34} {'alerta' if expected else 'nada':>9} {hits / trials:8.1%}")


if __name__ == "__main__":
    main()
