"""
Rendimiento del sistema, para el admin.

  GET /admin/rendimiento   API (latencia y errores), seguridad (logins,
                           bloqueos, accesos denegados), uso por perfil,
                           servicios conectados y métricas del modelo de IA.
  GET /admin/auditoria/cadena      Los últimos registros de la auditoría con sus huellas.
  GET /admin/auditoria/verificar   Recalcula la cadena de huellas y dice si está íntegra
                                   (docs/SEGURIDAD.md §7.5).

Todo es agregado: no aparece ningún paciente. Solo quien puede leer la
auditoría (admin) lo consulta.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import ContextoAcceso, RequierePermiso, get_db
from app.services import auditoria_cadena, metricas
from app.services.auditoria import registrar

router = APIRouter(prefix="/admin", tags=["Administración"])
settings = get_settings()


def _tabla(db: Session, nombre: str) -> bool:
    return bool(db.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{nombre}"}).scalar())


@router.get("/rendimiento", summary="Cómo está funcionando el sistema (solo admin)")
def rendimiento(ctx: ContextoAcceso = Depends(RequierePermiso("auditoria", "read")), db: Session = Depends(get_db)):
    seg = dict(db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE operacion = 'login' AND resultado = 'exito')            AS logins_ok,
               COUNT(*) FILTER (WHERE operacion IN ('login', 'login_fallido') AND resultado <> 'exito') AS logins_fallidos,
               COUNT(*) FILTER (WHERE resultado = 'denegado' AND operacion NOT IN ('login', 'login_fallido'))
                                                                                             AS accesos_denegados,
               COUNT(*) FILTER (WHERE operacion = 'fhir_evento' OR entidad_tipo = 'fhir_evento'
                                   OR endpoint LIKE '%/fhir/notificacion%')                  AS eventos_fhir,
               COUNT(*)                                                                      AS eventos
        FROM log_auditoria WHERE timestamp > NOW() - INTERVAL '24 hours'
    """)).mappings().first())
    seg["cuentas_bloqueadas"] = db.execute(text(
        "SELECT COUNT(*) FROM usuarios WHERE activo AND bloqueado_hasta > NOW()")).scalar()
    seg["sesiones_activas"] = db.execute(text(
        "SELECT COUNT(*) FROM sesiones WHERE revocado_at IS NULL AND expira_at > NOW()")).scalar()
    denegados = [dict(r) for r in db.execute(text("""
        SELECT COALESCE(rol_codigo, '—') AS rol, entidad_tipo AS recurso, operacion, COUNT(*) AS n
        FROM log_auditoria WHERE resultado = 'denegado' AND timestamp > NOW() - INTERVAL '24 hours'
        GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 8
    """)).mappings()]
    uso = [dict(r) for r in db.execute(text("""
        SELECT r.codigo AS rol, r.nombre, COUNT(u.id) AS usuarios,
               COUNT(u.id) FILTER (WHERE u.ultimo_acceso > NOW() - INTERVAL '24 hours') AS activos_24h
        FROM roles r LEFT JOIN usuarios u ON u.rol_id = r.id AND u.activo
        GROUP BY r.codigo, r.nombre HAVING COUNT(u.id) > 0 ORDER BY activos_24h DESC, usuarios DESC
    """)).mappings()]
    acciones = [dict(r) for r in db.execute(text("""
        SELECT entidad_tipo AS recurso, operacion, COUNT(*) AS n
        FROM log_auditoria WHERE timestamp > NOW() - INTERVAL '24 hours' AND resultado = 'exito'
        GROUP BY 1, 2 ORDER BY n DESC LIMIT 10
    """)).mappings()]

    # IA (módulo de Esteban): lo que el servicio ml publique y el estado de los reportes.
    ia = {"servicio": "no disponible", "metricas": None, "reportes": None}
    try:
        r = httpx.get(f"{settings.ml_url}/metricas", timeout=2)
        ia["servicio"] = "ok" if r.status_code == 200 else f"responde {r.status_code}"
        if r.status_code == 200:
            ia["metricas"] = r.json()
    except Exception:  # noqa: BLE001
        pass
    for tabla in ("reportes_ia", "ia_reportes"):
        if _tabla(db, tabla):
            ia["reportes"] = {f["estado"]: f["n"] for f in db.execute(text(
                f"SELECT estado, COUNT(*) AS n FROM {tabla} GROUP BY estado")).mappings()}
            break

    return {"api": metricas.resumen(), "seguridad": seg, "denegados": denegados,
            "uso_por_perfil": uso, "acciones_24h": acciones, "ia": ia}


@router.get("/auditoria/cadena", summary="Últimos registros de la auditoría con sus huellas (solo admin)")
def cadena_auditoria(limite: int = Query(20, ge=1, le=200),
                     ctx: ContextoAcceso = Depends(RequierePermiso("auditoria", "read")),
                     db: Session = Depends(get_db)):
    return {"registros": auditoria_cadena.ultimos_registros(db, limite)}


@router.get("/auditoria/verificar", summary="Verificar que la cadena de auditoría no fue alterada (solo admin)")
def verificar_auditoria(ultimos: int | None = Query(None, ge=1, le=5_000_000,
                                                    description="Revisar solo los últimos N registros"),
                        ctx: ContextoAcceso = Depends(RequierePermiso("auditoria", "read")),
                        db: Session = Depends(get_db)):
    """
    Recalcula cada huella con la clave de auditoría. Si algún registro se editó, se borró o se movió,
    dice cuál es el primero roto. Guarde `ultima_huella` y `total` aparte: así también se detecta que
    alguien borre los últimos registros. La verificación queda registrada en la propia auditoría.
    """
    r = auditoria_cadena.verificar(db, ultimos)
    registrar(db, ctx.usuario, "auditoria", None, "verificar_cadena",
              valor_nuevo={"integra": r["integra"], "total": r["total"], "problemas": len(r["problemas"])},
              resultado="exito" if r["integra"] else "error", request=ctx.request)
    db.commit()
    return r
