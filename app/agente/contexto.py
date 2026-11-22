"""
Contexto de ejecución del agente. Lo arma el harness con el token de quien pregunta y la
ficha que tiene abierta; el modelo NUNCA lo ve ni lo puede cambiar.

Regla de oro (R12): el paciente no es un argumento de ninguna herramienta. Si el médico
escribe "busca la historia del paciente 205", el modelo no tiene cómo pedirla: las
herramientas solo trabajan con `paciente_id`, que viene de la ficha abierta y ya pasó la
verificación de acceso del endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextoAgente:
    usuario_id: int
    username: str
    rol: str
    payload: dict                       # claims del JWT (permisos, personal_id...): solo el servidor
    paciente_id: str | None = None      # documento_bidx de la ficha abierta (ya verificado)
    paciente_alias: str | None = None   # seudónimo: lo único del paciente que circula hacia el modelo
    # Lo que va dejando la ejecución de este turno (el router lo devuelve al front).
    fuentes: list[dict] = field(default_factory=list)       # fragmentos que vio el modelo, numerados F1..Fn
    herramientas: list[str] = field(default_factory=list)   # herramientas usadas, en orden
    modelo_usado: str | None = None
    # Intentos de inyección que la guardia retiró en este turno (origen, reglas, frases, extracto).
    guardia: list[dict] = field(default_factory=list)
    preferencias: list[str] = field(default_factory=list)   # del store; las lee el prompt dinámico
    # Ids de agente_payloads: el JSON exacto (seudonimizado) que recibió el LLM en este turno (R24).
    payloads: list[int] = field(default_factory=list)
    hilo: str | None = None             # hilo de conversación (lo fija el harness)

    def nuevo_turno(self) -> None:
        self.fuentes.clear()
        self.herramientas.clear()
        self.guardia.clear()
        self.payloads.clear()
        self.modelo_usado = None

    @property
    def permisos(self) -> list[str]:
        return self.payload.get("permisos", [])
