"""
POST /agente/chat     · conversar con Lati como agente (Semana 13)
POST /agente/decidir  · aprobar o rechazar la acción que el agente propuso
GET  /agente/estado   · si el agente está disponible para este perfil (el front decide con esto)

Seguridad (lo que el harness fija y el modelo no puede cambiar):
  · rol: paciente y contable no usan el agente (403); siguen con /rag/preguntar;
  · paciente: solo el de la ficha abierta, y antes de entrar se verifica que el usuario lo
    pueda ver (misma guarda de dos niveles de las pantallas);
  · aprobar: solo quien tiene remision:create, y solo en su propio hilo;
  · auditoría: cada turno, cada herramienta y cada decisión quedan en log_auditoria.
"""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agente.contexto import ContextoAgente
from app.agente.harness import Harness, _auditar
from app.agente.modelo import hay_modelo_real, modelos_del_agente
from app.core.config import get_settings
from app.core.deps import ContextoAcceso, get_db, get_token_payload, get_usuario_actual
from app.core.security import tiene_permiso
from app.models import Paciente, Usuario
from app.services import incidentes
from app.services.auditoria import registrar

router = APIRouter(prefix="/agente", tags=["Agente"])

ROLES_SIN_AGENTE = {"paciente", "contable"}
CONVERSACION = r"^[\w-]{1,40}$"


class Mensaje(BaseModel):
    mensaje: str = Field(..., min_length=2, max_length=1000)
    conversacion_id: str = Field("principal", pattern=CONVERSACION)
    paciente_id: str | None = Field(None, max_length=64, description="Ficha abierta (documento_bidx)")


class Decision(BaseModel):
    conversacion_id: str = Field(..., pattern=CONVERSACION)
    paciente_id: str | None = Field(None, max_length=64)
    decision: Literal["approve", "reject"]
    motivo: str | None = Field(None, max_length=300)


def _harness(request: Request) -> Harness:
    h = getattr(request.app.state, "harness", None)
    if not get_settings().agente_activo or h is None or not hay_modelo_real():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "El agente no está disponible")
    return h


def _contexto(db: Session, usuario: Usuario, payload: dict, paciente_id: str | None) -> ContextoAgente:
    """Arma el contexto que fija el harness. Lanza 403/404 antes de llamar al modelo."""
    rol = payload.get("rol") or ""
    if rol in ROLES_SIN_AGENTE:
        registrar(db, usuario, "agente", None, "agente_chat", resultado="denegado")
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu perfil usa el asistente de consulta, no el agente")
    alias = None
    if paciente_id:
        from app.services import rag   # noqa: PLC0415
        acc = ContextoAcceso(usuario, payload, tiene_permiso(payload.get("permisos", []), "paciente", "read"),
                             "paciente", "read", db, None)
        if acc.alcance is None:
            registrar(db, usuario, "agente", None, "agente_chat", resultado="denegado")
            db.commit()
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu perfil no consulta pacientes")
        p = db.get(Paciente, paciente_id)
        if p is None or not p.activo:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Paciente no encontrado")
        acc.verificar_registro(p)          # 403 si no está a su cargo (y queda auditado)
        alias = rag.seudonimo(paciente_id)
    return ContextoAgente(usuario_id=usuario.id, username=usuario.username, rol=rol, payload=payload,
                          paciente_id=paciente_id, paciente_alias=alias)


@router.get("/estado", summary="¿Puedo usar el agente?")
def estado(request: Request, payload: dict = Depends(get_token_payload)):
    h = getattr(request.app.state, "harness", None)
    disponible = bool(get_settings().agente_activo and h is not None and hay_modelo_real())
    return {"disponible": disponible, "puede_usar": disponible and payload.get("rol") not in ROLES_SIN_AGENTE,
            "memoria": getattr(h, "memoria", None), "modelos": modelos_del_agente() if disponible else []}


@router.post("/chat", summary="Conversar con Lati (agente con herramientas)")
async def chat(datos: Mensaje, request: Request, payload: dict = Depends(get_token_payload),
               usuario: Usuario = Depends(get_usuario_actual), db: Session = Depends(get_db)):
    ctx = await run_in_threadpool(_contexto, db, usuario, payload, datos.paciente_id)      # 403 contable/paciente
    await run_in_threadpool(incidentes.exigir_sin_bloqueo, db, usuario, request, "agente")  # 423 si está bloqueado
    reglas = incidentes.detectar(datos.mensaje)
    if reglas:              # R25: la pregunta intenta manipular al agente → no llega al modelo
        def _incidente():
            inc = incidentes.registrar_incidente(db, usuario, request, "agente", reglas)
            db.commit()
            return inc
        inc = await run_in_threadpool(_incidente)
        return {"estado": "rechazado_por_seguridad", "respuesta": incidentes.NEGATIVA, "citas": [], "fuentes": [],
                "herramientas": [], "payload_ids": [], "conversacion": datos.conversacion_id, **inc}
    harness = _harness(request)                                                            # 503 si no hay modelo
    r = await harness.atender(ctx, datos.mensaje, datos.conversacion_id)
    await asyncio.to_thread(_auditar, ctx, "agente_chat",
                            {"estado": r["estado"], "herramientas": r.get("herramientas", []),
                             "modelo": r.get("generador")})
    return r


@router.post("/decidir", summary="Aprobar o rechazar la acción propuesta por el agente")
async def decidir(datos: Decision, request: Request, payload: dict = Depends(get_token_payload),
                  usuario: Usuario = Depends(get_usuario_actual), db: Session = Depends(get_db)):
    ctx = await run_in_threadpool(_contexto, db, usuario, payload, datos.paciente_id)
    harness = _harness(request)
    if tiene_permiso(ctx.permisos, "remision", "create") is None:
        await asyncio.to_thread(_auditar, ctx, f"agente_{'aprobar' if datos.decision == 'approve' else 'rechazar'}",
                                {}, "denegado")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu perfil no aprueba remisiones")
    r = await harness.decidir(ctx, datos.conversacion_id, datos.decision, datos.motivo)
    await asyncio.to_thread(_auditar, ctx, f"agente_{'aprobar' if datos.decision == 'approve' else 'rechazar'}",
                            {"estado": r["estado"], "motivo": datos.motivo})
    return r


# ------------------------------------------------------------------ R24: qué recibió el LLM
@router.get("/payloads", summary="Lo que recibió el LLM en mis conversaciones (seudonimizado)")
def payloads(conversacion: str | None = None, limite: int = 20, payload: dict = Depends(get_token_payload),
             usuario: Usuario = Depends(get_usuario_actual), db: Session = Depends(get_db)):
    from sqlalchemy import text  # noqa: PLC0415
    es_admin = payload.get("rol") == "admin"
    filas = db.execute(text("""
        SELECT id, usuario_id, conversacion, paciente_alias, modelo, huella, created_at FROM agente_payloads
        WHERE (:admin OR usuario_id = :u) AND (CAST(:c AS text) IS NULL OR conversacion = :c)
        ORDER BY id DESC LIMIT :n"""), {"admin": es_admin, "u": usuario.id, "c": conversacion,
                                        "n": max(1, min(limite, 100))}).mappings().all()
    return {"payloads": [dict(f) for f in filas]}


@router.get("/payloads/{payload_id}", summary="El JSON exacto que recibió el LLM en una llamada (R24)")
def payload_llm(payload_id: int, request: Request, payload: dict = Depends(get_token_payload),
                usuario: Usuario = Depends(get_usuario_actual), db: Session = Depends(get_db)):
    from sqlalchemy import text  # noqa: PLC0415
    fila = db.execute(text("SELECT * FROM agente_payloads WHERE id = :i"), {"i": payload_id}).mappings().first()
    if fila is None or (payload.get("rol") != "admin" and fila["usuario_id"] != usuario.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese payload o no es suyo")
    registrar(db, usuario, "agente", payload_id, "ver_payload_llm", request=request)
    db.commit()
    return dict(fila)
