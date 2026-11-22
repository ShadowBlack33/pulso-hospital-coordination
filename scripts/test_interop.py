#!/usr/bin/env python3
"""
Pruebas de la puerta de interoperabilidad (Semana 14). Necesitan una base con db/05_seguridad_e_interop.sql
aplicado. Crean un cliente de prueba y dos pacientes de prueba y lo borran todo al terminar; se niegan a
correr contra una base que no sea local salvo con --forzar. No usan red: llaman a la API en memoria.

  1. Identificador federado: coincide con el ejemplo de la clase y normaliza el documento.
  2. OAuth2: token de 10 minutos; errores iguales para cliente inexistente y secreto malo.
  3. El token de interoperabilidad no abre la API normal, ni al revés.
  4. Observation: 201 guardada; crítica → alerta; 409 duplicada; 422 con OperationOutcome; 202 sin conciliar.
  5. Firma, nonce y reloj: manipulada, repetida, vieja, sin cabeceras → 401. Sin scope → 403.
  6. Conciliación: el paciente federado aparece después y el pendiente se asocia solo.
  7. Auditoría: los rechazos quedan, sin secretos, y la cadena sigue íntegra.

Uso:  python scripts/test_interop.py
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
for var in ("JWT_SECRET_KEY", "APP_ENCRYPTION_KEY", "APP_BLIND_INDEX_KEY", "IA_SEUDONIMO_KEY"):
    os.environ.setdefault(var, base64.urlsafe_b64encode(b"k" * 32).decode())
os.environ["INTEROP_SECRETO_FEDERADO"] = "secreto-federado-del-curso-solo-demo"      # el de la diapositiva
os.environ["TAREAS_FONDO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.crypto import blind_index  # noqa: E402

host = (get_settings().database_url.split("@")[-1].split("/")[0].split(":")[0] or "").lower()
if host not in {"localhost", "127.0.0.1", "db", "postgres", ""} and "--forzar" not in sys.argv:
    sys.exit(f"Esta prueba escribe datos de prueba; la base es «{host}», no local. Usa --forzar si estás seguro.")

from app.api import pacientes as P  # noqa: E402
from app.core.deps import ContextoAcceso, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Usuario  # noqa: E402
from app.services import auditoria_cadena as C  # noqa: E402
from app.services import interop as I  # noqa: E402

P._publicar_en_fhir = lambda db, p: None
fallos = 0


def check(nombre: str, ok: bool, detalle=None) -> None:
    global fallos
    print(f"  {'OK  ' if ok else 'FALLA'}  {nombre}")
    if not ok:
        fallos += 1
        if detalle is not None:
            print("        ", str(detalle)[:500])


print("\n[1] Identificador federado")
check("Coincide con el ejemplo de la clase (CC|1144123456 → fa0d2121…)",
      I.sid_federado("CC", "1144123456") == "fa0d2121aa10d0c2bb7a5acd8b596f2f")
check("Puntos y espacios del documento no cambian el resultado; el tipo sí cuenta",
      I.sid_federado("cc", "1.144.123.456") == I.sid_federado("CC", " 1144123456 ") != I.sid_federado("TI", "1144123456"))
check("Son 32 caracteres hexadecimales", len(I.sid_federado("CC", "1")) == 32)

db = SessionLocal()
db.execute(text("SELECT sid_federado FROM pacientes LIMIT 1"))         # falla si falta la migración
db.execute(text("SELECT 1 FROM interop_clientes LIMIT 1"))
cli = TestClient(app)
usuario = db.query(Usuario).first()
ctx = ContextoAcceso(usuario, {}, "all", "paciente", "create", db, None)
DOC_A, DOC_B = "1999000201", "1999000202"
SID_A, SID_B = I.sid_federado("CC", DOC_A), I.sid_federado("CC", DOC_B)
bidx = {d: blind_index(d, "pacientes.documento") for d in (DOC_A, DOC_B)}
cliente = None


def limpiar() -> None:
    db.rollback()
    for b in bidx.values():
        for t in ("alertas", "notificaciones", "eventos_fhir", "observaciones"):
            col = "paciente_id"
            try:
                db.execute(text(f"DELETE FROM {t} WHERE {col} = :p"), {"p": b})
            except Exception:  # noqa: BLE001 -- la tabla puede no tener esa columna en este esquema
                db.rollback()
        db.execute(text("DELETE FROM pacientes WHERE documento_bidx = :p"), {"p": b})
    db.execute(text("DELETE FROM interop_mensajes WHERE sid_federado IN (:a, :b)"), {"a": SID_A, "b": SID_B})
    if cliente:
        db.execute(text("DELETE FROM interop_nonces WHERE client_id = :c"), {"c": cliente["client_id"]})
        db.execute(text("DELETE FROM interop_clientes WHERE client_id = :c"), {"c": cliente["client_id"]})
    db.commit()


def crear_paciente(doc: str) -> None:
    P.crear(P.PacienteCrear(tipo_documento="CC", documento=doc, nombre="Prueba", apellido="Interop",
                            fecha_nacimiento=date(1985, 5, 5), sexo="M"), None, ctx, db)


def obs(sid: str, id_obs: str, valor: float = 95, loinc: str = "2339-0", unidad: str = "mg/dL") -> dict:
    return {"resourceType": "Observation", "id": id_obs, "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc}]},
            "subject": {"identifier": {"system": I.SYS_FEDERADO, "value": sid}},
            "effectiveDateTime": "2026-11-10T09:30:00Z",
            "valueQuantity": {"value": valor, "unit": unidad, "system": "http://unitsofmeasure.org", "code": unidad}}


def token(c=None, **extra) -> str:
    c = c or cliente
    return cli.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": c["client_id"],
                                           "client_secret": c["client_secret"], **extra}).json()["access_token"]


def enviar(cuerpo, tok=None, *, ts=None, nonce=None, firma_con=None, quitar=(), manipular=None):
    """POST firmado a /interop/Observation. Devuelve la respuesta."""
    crudo = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo, separators=(",", ":")).encode()
    ts = ts or str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(18)
    sig = I.firmar(firma_con or cliente["firma_secreto"], ts, nonce, "POST", I.RUTA_OBSERVATION, crudo)
    cab = {"Authorization": f"Bearer {tok or token()}", "Content-Type": "application/fhir+json",
           I.CABECERAS["timestamp"]: ts, I.CABECERAS["nonce"]: nonce, I.CABECERAS["firma"]: sig}
    for q in quitar:
        cab.pop(I.CABECERAS[q] if q in I.CABECERAS else q, None)
    return cli.post(I.RUTA_OBSERVATION, content=manipular if manipular is not None else crudo, headers=cab)


def filas(sql: str, **p):
    return db.execute(text(sql), p).all()


try:
    limpiar()
    cliente = I.crear_cliente(db, "Cliente de prueba")
    db.commit()
    crear_paciente(DOC_A)

    print("\n[2] OAuth2 client_credentials")
    r = cli.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": cliente["client_id"],
                                       "client_secret": cliente["client_secret"], "scope": I.SCOPE_OBSERVATION})
    j = r.json()
    check("200 con access_token, Bearer, 600 segundos y el scope pedido",
          r.status_code == 200 and j["token_type"] == "Bearer" and j["expires_in"] == 600 and j["scope"] == I.SCOPE_OBSERVATION, j)
    check("No se guarda en caché (Cache-Control: no-store)", r.headers.get("cache-control") == "no-store")
    claims = jwt.get_unverified_claims(j["access_token"])
    check("El token es HS256, de 10 minutos, con emisor, audiencia, sub=client_id y scope",
          jwt.get_unverified_header(j["access_token"])["alg"] == "HS256" and claims["exp"] - claims["iat"] == 600
          and claims["aud"] == "pulso-interop" and claims["iss"] == "pulso" and claims["sub"] == cliente["client_id"]
          and claims["scope"] == I.SCOPE_OBSERVATION and claims["type"] == "interop", claims)
    mal = cli.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": cliente["client_id"], "client_secret": "x"})
    no_existe = cli.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": "cli_noexiste", "client_secret": "x"})
    check("Secreto malo y cliente inexistente: 401 invalid_client, el mismo mensaje",
          mal.status_code == no_existe.status_code == 401 and mal.json() == no_existe.json()
          and mal.json()["error"] == "invalid_client", (mal.json(), no_existe.json()))
    check("Otro grant_type → 400 unsupported_grant_type",
          cli.post("/oauth/token", data={"grant_type": "password", "client_id": "a", "client_secret": "b"}).json()["error"]
          == "unsupported_grant_type")
    check("Un scope que el cliente no tiene → 400 invalid_scope",
          cli.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": cliente["client_id"],
                                         "client_secret": cliente["client_secret"], "scope": "system/Patient.write"}).json()["error"]
          == "invalid_scope")
    basic = base64.b64encode(f"{cliente['client_id']}:{cliente['client_secret']}".encode()).decode()
    check("También se acepta HTTP Basic (RFC 6749 §2.3.1)",
          cli.post("/oauth/token", data={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {basic}"}).status_code == 200)

    print("\n[3] Los dos tipos de token no se mezclan")
    tok = token()
    check("El token de interoperabilidad no abre la API normal",
          cli.get("/auth/yo", headers={"Authorization": f"Bearer {tok}"}).status_code == 401)
    from app.core.security import crear_access_token  # noqa: E402
    normal = crear_access_token(usuario.id, "x", "admin", ["paciente:read:all"])
    check("Un token de usuario no sirve en la puerta de interoperabilidad",
          enviar(obs(SID_A, "mezcla-1"), tok=normal).status_code == 401)
    ahora = datetime.now(timezone.utc)
    sin_aud = jwt.encode({"iss": "pulso", "sub": cliente["client_id"], "scope": I.SCOPE_OBSERVATION, "type": "interop",
                          "iat": ahora, "exp": ahora + timedelta(minutes=5)}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    vencido = jwt.encode({"iss": "pulso", "aud": "pulso-interop", "sub": cliente["client_id"], "scope": I.SCOPE_OBSERVATION,
                          "type": "interop", "iat": ahora - timedelta(minutes=20), "exp": ahora - timedelta(minutes=10)},
                         os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    check("Token sin audiencia o vencido → 401",
          enviar(obs(SID_A, "mezcla-2"), tok=sin_aud).status_code == 401 and enviar(obs(SID_A, "mezcla-3"), tok=vencido).status_code == 401)
    sin_scope = jwt.encode({"iss": "pulso", "aud": "pulso-interop", "sub": cliente["client_id"], "scope": "system/Patient.read",
                            "type": "interop", "iat": ahora, "exp": ahora + timedelta(minutes=5)}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    r = enviar(obs(SID_A, "mezcla-4"), tok=sin_scope)
    check("Token válido pero sin el scope system/Observation.write → 403", r.status_code == 403, r.text)
    check("Sin token → 401 con WWW-Authenticate",
          (lambda x: x.status_code == 401 and "Bearer" in x.headers.get("www-authenticate", ""))(
              cli.post(I.RUTA_OBSERVATION, json=obs(SID_A, "sin-token"))))

    print("\n[4] La Observation")
    r = enviar(obs(SID_A, "ext-001", 95))
    check("Válida y con paciente conocido → 201 aceptada", r.status_code == 201 and r.json()["estado"] == "aceptada", r.text)
    o = filas("SELECT codigo_loinc, valor_numerico, unidad_ucum, origen, es_critico FROM observaciones WHERE paciente_id = :p", p=bidx[DOC_A])
    check("Quedó en la historia con LOINC, valor, unidad UCUM y origen fhir_externo",
          len(o) == 1 and (o[0].codigo_loinc, float(o[0].valor_numerico), o[0].unidad_ucum, o[0].origen, o[0].es_critico)
          == ("2339-0", 95.0, "mg/dL", "fhir_externo", False), o)
    check("El mismo id otra vez (nonce nuevo) → 409 y no duplica",
          enviar(obs(SID_A, "ext-001", 95)).status_code == 409
          and filas("SELECT count(*) FROM observaciones WHERE paciente_id = :p", p=bidx[DOC_A])[0][0] == 1)
    r = enviar(obs(SID_A, "ext-002", 30))
    alerta = filas("SELECT tipo, severidad, estado FROM alertas WHERE paciente_id = :p", p=bidx[DOC_A])
    check("Glucosa de 30 mg/dL: 201, marcada crítica y con alerta activa",
          r.status_code == 201 and r.json()["alerta_critica"] is True and alerta
          and alerta[0].tipo == "resultado_critico_externo" and alerta[0].estado == "activa", (r.text, alerta))
    check("La alerta no lleva datos personales", not any("Prueba" in (a or "") for a in
                                                         [x[0] for x in filas("SELECT mensaje FROM alertas WHERE paciente_id = :p", p=bidx[DOC_A])]))

    malo = obs(SID_A, "ext-003")
    malo["subject"]["identifier"] = {"system": "http://hl7.org/fhir/sid/cc", "value": DOC_A}      # cédula en claro
    del malo["valueQuantity"]["system"]
    r = enviar(malo)
    j = r.json()
    check("422 con OperationOutcome que dice qué corregir (cédula en claro y unidad sin UCUM)",
          r.status_code == 422 and j["resourceType"] == "OperationOutcome" and len(j["issue"]) >= 2
          and any("federado" in i["diagnostics"] for i in j["issue"]) and any("UCUM" in i["diagnostics"] for i in j["issue"]), j)
    check("422 si no es JSON y si falta el LOINC", enviar(b"esto no es json").status_code == 422
          and enviar({**obs(SID_A, "ext-004"), "code": {"text": "glucosa"}}).status_code == 422)
    check("Un valor que no es número se rechaza (422)", enviar(obs(SID_A, "ext-005", valor="95")).status_code == 422)
    check("Un cuerpo enorme se corta (413)", enviar(b"{" + b" " * 300_000 + b"}").status_code == 413)

    print("\n[5] Firma, nonce y reloj")
    check("Cuerpo cambiado después de firmar → 401", enviar(obs(SID_A, "firma-1", 95), manipular=json.dumps(obs(SID_A, "firma-1", 950)).encode()).status_code == 401)
    check("Firma hecha con otro secreto → 401", enviar(obs(SID_A, "firma-2"), firma_con="otro-secreto").status_code == 401)
    for q in ("timestamp", "nonce", "firma"):
        check(f"Sin la cabecera {I.CABECERAS[q]} → 401", enviar(obs(SID_A, f"falta-{q}"), quitar=(q,)).status_code == 401)
    check("Marca de tiempo de hace una hora → 401", enviar(obs(SID_A, "viejo-1"), ts=str(int(time.time()) - 3600)).status_code == 401)
    check("Marca de tiempo del futuro (+1 hora) → 401", enviar(obs(SID_A, "futuro-1"), ts=str(int(time.time()) + 3600)).status_code == 401)
    nonce = secrets.token_urlsafe(18)
    primero = enviar(obs(SID_A, "replay-1"), ts="%d" % time.time(), nonce=nonce)
    r2 = enviar(obs(SID_A, "replay-2"), nonce=nonce)
    check("El primer mensaje pasa; reusar su nonce (aunque cambie el contenido) → 401",
          primero.status_code == 201 and r2.status_code == 401 and "Nonce" in r2.text, (primero.text, r2.text))
    check("Un nonce corto o con símbolos se rechaza", enviar(obs(SID_A, "n-1"), nonce="corto").status_code == 401)
    check("La firma de un mensaje no vale en otro endpoint (la ruta va firmada)",
          I.firmar(cliente["firma_secreto"], "1", "n", "POST", "/interop/Observation", b"x") !=
          I.firmar(cliente["firma_secreto"], "1", "n", "POST", "/interop/Otro", b"x"))
    sin_cliente = I.crear_cliente(db, "Cliente que se desactiva")
    db.commit()
    tok_inactivo = token(sin_cliente)
    db.execute(text("UPDATE interop_clientes SET activo = false WHERE client_id = :c"), {"c": sin_cliente["client_id"]})
    db.commit()
    ts = str(int(time.time())); nn = secrets.token_urlsafe(18); cu = json.dumps(obs(SID_A, "inactivo-1")).encode()
    r = cli.post(I.RUTA_OBSERVATION, content=cu, headers={
        "Authorization": f"Bearer {tok_inactivo}", I.CABECERAS["timestamp"]: ts, I.CABECERAS["nonce"]: nn,
        I.CABECERAS["firma"]: I.firmar(sin_cliente["firma_secreto"], ts, nn, "POST", I.RUTA_OBSERVATION, cu)})
    check("Un cliente desactivado ya no entra, aunque su token no haya vencido", r.status_code == 401, r.text)
    db.execute(text("DELETE FROM interop_clientes WHERE client_id = :c"), {"c": sin_cliente["client_id"]})
    db.commit()

    print("\n[6] Conciliación")
    r = enviar(obs(SID_B, "pend-001", 105))
    check("Paciente federado que aún no existe → 202 pendiente",
          r.status_code == 202 and r.json()["estado"] == "pendiente", r.text)
    check("Queda el cuerpo guardado, sin datos personales, a la espera",
          filas("SELECT estado FROM interop_mensajes WHERE id_externo = 'pend-001'")[0][0] == "pendiente")
    check("Llegar otra vez con el mismo id sigue siendo 409", enviar(obs(SID_B, "pend-001", 105)).status_code == 409)
    crear_paciente(DOC_B)
    m = filas("SELECT estado, observacion_id, cuerpo FROM interop_mensajes WHERE id_externo = 'pend-001'")[0]
    check("Al registrar al paciente se le asigna el identificador y el pendiente se asocia solo",
          filas("SELECT sid_federado FROM pacientes WHERE documento_bidx = :p", p=bidx[DOC_B])[0][0] == SID_B
          and m.estado == "aceptada" and m.observacion_id and m.cuerpo is None, m)
    check("La Observation quedó en la historia del paciente nuevo",
          filas("SELECT valor_numerico FROM observaciones WHERE paciente_id = :p", p=bidx[DOC_B])[0][0] == 105)
    check("conciliar() sin pendientes no hace nada", I.conciliar(db) == {"conciliados": 0, "siguen_pendientes": 0})

    print("\n[7] Auditoría")
    reg = filas("SELECT operacion, resultado, valor_nuevo::text FROM log_auditoria WHERE entidad_tipo = 'interop' ORDER BY id DESC LIMIT 80")
    ops = {(x[0], x[1]) for x in reg}
    check("Quedan registrados los aceptados y los rechazados",
          {("observation_recibida", "exito"), ("rechazo_observation", "denegado"), ("token_emitido", "exito"),
           ("token_rechazado", "denegado")} <= ops, ops)
    todo = " ".join(x[2] or "" for x in reg)
    check("Ningún secreto, firma ni token aparece en la auditoría",
          not any(s in todo for s in (cliente["client_secret"], cliente["firma_secreto"], tok, "v1=")))
    check("La cadena de auditoría sigue íntegra", C.verificar(db)["integra"])
finally:
    limpiar()
    db.close()

print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLAS'}")
sys.exit(1 if fallos else 0)
