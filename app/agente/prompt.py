"""Arma el prompt de sistema (por capas, en prompts/sistema.md) con el contexto de cada llamada."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agente.guardia import limpiar_etiquetas

PLANTILLA = (Path(__file__).parent / "prompts" / "sistema.md").read_text(encoding="utf-8")
VERSION = "agente-v2"   # v2: etiquetas + guardia (semana 14)

PERFILES = {"medico": "médico general", "especialista": "médico especialista", "admin": "administrador",
            "coordinador": "coordinador del hospital", "enfermero_jefe": "enfermería jefe"}


class _Seguro(dict):
    def __missing__(self, clave):
        return "{" + clave + "}"


def armar_prompt(ctx, indice_skills: str, preferencias: list[str], primer_turno: bool) -> str:
    return PLANTILLA.format_map(_Seguro(
        usuario=limpiar_etiquetas(ctx.username),
        rol=PERFILES.get(ctx.rol, ctx.rol),
        paciente=(f"{ctx.paciente_alias} (seudónimo)" if ctx.paciente_alias
                  else "ninguna (solo guías y datos de operación; no hay historia de paciente disponible)"),
        fecha=datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%d %H:%M"),
        skills=indice_skills or "(ninguna)",
        preferencias="\n".join(f"- {limpiar_etiquetas(p)}" for p in preferencias) or "(ninguna)",
        saludo=("Es el primer mensaje: puedes saludar en una frase corta." if primer_turno else
                "La conversación ya empezó: no saludes, no te presentes y no repitas que eres una IA."),
    ))
