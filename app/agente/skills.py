"""
Skills: procedimientos que el agente carga solo cuando los necesita (divulgación progresiva).
En el prompt va una línea por skill (nombre y descripción); el cuerpo entra a la conversación
únicamente cuando el modelo llama a cargar_skill. El nombre se busca en el catálogo: nunca se
arma una ruta de archivo con texto del modelo.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

CARPETA = Path(__file__).parent / "skills"


def _leer(archivo: Path) -> tuple[dict, str]:
    texto = archivo.read_text(encoding="utf-8").replace("\r\n", "\n")
    _, cabecera, cuerpo = texto.split("---", 2)
    meta = dict(linea.split(":", 1) for linea in cabecera.strip().splitlines() if ":" in linea)
    return {k.strip(): v.strip() for k, v in meta.items()}, cuerpo.strip()


@lru_cache
def catalogo() -> dict[str, Path]:
    return {_leer(a)[0]["name"]: a for a in sorted(CARPETA.glob("*/SKILL.md"))}


def indice() -> str:
    return "\n".join(f"- {n}: {_leer(a)[0]['description']}" for n, a in catalogo().items())


def cargar(nombre: str) -> str:
    rutas = catalogo()
    if nombre not in rutas:
        return f"SKILL_NO_EXISTE: usa una de {list(rutas)}"
    return _leer(rutas[nombre])[1]
