"""
Consentimiento para compartir datos con otra institución (proyecto final, R32; Ley 1581/2012).

Sin un consentimiento VIGENTE del paciente para la institución destino, Pulso no envía ninguna
remisión hacia ella (lo verifica POST /remisiones/externas). El consentimiento es por institución y
por finalidad (remisión); revocarlo no lo borra: queda la fecha y quién lo hizo.

  GET    /pacientes/{id}/consentimientos                 los del paciente (?formato=fhir → Consent R4)
  POST   /pacientes/{id}/consentimientos                 registrarlo (el clínico a cargo o el propio paciente)
  DELETE /pacientes/{id}/consentimientos/{consentimiento_id}   revocarlo

El contable y quien no tenga acceso a ese paciente reciben 403 (y queda en la auditoría).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.deps import ContextoAcceso, RequierePermiso, get_db, tiene_permiso
from app.models import Paciente
from app.services.auditoria import registrar

router = APIRouter(tags=["Consentimiento"])

SYS_CONSENT_SCOPE = "http://terminology.hl7.org/CodeSystem/consentscope"
SYS_CONSENT_CAT = "http://loinc.org"
SYS_ACTREASON = "http://terminology.hl7.org/CodeSystem/v3-ActReason"


def _paciente(db: Session, ctx: ContextoAcceso, paciente_id: str) -> Paciente:
    p = db.get(Paciente, paciente_id)
    if p is None or not p.activo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paciente no encontrado")
    ctx.verificar_registro(p)          # 403 si no es su paciente (o, para el rol paciente, si no es él)
    return p


def _puede_registrar(ctx: ContextoAcceso) -> bool:
    """Lo registra el clínico que remite (permiso de remitir) o el propio paciente sobre sí mismo."""
    return bool(tiene_permiso(ctx.payload.get("permisos", []), "remision", "create")) or ctx.alcance == "self"


def _vista(f: dict) -> dict:
    return {"id": f["id"], "socio_id": f["socio_id"], "socio": f["socio"], "socio_codigo": f["codigo"],
            "finalidad": f["finalidad"], "soporte": f["soporte"], "otorgado_at": f["otorgado_at"],
            "revocado_at": f["revocado_at"], "vigente": f["revocado_at"] is None}


def a_fhir(f: dict, mrn: str | None) -> dict:
    """El consentimiento como recurso Consent de FHIR R4."""
    return {
        "resourceType": "Consent", "id": str(f["id"]),
        "status": "active" if f["revocado_at"] is None else "inactive",
        "scope": {"coding": [{"system": SYS_CONSENT_SCOPE, "code": "patient-privacy", "display": "Privacy Consent"}]},
        "category": [{"coding": [{"system": SYS_CONSENT_CAT, "code": "59284-0", "display": "Patient Consent"}]}],
        "patient": {"identifier": {"system": "http://hsanrafael.co/fhir/identifier/mrn", "value": mrn}} if mrn else None,
        "dateTime": f["otorgado_at"].isoformat() if f["otorgado_at"] else None,
        "policyRule": {"text": "Ley 1581 de 2012: autorización para transferir datos sensibles a otra institución"},
        "provision": {
            "type": "permit",
            "period": {"start": f["otorgado_at"].isoformat() if f["otorgado_at"] else None,
                       **({"end": f["revocado_at"].isoformat()} if f["revocado_at"] else {})},
            "actor": [{"role": {"text": "Institución destino"}, "reference": {"display": f["socio"]}}],
            "purpose": [{"system": SYS_ACTREASON, "code": "TREAT", "display": "treatment"}],
        },
    }


def _filas(db: Session, paciente_id: str) -> list[dict]:
    return [dict(f) for f in db.execute(text("""
        SELECT c.*, s.nombre AS socio, s.codigo FROM consentimientos c JOIN interop_socios s ON s.id = c.socio_id
        WHERE c.paciente_id = :p ORDER BY c.otorgado_at DESC"""), {"p": paciente_id}).mappings().all()]


@router.get("/pacientes/{paciente_id}/consentimientos", summary="Consentimientos del paciente para compartir datos")
def listar(paciente_id: str, formato: str = Query("pulso", pattern="^(pulso|fhir)$"),
           ctx: ContextoAcceso = Depends(RequierePermiso("paciente", "read")), db: Session = Depends(get_db)):
    p = _paciente(db, ctx, paciente_id)
    filas = _filas(db, paciente_id)
    if formato == "fhir":
        return {"resourceType": "Bundle", "type": "searchset", "total": len(filas),
                "entry": [{"resource": a_fhir(f, str(p.uuid))} for f in filas]}
    socios = db.execute(text("SELECT id, codigo, nombre FROM interop_socios WHERE activo ORDER BY nombre")).mappings().all()
    vigentes = {f["socio_id"] for f in filas if f["revocado_at"] is None}
    return {"consentimientos": [_vista(f) for f in filas],
            "instituciones": [{**dict(s), "consentimiento": s["id"] in vigentes} for s in socios],
            "puede_registrar": _puede_registrar(ctx)}


class ConsentimientoCrear(BaseModel):
    socio: str = Field(..., description="Código o id de la institución destino")
    soporte: str = Field("Autorización verbal del paciente registrada por el clínico", max_length=200,
                         description="Cómo se obtuvo: firma, formato físico, autorización verbal con testigo...")


@router.post("/pacientes/{paciente_id}/consentimientos", status_code=status.HTTP_201_CREATED,
             summary="Registrar el consentimiento para compartir con una institución")
def registrar_consentimiento(paciente_id: str, datos: ConsentimientoCrear, request: Request,
                             ctx: ContextoAcceso = Depends(RequierePermiso("paciente", "read")),
                             db: Session = Depends(get_db)):
    p = _paciente(db, ctx, paciente_id)
    if not _puede_registrar(ctx):
        registrar(db, ctx.usuario, "consentimiento", None, "create", resultado="denegado", request=request)
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Su perfil no registra consentimientos")
    socio = db.execute(text("SELECT id, nombre FROM interop_socios WHERE activo AND (codigo = :v OR CAST(id AS text) = :v)"),
                       {"v": datos.socio}).first()
    if socio is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esa institución no está registrada como socia")
    fila = db.execute(text("""
        INSERT INTO consentimientos (paciente_id, socio_id, soporte, registrado_por)
        VALUES (:p, :s, :so, :u) ON CONFLICT DO NOTHING RETURNING id"""),
        {"p": p.documento_bidx, "s": socio[0], "so": datos.soporte.strip(), "u": ctx.usuario.id}).first()
    if fila is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya hay un consentimiento vigente para {socio[1]}")
    registrar(db, ctx.usuario, "consentimiento", fila[0], "create", request=request,
              valor_nuevo={"socio_id": socio[0], "finalidad": "remision"})
    db.commit()
    return {"id": fila[0], "socio": socio[1], "vigente": True}


@router.delete("/pacientes/{paciente_id}/consentimientos/{consentimiento_id}", summary="Revocar un consentimiento")
def revocar(paciente_id: str, consentimiento_id: int, request: Request,
            ctx: ContextoAcceso = Depends(RequierePermiso("paciente", "read")), db: Session = Depends(get_db)):
    p = _paciente(db, ctx, paciente_id)
    if not _puede_registrar(ctx):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Su perfil no gestiona consentimientos")
    fila = db.execute(text("""UPDATE consentimientos SET revocado_at = now(), revocado_por = :u
                              WHERE id = :i AND paciente_id = :p AND revocado_at IS NULL RETURNING id"""),
                      {"u": ctx.usuario.id, "i": consentimiento_id, "p": p.documento_bidx}).first()
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay un consentimiento vigente con ese id")
    registrar(db, ctx.usuario, "consentimiento", consentimiento_id, "revocar", request=request)
    db.commit()
    return {"id": consentimiento_id, "vigente": False}
