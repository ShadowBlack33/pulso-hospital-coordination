#!/usr/bin/env python3
"""
Remisiones entre instituciones (proyecto final, R29-R32), contra una base local (se niega a correr contra
otra sin --forzar). Levanta la API en un hilo (puerto 8799) y registra a Pulso como SU PROPIO SOCIO
("espejo"): así se prueba el viaje completo ida y vuelta sin depender del otro equipo.

  1. Contrato: /interop/metadata público y sin datos.
  2. R31: credenciales de sistema; sin ellas 401, sin el scope 403, con API key también entra; un token de
     sistema no abre la API clínica y uno de usuario no abre la entrada.
  3. R30: un Bundle inválido → 422 con OperationOutcome (un issue por problema) y NADA escrito; un
     DiagnosticReport no aprobado se rechaza; la misma remisión dos veces → 409.
  4. R32: sin consentimiento para ESA institución no se remite (409); un reporte de IA pendiente no se
     envía; el Bundle no lleva teléfono, correo ni dirección.
  5. Ida y vuelta: A remite → B la recibe en vivo (bandeja del especialista) → B acepta → A recibe el Task
     y el médico el aviso. También el rechazo con motivo y la consulta del estado por GET /interop/Task/{id}.
  6. R29: un cliente que exige firma HMAC sin firma → 401; con firma → 201.
  7. Auditoría de ambos lados con origen externo, y la cadena sigue íntegra.

Uso:  python scripts/test_remisiones_externas.py
"""
from __future__ import annotations

import base64
import json
import os
import random
import secrets
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
for var in ("JWT_SECRET_KEY", "APP_ENCRYPTION_KEY", "APP_BLIND_INDEX_KEY", "IA_SEUDONIMO_KEY"):
    os.environ.setdefault(var, base64.urlsafe_b64encode(b"k" * 32).decode())
os.environ.setdefault("INTEROP_SECRETO_FEDERADO", "secreto-federado-del-curso-solo-demo")
os.environ["TAREAS_FONDO"] = "false"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

host = (get_settings().database_url.split("@")[-1].split("/")[0].split(":")[0] or "").lower()
if host not in {"localhost", "127.0.0.1", "db", "postgres", ""} and "--forzar" not in sys.argv:
    sys.exit(f"Esta prueba escribe datos de prueba; la base es «{host}», no local. Usa --forzar si estás seguro.")

from app.core.deps import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auditoria_cadena as C  # noqa: E402
from app.services import interop as I  # noqa: E402
from app.services import remisiones_externas as RX  # noqa: E402

PUERTO = 8799
B = f"http://127.0.0.1:{PUERTO}"
fallos = 0


def check(nombre: str, ok: bool, detalle=None) -> None:
    global fallos
    print(f"  {'OK  ' if ok else 'FALLA'}  {nombre}")
    if not ok:
        fallos += 1
        if detalle is not None:
            print("        ", str(detalle)[:500])


servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PUERTO, log_level="warning"))
threading.Thread(target=servidor.run, daemon=True).start()
for _ in range(50):
    try:
        if httpx.get(f"{B}/interop/metadata", timeout=1).status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.2)

db = SessionLocal()
db.execute(text("SELECT 1 FROM interop_socios LIMIT 1"))          # falla si falta la migración
SUF = secrets.token_hex(3)
CODIGO = f"espejo{SUF}"


def login(u: str) -> dict:
    return {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login", json={"username": u, "password": "Demo#Hospital2026"}).json()["access_token"]}


def notifs(h: dict) -> list:
    j = httpx.get(f"{B}/notificaciones", headers=h).json()
    return j if isinstance(j, list) else j.get("notificaciones", [])


def bundle(doc: str, sr_id: str, dr_estado: str | None = "final", **cambios) -> dict:
    p = {"resourceType": "Patient", "identifier": [{"type": {"coding": [{"code": "CC"}]}, "system": "urn:co:cc", "value": doc}],
         "name": [{"family": "Externa", "given": ["Rosa"]}], "gender": "female", "birthDate": "1958-07-09",
         "telecom": [{"system": "phone", "value": "3000000000"}]}
    sr = {"resourceType": "ServiceRequest", "identifier": [{"system": "urn:otro:remision", "value": sr_id}],
          "status": "active", "intent": "order", "priority": "urgent",
          "code": {"coding": [{"system": "http://snomed.info/sct", "code": "3457005", "display": "Patient referral"}]},
          "performerType": {"coding": [{"system": "http://snomed.info/sct", "code": "419192003", "display": "Internal medicine"}]},
          "subject": {"reference": "urn:uuid:p1"}, "reasonCode": [{"text": "Hiperpotasemia persistente; valoración por medicina interna"}],
          "requester": {"display": "Clínica del Otro Equipo"}}
    obs = {"resourceType": "Observation", "status": "final", "id": f"k-{sr_id}",
           "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3", "display": "Potassium"}]},
           "subject": {"reference": "urn:uuid:p1"}, "effectiveDateTime": "2026-11-21T08:00:00Z",
           "valueQuantity": {"value": 6.7, "unit": "mmol/L", "system": "http://unitsofmeasure.org", "code": "mmol/L"}}
    entradas = [{"fullUrl": "urn:uuid:p1", "resource": p}, {"fullUrl": "urn:uuid:sr1", "resource": sr},
                {"fullUrl": "urn:uuid:o1", "resource": obs}]
    if dr_estado:
        entradas.append({"fullUrl": "urn:uuid:dr1", "resource": {
            "resourceType": "DiagnosticReport", "status": dr_estado, "code": {"text": "Reporte IA"},
            "subject": {"reference": "urn:uuid:p1"}, "conclusion": "Riesgo alto de deterioro (aprobado)"}})
    b = {"resourceType": "Bundle", "type": "transaction", "entry": entradas}
    for k, v in cambios.items():
        if k == "sin_paciente":
            b["entry"] = [e for e in b["entry"] if e["resource"]["resourceType"] != "Patient"]
        if k == "sr":
            sr.update(v)
        if k == "obs":
            obs.update(v)
    return b


def cuenta(tabla: str, donde: str, **p) -> int:
    return db.execute(text(f"SELECT COUNT(*) FROM {tabla} WHERE {donde}"), p).scalar()


try:
    print("\n[1] Contrato público")
    md = httpx.get(f"{B}/interop/metadata").json()
    check("GET /interop/metadata describe rutas, autenticación y scopes (sin datos de pacientes)",
          md["resourceType"] == "CapabilityStatement" and "/interop/Bundle" in json.dumps(md)
          and I.SCOPE_REMISION in md["seguridad"]["scopes"], md)

    # El socio "espejo" es Pulso mismo: entra con un cliente sin firma y lo llamamos con OAuth2.
    cliente = I.crear_cliente(db, f"Espejo {SUF}", exige_firma=False)
    socio_id = RX.crear_socio(db, CODIGO, f"Pulso espejo {SUF}", B, "oauth2", cliente["client_id"], cliente["client_secret"],
                              client_id=cliente["client_id"])
    db.commit()
    tok = httpx.post(f"{B}/oauth/token", data={"grant_type": "client_credentials", "client_id": cliente["client_id"],
                                               "client_secret": cliente["client_secret"]}).json()["access_token"]
    SIS = {"Authorization": f"Bearer {tok}", "Content-Type": "application/fhir+json"}

    print("\n[2] R31 · credenciales de sistema")
    doc1 = str(random.randint(10**9, 2 * 10**9))
    r = httpx.post(f"{B}/interop/Bundle", json=bundle(doc1, f"sr-{SUF}-0"))
    check("Sin credencial → 401 con OperationOutcome", r.status_code == 401 and r.json()["resourceType"] == "OperationOutcome", r.text)
    solo_obs = I.crear_cliente(db, f"Solo obs {SUF}", scopes=I.SCOPE_OBSERVATION, exige_firma=False)
    db.commit()
    r = httpx.post(f"{B}/interop/Bundle", json=bundle(doc1, f"sr-{SUF}-0"), headers={"X-API-Key": solo_obs["api_key"]})
    check("Credencial sin el scope system/ServiceRequest.write → 403", r.status_code == 403, r.text)
    check("Un token de sistema no abre la API clínica", httpx.get(f"{B}/pacientes", headers=SIS).status_code == 401)
    m = login("medico01")
    check("Un token de usuario no abre la entrada del socio",
          httpx.post(f"{B}/interop/Bundle", json=bundle(doc1, "x"), headers=m).status_code == 401)

    print("\n[3] R30 · validación y atomicidad")
    antes_p, antes_r = cuenta("pacientes", "TRUE"), cuenta("remisiones", "TRUE")
    malo = bundle(doc1, f"sr-{SUF}-1", dr_estado="preliminary",
                  obs={"code": {"coding": [{"system": "urn:otro", "code": "K"}]}, "valueQuantity": {"value": "6,7"}},
                  sr={"status": "completed", "reasonCode": []})
    r = httpx.post(f"{B}/interop/Bundle", json=malo, headers=SIS)
    issues = r.json().get("issue", [])
    texto = " ".join(i["diagnostics"] for i in issues)
    check("Bundle inválido → 422 con un issue por problema (LOINC, número, estado, motivo, reporte no aprobado)",
          r.status_code == 422 and len(issues) >= 5 and "LOINC" in texto and "APROBADO" in texto, issues)
    check("Un rechazo no deja datos a medias (ni paciente ni remisión)",
          cuenta("pacientes", "TRUE") == antes_p and cuenta("remisiones", "TRUE") == antes_r)
    check("Sin Patient → 422", httpx.post(f"{B}/interop/Bundle", json=bundle(doc1, "y", sin_paciente=True), headers=SIS).status_code == 422)
    check("No es JSON → 400", httpx.post(f"{B}/interop/Bundle", content=b"{no", headers=SIS).status_code == 400)
    check("Otro recurso en vez de Bundle → 422",
          httpx.post(f"{B}/interop/Bundle", json={"resourceType": "Patient"}, headers=SIS).status_code == 422)

    print("\n[4] Recepción (B)")
    r = httpx.post(f"{B}/interop/Bundle", json=bundle(doc1, f"sr-{SUF}-2"), headers=SIS)
    t = r.json()
    check("Bundle válido → 201 con un Task 'requested' y Location", r.status_code == 201 and t["resourceType"] == "Task"
          and t["status"] == "requested" and r.headers.get("location") == f"/interop/Task/{t['id']}", r.text)
    rem = db.execute(text("SELECT * FROM remisiones WHERE id = :i"), {"i": int(t["id"])}).mappings().first()
    check("Quedó como remisión entrante, con especialista asignado y el paciente creado",
          rem["direccion"] == "entrante" and rem["especialista_id"] and rem["datos_externos"]["paciente_nuevo"], dict(rem))
    check("Las Observation del Bundle quedaron en la historia", cuenta("observaciones", "paciente_id = :p", p=rem["paciente_id"]) == 1)
    tel = db.execute(text("SELECT telefono_cifrado FROM pacientes WHERE documento_bidx = :p"), {"p": rem["paciente_id"]}).scalar()
    check("Lo que no hace falta (el teléfono que mandó el socio) no se guarda", tel is None)
    esp_user = db.execute(text("SELECT u.username FROM usuarios u WHERE u.personal_id = :p AND u.activo LIMIT 1"),
                          {"p": rem["especialista_id"]}).scalar()
    e = login(esp_user)
    check("El especialista recibe el aviso en vivo (bandeja: remision_externa_recibida)",
          any(n["tipo"] == "remision_externa_recibida" for n in notifs(e)))
    r = httpx.post(f"{B}/interop/Bundle", json=bundle(doc1, f"sr-{SUF}-2"), headers=SIS)
    check("La misma remisión otra vez → 409", r.status_code == 409 and r.json()["issue"][0]["code"] == "duplicate", r.text)
    r = httpx.get(f"{B}/interop/Task/{t['id']}", headers={"X-API-Key": cliente["api_key"]})
    check("GET /interop/Task/{id} con API key: el socio consulta el estado", r.status_code == 200 and r.json()["status"] == "requested", r.text)
    check("Otro cliente no ve esa remisión (404)",
          httpx.get(f"{B}/interop/Task/{t['id']}", headers={"X-API-Key": solo_obs["api_key"]}).status_code in (403, 404))

    print("\n[5] Envío (A) con consentimiento y minimización")
    doc2 = str(random.randint(10**9, 2 * 10**9))
    pid = httpx.post(f"{B}/pacientes", headers=m, json={"tipo_documento": "CC", "documento": doc2, "nombre": "Luis", "apellido": "Saliente",
                                                       "fecha_nacimiento": "1966-01-02", "sexo": "M", "telefono": "3109998877",
                                                       "email": "luis.saliente@ejemplo.co", "direccion": "Calle Falsa 123"}).json()["paciente_id"]
    cuerpo = {"paciente_id": pid, "socio": CODIGO, "motivo": "Dolor torácico atípico; valoración por cardiología", "prioridad": "urgente",
              "especialidad_id": 12}
    r = httpx.post(f"{B}/remisiones/externas", headers=m, json=cuerpo)
    check("Sin consentimiento para esa institución → 409 (Ley 1581/2012)", r.status_code == 409 and "consentimiento" in r.text, r.text)
    httpx.post(f"{B}/pacientes/{pid}/consentimientos", headers=m, json={"socio": CODIGO})
    lst = httpx.get(f"{B}/pacientes/{pid}/consentimientos", params={"formato": "fhir"}, headers=m).json()
    check("El consentimiento existe como recurso Consent de FHIR", lst["entry"][0]["resource"]["resourceType"] == "Consent"
          and lst["entry"][0]["resource"]["status"] == "active", lst)
    db.execute(text("""INSERT INTO reportes_ia (paciente_id, creado_por, estado, resultado, probabilidad, critico, payload_enviado)
                       VALUES (:p, (SELECT id FROM usuarios WHERE username = 'medico01'), 'pendiente', '{}'::jsonb, 0.4, false, '{}'::jsonb)"""),
               {"p": pid})
    db.commit()
    rep = db.execute(text("SELECT id FROM reportes_ia WHERE paciente_id = :p ORDER BY id DESC LIMIT 1"), {"p": pid}).scalar()
    r = httpx.post(f"{B}/remisiones/externas", headers=m, json={**cuerpo, "reporte_id": rep})
    check("Un reporte de IA pendiente no se envía (409)", r.status_code == 409 and "APROBADO" in r.text, r.text)
    capturado: dict = {}
    original = RX._post_socio

    def espia(socio, ruta, recurso):
        capturado.setdefault(ruta, recurso)
        return original(socio, ruta, recurso)
    RX._post_socio = espia
    r = httpx.post(f"{B}/remisiones/externas", headers=m, json=cuerpo)
    sal = r.json()
    check("Con consentimiento: 201 y el socio la recibió (estado_envio enviada)", r.status_code == 201
          and sal["estado_envio"] == "enviada", sal)
    enviado = json.dumps(capturado.get("/interop/Bundle", {}))
    tipos = [x["resource"]["resourceType"] for x in capturado.get("/interop/Bundle", {}).get("entry", [])]
    check("El Bundle lleva Patient y ServiceRequest (SNOMED)", "Patient" in tipos and "ServiceRequest" in tipos
          and "3457005" in enviado, tipos)
    check("Minimización: ni teléfono, ni correo, ni dirección salen hacia el socio",
          "3109998877" not in enviado and "luis.saliente" not in enviado and "Falsa" not in enviado)
    ents = capturado.get("/interop/Bundle", {}).get("entry", [])
    check("Todo va con PUT e ids estables, con Consent y Task 'requested' (contratos tipo R30)",
          all(x["request"]["method"] == "PUT" for x in ents) and "Consent" in tipos and "Task" in tipos
          and any(x["resource"]["resourceType"] == "ServiceRequest" and x["resource"]["id"] == f"rem-{sal['id']}" for x in ents), tipos)

    print("\n[6] Ida y vuelta: B acepta → A se entera")
    ent = db.execute(text("SELECT * FROM remisiones WHERE direccion = 'entrante' AND id_externo = :x"),
                     {"x": str(sal["id"])}).mappings().first()
    check("Del otro lado (B) llegó como entrante", ent is not None)
    e2 = login(db.execute(text("SELECT username FROM usuarios WHERE personal_id = :p AND activo LIMIT 1"),
                          {"p": ent["especialista_id"]}).scalar())
    r = httpx.post(f"{B}/remisiones/{ent['id']}/aceptar", headers=e2)
    time.sleep(2.5)
    a = db.execute(text("SELECT estado, estado_envio FROM remisiones WHERE id = :i"), {"i": sal["id"]}).first()
    check("B aceptó → A recibió el Task: la saliente quedó 'aceptada'", r.status_code == 200 and a.estado == "aceptada", a)
    check("El médico de A recibe el aviso en vivo (remision_externa_respondida)",
          any(n["tipo"] == "remision_externa_respondida" and "aceptó" in n["mensaje"] for n in notifs(m)))
    b_lado = db.execute(text("SELECT estado_envio FROM remisiones WHERE id = :i"), {"i": ent["id"]}).scalar()
    check("B registró que le respondió al socio", b_lado == "respondida", b_lado)

    print("\n[7] Rechazo con motivo")
    r = httpx.post(f"{B}/remisiones/{t['id']}/rechazar", headers=e, json={"motivo": "No es pertinente: requiere nefrología"})
    check("El especialista rechaza una remisión externa con motivo", r.status_code == 200, r.text)
    r = httpx.get(f"{B}/interop/Task/{t['id']}", headers={"X-API-Key": cliente["api_key"]}).json()
    check("El socio ve el Task 'rejected' con el motivo", r["status"] == "rejected" and "nefrología" in r["statusReason"]["text"], r)
    interna = db.execute(text("SELECT id FROM remisiones WHERE direccion = 'interna' AND estado = 'pendiente' LIMIT 1")).scalar()
    if interna:
        check("Una remisión INTERNA sigue sin poder rechazarse (regla del Corte 2)",
              httpx.post(f"{B}/remisiones/{interna}/rechazar", headers=login("admin"), json={"motivo": "prueba x"}).status_code == 409)
    check("Un Task que no corresponde a nada nuestro → 404",
          httpx.post(f"{B}/interop/Task", headers=SIS, json={"resourceType": "Task", "status": "accepted",
                                                             "basedOn": [{"identifier": {"value": "999999"}}]}).status_code == 404)

    print("\n[8] R29 · firma HMAC para el cliente que la exige")
    firmado = I.crear_cliente(db, f"Con firma {SUF}", exige_firma=True)
    db.commit()
    cuerpo_b = json.dumps(bundle(str(random.randint(10**9, 2 * 10**9)), f"sr-{SUF}-9")).encode()
    r = httpx.post(f"{B}/interop/Bundle", content=cuerpo_b, headers={"X-API-Key": firmado["api_key"]})
    check("Sin firma → 401", r.status_code == 401 and "Faltan cabeceras" in r.text, r.text)
    ts, nonce = str(int(time.time())), secrets.token_urlsafe(18)
    cab = {"X-API-Key": firmado["api_key"], I.CABECERAS["timestamp"]: ts, I.CABECERAS["nonce"]: nonce,
           I.CABECERAS["firma"]: I.firmar(firmado["firma_secreto"], ts, nonce, "POST", "/interop/Bundle", cuerpo_b)}
    r = httpx.post(f"{B}/interop/Bundle", content=cuerpo_b, headers=cab)
    check("Con firma válida → 201", r.status_code == 201, r.text)
    r = httpx.post(f"{B}/interop/Bundle", content=cuerpo_b, headers=cab)
    check("La misma firma otra vez (replay) → 401", r.status_code == 401, r.text)

    print("\n[9] Bundle con la forma de un socio real (contrato R30 del grupo de triaje)")
    tok2 = httpx.post(f"{B}/oauth/token", data={"grant_type": "client_credentials", "client_id": cliente["client_id"],
                                                "client_secret": cliente["client_secret"], "scope": "bundle:write task:write"})
    check("Pide el token con sus scopes (bundle:write task:write) → 200", tok2.status_code == 200, tok2.text)
    SOC = {"Authorization": f"Bearer {tok2.json()['access_token']}", "Content-Type": "application/fhir+json",
           "x-nonce": secrets.token_hex(16)}
    sid, tid, pidx = f"r30-sr-{SUF}", f"r30-task-{SUF}", f"r30-pac-{SUF}"

    def put(rec):
        return {"fullUrl": f"urn:uuid:{rec['resourceType']}-{rec['id']}", "resource": rec,
                "request": {"method": "PUT", "url": f"{rec['resourceType']}/{rec['id']}"}}
    real = {"resourceType": "Bundle", "type": "transaction", "entry": [
        put({"resourceType": "Patient", "id": pidx, "gender": "male", "birthDate": "1971"}),
        put({"resourceType": "Consent", "id": f"r30-con-{SUF}", "status": "active",
             "scope": {"coding": [{"code": "patient-privacy"}]}, "category": [{"text": "Interoperabilidad"}],
             "patient": {"reference": f"Patient/{pidx}"}, "provision": {"type": "permit", "period": {"start": "2026-11-01"}}}),
        put({"resourceType": "ServiceRequest", "id": sid, "status": "active", "intent": "order", "priority": "urgent",
             "code": {"coding": [{"system": "http://snomed.info/sct", "code": "306206005", "display": "Referral to service"}]},
             "subject": {"reference": f"Patient/{pidx}"}, "performerType": {"text": "Cardiología"},
             "reasonCode": [{"text": "Triaje II: dolor torácico con PA elevada"}]}),
        put({"resourceType": "Task", "id": tid, "status": "requested", "intent": "order",
             "focus": {"reference": f"ServiceRequest/{sid}"}, "for": {"reference": f"Patient/{pidx}"}}),
        put({"resourceType": "Observation", "id": f"r30-pa-{SUF}", "status": "final",
             "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9", "display": "Blood pressure panel"}]},
             "subject": {"reference": f"Patient/{pidx}"}, "effectiveDateTime": "2026-11-21T09:00:00-05:00",
             "component": [{"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                            "valueQuantity": {"value": 168, "unit": "mm[Hg]", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}},
                           {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                            "valueQuantity": {"value": 98, "unit": "mm[Hg]", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}}]}),
        put({"resourceType": "Observation", "id": f"r30-fc-{SUF}", "status": "final",
             "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]},
             "subject": {"reference": f"Patient/{pidx}"},
             "valueQuantity": {"value": 112, "unit": "/min", "system": "http://unitsofmeasure.org", "code": "/min"}})]}
    r = httpx.post(f"{B}/interop/Bundle", json=real, headers=SOC)
    check("Paciente seudonimizado + Consent + Task + Observation con componentes → 201", r.status_code == 201, r.text)
    nueva = r.json().get("id")
    otro = json.loads(json.dumps(real).replace(sid, sid + "b"))
    r2 = httpx.post(f"{B}/interop/Bundle", json=otro, headers=SOC)
    check("El mismo x-nonce con otra remisión → 409 (anti-replay sin firma)", r2.status_code == 409 and "nonce" in r2.text, r2.text)
    r2 = httpx.post(f"{B}/interop/Bundle", json=otro, headers={**SOC, "x-nonce": "corto"})
    check("x-nonce inválido → 400", r2.status_code == 400, r2.text)
    if nueva:
        rem2 = db.execute(text("SELECT * FROM remisiones WHERE id = :i"), {"i": int(nueva)}).mappings().first()
        check("Guardamos sus ids (ServiceRequest y Task) para responderles",
              rem2["datos_externos"].get("sr_id") == sid and rem2["datos_externos"].get("task_id") == tid, rem2["datos_externos"])
        check("Sus observaciones quedaron en la historia", cuenta("observaciones", "paciente_id = :p", p=rem2["paciente_id"]) >= 2)
        enviados: list = []

        def espia2(socio, ruta, recurso):
            enviados.append((ruta, recurso))
            return original(socio, ruta, recurso)
        RX._post_socio = espia2
        e3 = login(db.execute(text("SELECT username FROM usuarios WHERE personal_id = :p AND activo LIMIT 1"),
                              {"p": rem2["especialista_id"]}).scalar())
        r = httpx.post(f"{B}/remisiones/{nueva}/aceptar", headers=e3)
        time.sleep(2.5)
        tarea = next((x[1] for x in enviados if x[1].get("resourceType") == "Task"), {})
        check("Al aceptar, el Task de respuesta apunta a SU ServiceRequest y SU Task",
              r.status_code == 200 and tarea.get("status") == "accepted" and tarea.get("id") == tid
              and tarea.get("focus", {}).get("reference") == f"ServiceRequest/{sid}"
              and tarea.get("for", {}).get("reference") == f"Patient/{pidx}", tarea)
        RX._post_socio = original

    print("\n[10] Admin y auditoría")
    adm = login("admin")
    r = httpx.patch(f"{B}/admin/interop/socios/{CODIGO}", headers=adm, json={"base_url": B + "/"})
    check("El admin cambia la URL del socio sin redesplegar (y no ve secretos)",
          r.status_code == 200 and "secreto" not in json.dumps(r.json()).replace("tiene_secreto", ""), r.text)
    check("El médico no administra socios (403)", httpx.get(f"{B}/admin/interop/socios", headers=m).status_code == 403)
    ops = {x[0] for x in db.execute(text("SELECT operacion FROM log_auditoria WHERE id > (SELECT MAX(id) - 400 FROM log_auditoria)"))}
    check("Ambos lados auditados: recibida, enviada, respuesta del socio, respuesta al socio y rechazos",
          {"remision_recibida", "envio_externo", "respuesta_socio", "respuesta_a_socio", "rechazo_externo"} <= ops, ops)
    check("Ningún secreto quedó en la auditoría", not db.execute(text(
        "SELECT 1 FROM log_auditoria WHERE valor_nuevo::text LIKE :s LIMIT 1"), {"s": f"%{cliente['client_secret'][:20]}%"}).first())
    check("La cadena de auditoría sigue íntegra", C.verificar(db, ultimos=500)["integra"])
finally:
    RX._post_socio = original if "original" in dir() else RX._post_socio
    db.rollback()
    db.execute(text("UPDATE interop_socios SET activo = false WHERE codigo = :c"), {"c": CODIGO})
    db.commit()
    db.close()
    servidor.should_exit = True

print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLAS'}")
sys.exit(1 if fallos else 0)
