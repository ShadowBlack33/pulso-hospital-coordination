"""
Remisiones entre instituciones (proyecto final, R29-R32). Contrato completo en docs/INTEROP.md.

Para el SISTEMA socio (credenciales de sistema, nunca de un humano; no abren la API clínica):
  GET  /interop/metadata        qué aceptamos y cómo autenticarse (público, sin datos)
  POST /interop/Bundle          nos remiten un paciente (Patient + ServiceRequest + Observation + DiagnosticReport)
  POST /interop/Task            nos responden una remisión que les enviamos
  GET  /interop/Task/{id}       consultan el estado de una remisión que nos enviaron

Para los usuarios de Pulso (JWT y permisos de siempre):
  GET  /interop/socios                    instituciones a las que se puede remitir (con el consentimiento del paciente)
  POST /remisiones/externas               el médico remite a otra institución
  POST /remisiones/{id}/reenviar          reintentar el envío si el socio no respondió
  POST /remisiones/{id}/rechazar          el especialista rechaza una remisión QUE LLEGÓ de otra institución
  GET  /admin/interop/socios              (admin) socios configurados, sin secretos
  PATCH /admin/interop/socios/{codigo}    (admin) cambiar la URL del socio sin redesplegar

Credenciales del socio: Authorization: Bearer <token de /oauth/token> o X-API-Key: <client_id>.<secreto>;
firma HMAC solo si ese cliente la exige. Errores: OperationOutcome (400/401/403/404/409/422).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.deps import ContextoAcceso, RequierePermiso, SessionLocal, get_db
from app.models import Paciente
from app.services import interop as I
from app.services import reactores
from app.services import remisiones_externas as RX
from app.services.auditoria import registrar

log = logging.getLogger("pulso.interop")
router = APIRouter(tags=["Interoperabilidad: remisiones entre instituciones"])

FHIR_JSON = "application/fhir+json"
MAX_BUNDLE = 1024 * 1024          # 1 MB: un Bundle de remisión pesa unos pocos KB
RUTA_BUNDLE, RUTA_TASK = "/interop/Bundle", "/interop/Task"


def _outcome(http: int, detalle: str, tipo: str, errores: list[str]) -> JSONResponse:
    cuerpo = {"resourceType": "OperationOutcome",
              "issue": [{"severity": "error", "code": tipo, "diagnostics": d} for d in (errores or [detalle])]}
    cab = {"WWW-Authenticate": 'Bearer realm="pulso-interop"'} if http == 401 else None
    return JSONResponse(cuerpo, status_code=http, media_type=FHIR_JSON, headers=cab)


def _rechazo(client_id: str | None, ruta: str, e: I.ErrorInterop, ip: str | None) -> None:
    """Audita el rechazo y avisa al admin, en una sesión aparte (la de la petición se deshizo)."""
    try:
        with SessionLocal() as db:
            registrar(db, None, "interop", None, "rechazo_externo", resultado="denegado" if e.http in (401, 403) else "error",
                      valor_nuevo={"origen": "externo", "client_id": client_id, "ruta": ruta, "http": e.http,
                                   "motivo": e.detalle[:120], "problemas": e.errores[:5], "ip": ip})
            if e.http not in (401, 409):          # credencial mala o duplicado: no hace falta molestar al admin
                reactores.al_interop_rechazo(db, client_id, ruta, e.http, "; ".join(e.errores[:3]) or e.detalle)
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo auditar el rechazo externo")


async def _entrada(request: Request, ruta: str, scope: str, procesar) -> JSONResponse:
    """Esqueleto común de las rutas de entrada: tamaño, credencial, JSON, proceso atómico y rechazo auditado."""
    cuerpo = await request.body()
    if len(cuerpo) > MAX_BUNDLE:
        return _outcome(413, "Cuerpo demasiado grande", "too-costly", [f"El cuerpo supera {MAX_BUNDLE // 1024} KB"])
    cabeceras = {k: request.headers.get(v.lower()) for k, v in I.CABECERAS.items()}
    quien: dict = {}

    def _trabajo():
        with SessionLocal() as db:
            try:
                cid = I.autenticar_entrada(db, request.headers.get("authorization"), request.headers.get("x-api-key"),
                                           cabeceras, "POST", ruta, cuerpo, scope, quien)
                quien["client_id"] = cid
                try:
                    recurso = json.loads(cuerpo)
                except ValueError:
                    raise I.ErrorInterop(400, "El cuerpo no es JSON", "structure", ["El cuerpo no es JSON válido"]) from None
                http, r = procesar(db, cid, recurso, request)
                db.commit()
                return http, r
            except Exception:
                db.rollback()          # un rechazo no deja datos a medias (R30)
                raise

    try:
        http, r = await run_in_threadpool(_trabajo)
        cab = {"Location": f"/interop/Task/{r['id']}"} if http == 201 and r.get("resourceType") == "Task" else None
        return JSONResponse(r, status_code=http, media_type=FHIR_JSON, headers=cab)
    except I.ErrorInterop as e:
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
        await run_in_threadpool(_rechazo, quien.get("client_id"), ruta, e, (ip or "").split(",")[0].strip() or None)
        return _outcome(e.http, e.detalle, e.tipo, e.errores)
    except RuntimeError as e:
        log.error("interop %s: %s", ruta, e)
        return _outcome(503, "Interoperabilidad no configurada", "exception", [])


@router.get("/interop/metadata", summary="Qué acepta Pulso y cómo autenticarse (para cualquier equipo)")
def metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "resourceType": "CapabilityStatement", "status": "active", "kind": "instance", "fhirVersion": "4.0.1",
        "date": datetime.now(timezone.utc).date().isoformat(), "publisher": RX.INSTITUCION,
        "format": ["application/fhir+json", "application/json"],
        "implementation": {"description": "Pulso · puerta de interoperabilidad entre instituciones", "url": base},
        "seguridad": {
            "token": {"ruta": "/oauth/token", "grant_type": "client_credentials", "parametros": "client_id, client_secret, scope",
                      "formato": "application/x-www-form-urlencoded (o HTTP Basic)", "expira_s": 600},
            "api_key": {"cabecera": "X-API-Key", "formato": "<client_id>.<client_secret>"},
            "firma_hmac": {"obligatoria": "solo si se acordó con su institución",
                           "cabeceras": list(I.CABECERAS.values()),
                           "cadena": "v1\\n<timestamp>\\n<nonce>\\nPOST\\n<ruta>\\n<sha256 hex del cuerpo>"},
            "scopes": sorted(I.SCOPES_VALIDOS),
            "credenciales": "Se piden al equipo de Pulso por un canal privado; sirven solo para estas rutas.",
        },
        "rest": [{"mode": "server", "resource": [
            {"type": "Bundle", "ruta": RUTA_BUNDLE, "metodo": "POST", "scope": I.SCOPE_REMISION,
             "documentation": "Remisión: Bundle (transaction o collection) con 1 Patient (documento con tipo, nombre, "
                              "birthDate, gender), 1 ServiceRequest (status active, intent order, code SNOMED, subject al "
                              "Patient, reasonCode, priority, identifier), Observation con LOINC y UCUM y, opcional, 1 "
                              "DiagnosticReport con status final. Responde 201 con un Task y Location /interop/Task/{id}."},
            {"type": "Task", "ruta": RUTA_TASK, "metodo": "POST", "scope": I.SCOPE_TASK,
             "documentation": f"Respuesta a una remisión nuestra: status accepted|rejected|completed y basedOn con el "
                              f"identifier ({RX.SYS_REMISION_EXT}) del ServiceRequest que le enviamos."},
            {"type": "Task", "ruta": "/interop/Task/{id}", "metodo": "GET", "scope": I.SCOPE_REMISION,
             "documentation": "Estado de una remisión que usted nos envió (requested, accepted, rejected, completed)."},
            {"type": "Observation", "ruta": I.RUTA_OBSERVATION, "metodo": "POST", "scope": I.SCOPE_OBSERVATION,
             "documentation": "Un resultado suelto para un paciente identificado con el id federado."},
        ]}],
        "errores": "OperationOutcome con un issue por problema: 400 JSON inválido, 401 credencial, 403 scope, "
                   "404 no existe, 409 duplicado o sin especialista, 422 recurso inválido.",
    }


@router.post(RUTA_BUNDLE, summary="Recibir una remisión de otra institución (Bundle FHIR)")
async def recibir_bundle(request: Request):
    return await _entrada(request, RUTA_BUNDLE, I.SCOPE_REMISION, RX.recibir_bundle)


@router.post(RUTA_TASK, summary="Recibir la respuesta del socio a una remisión nuestra (Task)")
async def recibir_task(request: Request):
    return await _entrada(request, RUTA_TASK, I.SCOPE_TASK, RX.recibir_task)


@router.get("/interop/Task/{tarea_id}", summary="Estado de una remisión que el socio nos envió")
async def consultar_task(tarea_id: str, request: Request):
    def _trabajo():
        with SessionLocal() as db:
            cid = _cliente_sin_firma(db, request)          # un GET no tiene cuerpo que firmar
            r = db.execute(text("""SELECT * FROM remisiones WHERE direccion = 'entrante' AND client_id = :c
                                   AND (CAST(id AS text) = :t OR id_externo = :t) ORDER BY id LIMIT 1"""),
                           {"c": cid, "t": tarea_id}).mappings().first()
            if r is None:
                raise I.ErrorInterop(404, "No existe una remisión suya con ese id", "not-found")
            return RX.task_de(db, dict(r))
    try:
        return JSONResponse(await run_in_threadpool(_trabajo), media_type=FHIR_JSON)
    except I.ErrorInterop as e:
        return _outcome(e.http, e.detalle, e.tipo, e.errores)


def _cliente_sin_firma(db, request: Request) -> str:
    """Una consulta GET no tiene cuerpo que firmar: basta la credencial (token o API key) con su scope."""
    autorizacion, api_key = request.headers.get("authorization"), request.headers.get("x-api-key")
    if autorizacion and autorizacion.lower().startswith("bearer "):
        return I.leer_token(autorizacion, I.SCOPE_REMISION)["sub"]
    if api_key:
        cid, _, secreto = api_key.partition(".")
        cliente = I.autenticar_cliente(db, cid, secreto)
        if I.SCOPE_REMISION not in cliente["scopes"].split():
            raise I.ErrorInterop(403, f"La credencial no tiene el scope {I.SCOPE_REMISION}", "forbidden")
        return cid
    raise I.ErrorInterop(401, "Falta la credencial del sistema", "login")


# =============================================================== lado de los usuarios de Pulso
@router.get("/interop/socios", summary="Instituciones a las que se puede remitir")
def socios(paciente_id: str | None = Query(None, description="Para saber si el paciente ya dio su consentimiento"),
           ctx: ContextoAcceso = Depends(RequierePermiso("remision", "create")), db: Session = Depends(get_db)):
    if paciente_id:
        p = db.get(Paciente, paciente_id)
        if p is None or not p.activo:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Paciente no encontrado")
        ctx.verificar_registro(p)
    filas = db.execute(text("SELECT * FROM interop_socios WHERE activo ORDER BY nombre")).mappings().all()
    salida = []
    for f in filas:
        c = RX.consentimiento_vigente(db, paciente_id, f["id"]) if paciente_id else None
        salida.append({"id": f["id"], "codigo": f["codigo"], "nombre": f["nombre"],
                       "consentimiento": bool(c), "consentimiento_id": c["id"] if c else None})
    return {"socios": salida}


class RemisionExterna(BaseModel):
    paciente_id: str
    socio: str = Field(..., description="Código o id de la institución destino")
    motivo: str = Field(..., min_length=5, max_length=1000)
    prioridad: str = Field("prioritaria", pattern="^(rutinaria|prioritaria|urgente)$")
    especialidad_id: int | None = Field(None, description="Especialidad que se pide (va como SNOMED en performerType)")
    reporte_id: int | None = Field(None, description="Reporte de IA APROBADO que se adjunta (opcional)")
    observaciones_ids: list[int] | None = Field(None, max_length=20,
                                                description="Resultados a enviar; si no se dan, el último de cada examen de 30 días")


def _socio(db, valor: str) -> dict:
    fila = db.execute(text("SELECT * FROM interop_socios WHERE activo AND (codigo = :v OR CAST(id AS text) = :v)"),
                      {"v": str(valor)}).mappings().first()
    if fila is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esa institución no está registrada como socia")
    return dict(fila)


@router.post("/remisiones/externas", status_code=status.HTTP_201_CREATED, summary="Remitir un paciente a otra institución")
def remitir_externa(datos: RemisionExterna, request: Request,
                    ctx: ContextoAcceso = Depends(RequierePermiso("remision", "create")), db: Session = Depends(get_db)):
    p = db.get(Paciente, datos.paciente_id)
    if p is None or not p.activo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paciente no encontrado")
    ctx.verificar_registro(p)
    socio = _socio(db, datos.socio)
    # R32: sin consentimiento vigente para ESA institución, no sale nada.
    if not RX.consentimiento_vigente(db, p.documento_bidx, socio["id"]):
        registrar(db, ctx.usuario, "remision", None, "envio_externo", resultado="denegado", request=request,
                  valor_nuevo={"socio": socio["codigo"], "motivo": "sin_consentimiento"})
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"El paciente no tiene consentimiento registrado para compartir sus datos con {socio['nombre']} "
                            "(Ley 1581/2012). Regístrelo antes de remitir.")
    if datos.reporte_id is not None:
        rep = db.execute(text("SELECT estado, paciente_id FROM reportes_ia WHERE id = :i"), {"i": datos.reporte_id}).first()
        if rep is None or rep[1] != p.documento_bidx:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese reporte de IA no es de este paciente")
        if rep[0] != "aprobado":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Solo se remite un reporte de IA APROBADO; este está {rep[0]}")
    fila = db.execute(text("""
        INSERT INTO remisiones (paciente_id, medico_id, remitido_por, especialista_id, especialidad_id, tipo_solicitud,
                                motivo, prioridad, direccion, socio_id, estado_envio, datos_externos)
        VALUES (:p, :m, :u, NULL, :e, 'valoracion', :mo, :pr, 'saliente', :s, 'pendiente', CAST(:d AS jsonb)) RETURNING id"""),
        {"p": p.documento_bidx, "m": ctx.payload.get("personal_id"), "u": ctx.usuario.id, "e": datos.especialidad_id,
         "mo": datos.motivo.strip(), "pr": datos.prioridad, "s": socio["id"],
         "d": json.dumps({"socio": socio["nombre"], "socio_codigo": socio["codigo"], "reporte_id": datos.reporte_id,
                          "observaciones_ids": datos.observaciones_ids})}).first()
    registrar(db, ctx.usuario, "remision", fila[0], "create", request=request,
              valor_nuevo={"direccion": "saliente", "socio": socio["codigo"], "prioridad": datos.prioridad,
                           "reporte_id": datos.reporte_id})
    db.commit()                      # la remisión queda aunque el socio no responda: se puede reenviar
    envio = RX.enviar(db, fila[0])
    db.commit()
    r = db.execute(text("SELECT * FROM remisiones WHERE id = :i"), {"i": fila[0]}).mappings().first()
    return {"id": fila[0], "estado": r["estado"], "estado_envio": r["estado_envio"], "detalle_envio": r["detalle_envio"],
            "socio": socio["nombre"], **{k: envio[k] for k in ("observaciones", "reporte")}}


@router.post("/remisiones/{remision_id}/reenviar", summary="Reintentar el envío de una remisión a otra institución")
def reenviar(remision_id: int, request: Request, ctx: ContextoAcceso = Depends(RequierePermiso("remision", "create")),
             db: Session = Depends(get_db)):
    r = db.execute(text("SELECT * FROM remisiones WHERE id = :i AND direccion = 'saliente'"), {"i": remision_id}).mappings().first()
    if r is None or (ctx.alcance != "all" and r["remitido_por"] != ctx.usuario.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remisión no encontrada")
    if r["estado_envio"] == "enviada" and r["estado"] != "pendiente":
        raise HTTPException(status.HTTP_409_CONFLICT, "El socio ya respondió esta remisión")
    if not RX.consentimiento_vigente(db, r["paciente_id"], r["socio_id"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "El consentimiento para esa institución fue revocado")
    envio = RX.enviar(db, remision_id)
    db.commit()
    return {"id": remision_id, **envio}


class Rechazo(BaseModel):
    motivo: str = Field(..., min_length=5, max_length=1000)


@router.post("/remisiones/{remision_id}/rechazar", summary="Rechazar una remisión que llegó de otra institución")
def rechazar(remision_id: int, datos: Rechazo, request: Request, tareas: BackgroundTasks,
             ctx: ContextoAcceso = Depends(RequierePermiso("remision", "update")), db: Session = Depends(get_db)):
    r = db.execute(text("SELECT * FROM remisiones WHERE id = :i"), {"i": remision_id}).mappings().first()
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remisión no encontrada")
    if r["direccion"] != "entrante":
        # Regla del Corte 2: una remisión interna no se rechaza. Solo las de otra institución.
        raise HTTPException(status.HTTP_409_CONFLICT, "Solo se pueden rechazar las remisiones de otra institución")
    if ctx.alcance != "all" and r["especialista_id"] != ctx.payload.get("personal_id"):
        registrar(db, ctx.usuario, "remision", remision_id, "update", resultado="denegado", request=request)
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el especialista destinatario puede rechazarla")
    fila = db.execute(text("""UPDATE remisiones SET estado = 'rechazada', respuesta = :m, cerrada_at = now(), cerrada_por = :u
                              WHERE id = :i AND estado = 'pendiente' RETURNING *"""),
                      {"m": datos.motivo.strip(), "u": ctx.usuario.id, "i": remision_id}).mappings().first()
    if fila is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La remisión ya está {r['estado']}")
    registrar(db, ctx.usuario, "remision", remision_id, "update", request=request,
              valor_anterior={"estado": "pendiente"}, valor_nuevo={"estado": "rechazada", "origen": "externo"})
    db.commit()
    tareas.add_task(RX.responder_socio, remision_id)          # el socio recibe el Task "rejected"
    return {"id": remision_id, "estado": "rechazada"}


# =============================================================== admin
@router.get("/admin/interop/socios", summary="Socios configurados (sin secretos)")
def admin_socios(ctx: ContextoAcceso = Depends(RequierePermiso("usuario", "manage")), db: Session = Depends(get_db)):
    filas = db.execute(text("SELECT * FROM interop_socios ORDER BY nombre")).mappings().all()
    return {"socios": [RX.vista_socio(dict(f)) for f in filas]}


class SocioCambio(BaseModel):
    base_url: str | None = Field(None, pattern=r"^https?://\S+$")
    nombre: str | None = None
    ruta_bundle: str | None = Field(None, pattern=r"^/\S*$")
    ruta_task: str | None = Field(None, pattern=r"^/\S*$")
    ruta_token: str | None = Field(None, pattern=r"^/\S*$")
    activo: bool | None = None


@router.patch("/admin/interop/socios/{codigo}", summary="Cambiar URL o rutas de un socio (su túnel cambió)")
def admin_socio_cambiar(codigo: str, datos: SocioCambio, request: Request,
                        ctx: ContextoAcceso = Depends(RequierePermiso("usuario", "manage")), db: Session = Depends(get_db)):
    cambios = datos.model_dump(exclude_none=True)
    if not RX.actualizar_socio(db, codigo, **cambios):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Socio no encontrado o sin cambios")
    registrar(db, ctx.usuario, "interop", None, "socio_actualizado", request=request, valor_nuevo={"socio": codigo, **cambios})
    db.commit()
    fila = db.execute(text("SELECT * FROM interop_socios WHERE codigo = :c"), {"c": codigo}).mappings().first()
    return RX.vista_socio(dict(fila))
