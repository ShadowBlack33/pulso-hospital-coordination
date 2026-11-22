"""
Panel de seguridad del admin (proyecto final, R25): intentos de manipulación del agente y del asistente
y bloqueos temporales. Solo el admin (permiso usuario:manage).

  GET  /admin/seguridad/incidentes                 últimos intentos (usuario, IP, canal, reglas)
  GET  /admin/seguridad/bloqueos                   bloqueos vigentes y recientes
  POST /admin/seguridad/bloqueos/{id}/levantar     levantar un bloqueo antes de tiempo
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.deps import ContextoAcceso, RequierePermiso, get_db
from app.services.auditoria import registrar

router = APIRouter(prefix="/admin/seguridad", tags=["Seguridad"])


@router.get("/incidentes", summary="Intentos de manipulación del agente o del asistente")
def incidentes(limite: int = 50, ctx: ContextoAcceso = Depends(RequierePermiso("usuario", "manage")),
               db: Session = Depends(get_db)):
    filas = db.execute(text("""
        SELECT i.id, i.usuario_id, u.username, i.ip, i.canal, i.reglas, i.created_at
        FROM incidentes_seguridad i LEFT JOIN usuarios u ON u.id = i.usuario_id
        ORDER BY i.id DESC LIMIT :n"""), {"n": max(1, min(limite, 200))}).mappings().all()
    return {"incidentes": [{**dict(f), "reglas": (f["reglas"] or "").split(",")} for f in filas]}


@router.get("/bloqueos", summary="Bloqueos temporales")
def bloqueos(ctx: ContextoAcceso = Depends(RequierePermiso("usuario", "manage")), db: Session = Depends(get_db)):
    filas = db.execute(text("""
        SELECT b.*, u.username, (b.levantado_at IS NULL AND b.hasta > now()) AS vigente
        FROM bloqueos_temporales b LEFT JOIN usuarios u ON u.id = b.usuario_id
        ORDER BY b.id DESC LIMIT 100""")).mappings().all()
    return {"bloqueos": [dict(f) for f in filas]}


@router.post("/bloqueos/{bloqueo_id}/levantar", summary="Levantar un bloqueo temporal")
def levantar(bloqueo_id: int, request: Request, ctx: ContextoAcceso = Depends(RequierePermiso("usuario", "manage")),
             db: Session = Depends(get_db)):
    fila = db.execute(text("""UPDATE bloqueos_temporales SET levantado_at = now(), levantado_por = :u
                              WHERE id = :i AND levantado_at IS NULL RETURNING id"""),
                      {"u": ctx.usuario.id, "i": bloqueo_id}).first()
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay un bloqueo vigente con ese id")
    registrar(db, ctx.usuario, "seguridad", bloqueo_id, "levantar_bloqueo", request=request)
    db.commit()
    return {"id": bloqueo_id, "levantado": True}
