"""
Puerta de interoperabilidad (Semana 14, docs/INTEROP.md).

  POST /oauth/token           OAuth2 client_credentials → access token de 10 minutos
  POST /interop/Observation   Observation FHIR R4 firmada con HMAC

Códigos: 201 guardada · 202 aceptada, paciente aún sin conciliar · 401 sin credencial, firma mala,
nonce repetido o fuera de ventana · 403 falta el scope · 409 ya recibida · 422 no es una Observation
válida (con OperationOutcome que dice qué corregir) · 503 el servidor no tiene configurado el secreto.

Los rechazos quedan en la auditoría (con una sesión aparte, para que no se pierdan con el rollback).
"""
from __future__ import annotations

import base64
import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.core.deps import SessionLocal
from app.services import interop as I
from app.services.auditoria import registrar

log = logging.getLogger("pulso.interop")
router = APIRouter(tags=["Interoperabilidad"])

MAX_CUERPO = 256 * 1024          # una Observation pesa menos de 2 KB; esto corta el abuso
FHIR_JSON = "application/fhir+json"


def _outcome(http: int, detalle: str, tipo: str, errores: list[str]) -> JSONResponse:
    """OperationOutcome de FHIR R4: la forma estándar de decir «esto salió mal y por qué»."""
    severidad = "error"
    lista = errores or [detalle]
    cuerpo = {"resourceType": "OperationOutcome",
              "issue": [{"severity": severidad, "code": tipo, "diagnostics": d} for d in lista]}
    return JSONResponse(cuerpo, status_code=http, media_type=FHIR_JSON,
                        headers={"WWW-Authenticate": 'Bearer realm="pulso-interop"'} if http == 401 else None)


def _auditar(client_id: str | None, operacion: str, resultado: str, detalle: dict) -> None:
    """Sesión propia: queda registrado aunque la transacción de la petición se deshaga."""
    try:
        with SessionLocal() as db:
            registrar(db, None, "interop", None, operacion, valor_nuevo={"client_id": client_id, **detalle}, resultado=resultado)
            db.commit()
    except Exception:  # noqa: BLE001 -- auditar no puede tumbar la respuesta
        log.exception("No se pudo auditar el mensaje de interoperabilidad")


# --------------------------------------------------------------- /oauth/token
def _token(cuerpo: bytes, basic: str | None) -> tuple[int, dict]:
    campos = {k: v[0] for k, v in parse_qs(cuerpo.decode("utf-8", "replace"), keep_blank_values=True).items()}
    cid, secreto = campos.get("client_id", ""), campos.get("client_secret", "")
    if basic and basic.lower().startswith("basic "):                 # RFC 6749 §2.3.1: también se admite por cabecera
        try:
            cid, _, secreto = base64.b64decode(basic[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            pass
    with SessionLocal() as db:
        try:
            r = I.emitir_token(db, campos.get("grant_type", ""), cid, secreto, campos.get("scope"))
            db.commit()
            _auditar(cid, "token_emitido", "exito", {"scope": r["scope"]})
            return 200, r
        except I.ErrorInterop as e:
            db.rollback()
            _auditar(cid or None, "token_rechazado", "denegado", {"motivo": e.oauth})
            return e.http, {"error": e.oauth, "error_description": e.detalle}


@router.post("/oauth/token", summary="Token OAuth2 (client_credentials)")
async def oauth_token(request: Request):
    cuerpo = await request.body()
    if len(cuerpo) > 4096:
        return JSONResponse({"error": "invalid_request", "error_description": "Cuerpo demasiado grande"}, status_code=413)
    try:
        http, r = await run_in_threadpool(_token, cuerpo, request.headers.get("authorization"))
    except RuntimeError as e:      # falta JWT_SECRET_KEY
        log.error("interop/token: %s", e)
        return JSONResponse({"error": "server_error"}, status_code=503)
    cab = {"Cache-Control": "no-store", "Pragma": "no-cache"}      # RFC 6749 §5.1
    if http == 401:
        cab["WWW-Authenticate"] = 'Basic realm="pulso-interop"'
    return JSONResponse(r, status_code=http, headers=cab)


# --------------------------------------------------------------- /interop/Observation
def _observation(autorizacion: str | None, cabeceras: dict, cuerpo: bytes, quien: dict,
                 api_key: str | None = None) -> tuple[int, dict]:
    with SessionLocal() as db:
        try:
            cid = I.autenticar_entrada(db, autorizacion, api_key, cabeceras, "POST", I.RUTA_OBSERVATION,
                                       cuerpo, I.SCOPE_OBSERVATION, quien)                  # 401 / 403
            try:
                obs = json.loads(cuerpo)
            except ValueError:
                raise I.ErrorInterop(422, "El cuerpo no es JSON", "structure", ["El cuerpo no es JSON válido"]) from None
            errores = I.validar_observation(obs)
            if errores:
                raise I.ErrorInterop(422, "Observation inválida", "invalid", errores)         # 422
            http, r = I.recibir_observation(db, cid, obs)                                     # 201 / 202 / 409
            registrar(db, None, "interop", None, "observation_recibida", resultado="exito",
                      valor_nuevo={"client_id": cid, "id": obs["id"], "estado": r["estado"],
                                   "codigo": next(c["code"] for c in obs["code"]["coding"] if c.get("system") == "http://loinc.org"),
                                   "critica": bool(r.get("alerta_critica"))})
            db.commit()
            return http, r
        except I.ErrorInterop:
            db.rollback()
            raise


@router.post("/interop/Observation", summary="Recibir una Observation FHIR de otro sistema")
async def interop_observation(request: Request):
    cuerpo = await request.body()
    cabeceras = {k: request.headers.get(v.lower()) for k, v in I.CABECERAS.items()}
    if len(cuerpo) > MAX_CUERPO:
        return _outcome(413, "Cuerpo demasiado grande", "too-costly", [f"El cuerpo supera {MAX_CUERPO // 1024} KB"])
    quien: dict = {}          # leer_token anota aquí el cliente apenas el token es auténtico
    try:
        http, r = await run_in_threadpool(_observation, request.headers.get("authorization"), cabeceras, cuerpo, quien,
                                          request.headers.get("x-api-key"))
        return JSONResponse(r, status_code=http, media_type=FHIR_JSON)
    except I.ErrorInterop as e:
        await run_in_threadpool(_auditar, quien.get("client_id"), "rechazo_observation",
                                "denegado" if e.http in (401, 403) else "error",
                                {"http": e.http, "motivo": e.detalle[:120], "tipo": e.tipo})
        return _outcome(e.http, e.detalle, e.tipo, e.errores)
    except RuntimeError as e:
        log.error("interop/Observation: %s", e)
        return _outcome(503, "Interoperabilidad no configurada", "exception", [])
