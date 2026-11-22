"""
Remisiones entre instituciones (proyecto final, R30-R32; contrato en docs/INTEROP.md).

Flujo:
  A (quien remite) arma un Bundle FHIR con Patient, ServiceRequest, las Observation relevantes y el
  DiagnosticReport de IA APROBADO, y lo envía a B. B lo valida, crea la remisión y su especialista la
  ve en vivo. El especialista acepta o rechaza y B le responde a A con un Task. Ambos auditan.

Pulso hace los dos papeles:
  · recibir_bundle()   B: valida TODO antes de escribir nada (un rechazo no deja datos a medias).
  · enviar()           A: exige consentimiento para esa institución y reporte aprobado; minimiza.
  · recibir_task()     A: el socio responde → se actualiza la remisión y se avisa al médico.
  · responder_socio()  B: tras aceptar o rechazar, se envía el Task al socio.

Para poder hablar con CUALQUIER equipo, la forma de autenticarse con cada socio (OAuth2, API key,
bearer fijo o nada, con firma HMAC opcional) y sus rutas se configuran en interop_socios.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
import uuid
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import text

from app.core.crypto import blind_index, cifrar, descifrar
from app.services import fhir as fhir_svc
from app.services import interop as I
from app.services.auditoria import registrar

log = logging.getLogger("pulso.remisiones_externas")

INSTITUCION = "Hospital San Rafael (Pulso)"
SYS_DOC = "https://pulso.hsanrafael.co/fhir/identifier/documento"
SYS_TIPO_DOC = "https://pulso.hsanrafael.co/fhir/CodeSystem/tipo-documento"
SYS_REMISION_EXT = "https://pulso.hsanrafael.co/fhir/identifier/remision"
SYS_OBS_EXT = "https://pulso.hsanrafael.co/fhir/identifier/observacion"
SNOMED_REMISION = {"system": fhir_svc.SYS_SNOMED, "code": "3457005", "display": "Patient referral"}
TIPOS_DOC = {"CC", "TI", "CE", "PA", "RC", "MS", "AS", "PE", "PT", "CD", "SC", "NU"}
_V2_A_TIPO = {"PPN": "PA", "NI": "CC", "NNCOL": "CC", "CZ": "CC", "DL": "CC"}

PRIORIDAD_DESDE_FHIR = {"routine": "rutinaria", "urgent": "prioritaria", "asap": "urgente", "stat": "urgente"}
PRIORIDAD_A_FHIR = {"rutinaria": "routine", "prioritaria": "urgent", "urgente": "stat"}
ESTADO_A_TASK = {"pendiente": "requested", "aceptada": "accepted", "rechazada": "rejected", "atendida": "completed"}
TASK_A_ESTADO = {"accepted": "aceptada", "in-progress": "aceptada", "rejected": "rechazada", "cancelled": "rechazada",
                 "completed": "atendida", "failed": "rechazada", "requested": "pendiente", "received": "pendiente",
                 "ready": "aceptada", "on-hold": "pendiente"}
TIPOS_BUNDLE = {"transaction", "collection", "message", "batch", "document", "searchset"}
OBS_MAX = 20                 # observaciones por remisión: lo necesario, no la historia entera (R32)
OBS_DIAS = 30


# =============================================================== utilidades FHIR
def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _texto(concepto) -> str:
    """Texto legible de un CodeableConcept (o de una lista de ellos)."""
    if isinstance(concepto, list):
        return "; ".join(t for t in (_texto(c) for c in concepto) if t)
    if not isinstance(concepto, dict):
        return ""
    if concepto.get("text"):
        return str(concepto["text"])
    return ", ".join(str(c.get("display") or c.get("code") or "") for c in concepto.get("coding") or [] if c)


def _codigos(concepto, sistema: str | None = None) -> list[dict]:
    if not isinstance(concepto, dict):
        return []
    return [c for c in concepto.get("coding") or [] if isinstance(c, dict) and (sistema is None or c.get("system") == sistema)]


def _recursos(bundle: dict) -> list[tuple[str | None, dict]]:
    """(fullUrl, recurso) de cada entrada."""
    salida = []
    for e in bundle.get("entry") or []:
        if isinstance(e, dict) and isinstance(e.get("resource"), dict):
            salida.append((e.get("fullUrl"), e["resource"]))
    return salida


def _claves(full_url: str | None, r: dict) -> set[str]:
    """Las formas en que otro recurso del Bundle puede referirse a este."""
    claves = set()
    if full_url:
        claves.add(full_url)
    if r.get("id"):
        claves.add(f"{r.get('resourceType')}/{r['id']}")
        if full_url and full_url.endswith(f"/{r['resourceType']}/{r['id']}"):
            claves.add(full_url)
    return claves


def _id_de(full_url: str | None, r: dict) -> str | None:
    """Un id estable para el recurso: su identifier, su id o el uuid del fullUrl."""
    for i in r.get("identifier") or []:
        if isinstance(i, dict) and i.get("value"):
            return str(i["value"])[:120]
    if r.get("id"):
        return str(r["id"])[:120]
    if full_url:
        return full_url.rsplit(":", 1)[-1].rsplit("/", 1)[-1][:120]
    return None


# =============================================================== paciente
def _documento(pat: dict) -> tuple[str, str] | None:
    """(tipo, número) del documento de identidad del Patient, si lo trae."""
    candidatos = []
    for i in pat.get("identifier") or []:
        if not isinstance(i, dict) or not i.get("value"):
            continue
        sistema = str(i.get("system") or "")
        if sistema in (I.SYS_FEDERADO, fhir_svc.SYS_MRN):
            continue
        valor = re.sub(r"[^0-9A-Za-z]", "", str(i["value"])).upper()
        if not valor:
            continue
        tipo = None
        for c in _codigos(i.get("type") or {}):
            cod = str(c.get("code") or "").upper()
            tipo = cod if cod in TIPOS_DOC else _V2_A_TIPO.get(cod, tipo)
        if tipo is None:
            fin = sistema.rstrip("/").rsplit("/", 1)[-1].upper()
            tipo = fin if fin in TIPOS_DOC else None
        candidatos.append((0 if tipo else 1, tipo or "CC", valor))
    if not candidatos:
        return None
    candidatos.sort()
    _, tipo, valor = candidatos[0]
    return tipo, valor


def _federado(pat: dict) -> str | None:
    for i in pat.get("identifier") or []:
        if isinstance(i, dict) and i.get("system") == I.SYS_FEDERADO and re.fullmatch(r"[0-9a-f]{32}", str(i.get("value") or "")):
            return i["value"]
    return None


def _nombre(pat: dict) -> tuple[str, str] | None:
    for n in pat.get("name") or []:
        if not isinstance(n, dict):
            continue
        dados = " ".join(str(g) for g in n.get("given") or [] if g).strip()
        apellido = str(n.get("family") or "").strip()
        if not (dados and apellido) and n.get("text"):
            partes = str(n["text"]).split()
            if len(partes) >= 2:
                dados, apellido = " ".join(partes[:-1]), partes[-1]
        if dados and apellido:
            return dados[:80], apellido[:80]
    return None


def _validar_paciente(pat: dict, errores: list[str]) -> dict:
    doc, sid, nombre = _documento(pat), _federado(pat), _nombre(pat)
    seudonimo = None
    if not doc and not sid:
        # Paciente seudonimizado (sin nombre ni documento): se acepta con su id o su primer identifier.
        ident = next((i for i in pat.get("identifier") or [] if isinstance(i, dict) and i.get("value")), None)
        seudonimo = str(ident["value"]) if ident else (str(pat["id"]) if pat.get("id") else None)
        if not seudonimo:
            errores.append("Patient debe traer un documento de identidad (con su tipo), el identificador federado "
                           f"({I.SYS_FEDERADO}) o al menos un id o identifier seudonimizado")
    nacimiento = None
    if pat.get("birthDate"):
        try:
            f = str(pat["birthDate"])[:10]                  # FHIR admite AAAA, AAAA-MM o AAAA-MM-DD
            nacimiento = date.fromisoformat({4: f + "-01-01", 7: f + "-01"}.get(len(f), f))
        except ValueError:
            errores.append("Patient.birthDate debe ser una fecha AAAA, AAAA-MM o AAAA-MM-DD")
    sexo = {"male": "M", "female": "F", "other": "O", "unknown": "O"}.get(str(pat.get("gender") or ""), None)
    return {"doc": doc, "sid": sid, "nombre": nombre, "nacimiento": nacimiento, "sexo": sexo, "seudonimo": seudonimo}


def _paciente(db, datos: dict, errores: list[str], usuario_sistema: int) -> tuple[str | None, bool]:
    """Busca al paciente (federado, luego documento) o lo crea. Devuelve (paciente_id, creado)."""
    if datos["sid"]:
        filas = db.execute(text("SELECT documento_bidx FROM pacientes WHERE sid_federado = :s AND activo"),
                           {"s": datos["sid"]}).all()
        if len(filas) == 1:
            return filas[0][0], False
    if datos["doc"]:
        bidx = blind_index(datos["doc"][1], "pacientes.documento")
        if db.execute(text("SELECT 1 FROM pacientes WHERE documento_bidx = :b"), {"b": bidx}).first():
            return bidx, False
        faltan = [c for c, v in (("Patient.name (nombre y apellido)", datos["nombre"]),
                                 ("Patient.birthDate", datos["nacimiento"]),
                                 ("Patient.gender", datos["sexo"])) if not v]
        if faltan:
            errores.append("El paciente no existe en Pulso; para registrarlo hace falta: " + ", ".join(faltan))
            return None, False
        tipo, numero = datos["doc"]
        dados, apellido = datos["nombre"]
        db.execute(text("""
            INSERT INTO pacientes (documento_bidx, tipo_documento, documento_cifrado, nombre_cifrado, apellido_cifrado,
                                   fecha_nacimiento, sexo, created_by)
            VALUES (:b, :t, :d, :n, :a, :f, :s, :u)"""),
            {"b": bidx, "t": tipo, "d": cifrar(numero, "pacientes.documento"), "n": cifrar(dados, "pacientes.nombre"),
             "a": cifrar(apellido, "pacientes.apellido"), "f": datos["nacimiento"], "s": datos["sexo"], "u": usuario_sistema})
        I.asignar_sid(db, bidx, tipo, numero)
        return bidx, True
    # Sin documento ni id federado conocido: el socio minimizó (R32) y mandó un paciente SEUDONIMIZADO.
    # Se registra con su seudónimo como identificador externo; si vuelve a llegar, es el mismo paciente.
    seudonimo = datos.get("seudonimo") or datos.get("sid")
    if not seudonimo:
        errores.append("Patient necesita un id o un identifier (aunque sea un seudónimo) para poder registrarlo")
        return None, False
    numero = re.sub(r"[^0-9A-Za-z]", "", seudonimo)[:40] or "X"
    bidx = blind_index(f"EXT{numero}", "pacientes.documento")
    if db.execute(text("SELECT 1 FROM pacientes WHERE documento_bidx = :b"), {"b": bidx}).first():
        return bidx, False
    dados, apellido = datos["nombre"] or ("Paciente", f"externo {numero[:12]}")
    db.execute(text("""
        INSERT INTO pacientes (documento_bidx, tipo_documento, documento_cifrado, nombre_cifrado, apellido_cifrado,
                               fecha_nacimiento, sexo, created_by)
        VALUES (:b, 'EXT', :d, :n, :a, :f, :s, :u)"""),
        {"b": bidx, "d": cifrar(f"EXT{numero}", "pacientes.documento"), "n": cifrar(dados, "pacientes.nombre"),
         "a": cifrar(apellido, "pacientes.apellido"), "f": datos["nacimiento"] or date(1900, 1, 1),
         "s": datos["sexo"] or "O", "u": usuario_sistema})
    return bidx, True


# =============================================================== especialidad
def _especialidad(db, sr: dict) -> tuple[int | None, str]:
    """Especialidad pedida: por SNOMED (performerType o code) o por nombre; si no, medicina interna."""
    conceptos = (sr.get("performerType") or {}, sr.get("code") or {}, *(sr.get("category") or []))
    for c in conceptos:
        for cod in _codigos(c, fhir_svc.SYS_SNOMED):
            fila = db.execute(text("SELECT id, nombre FROM especialidades WHERE activo AND codigo_snomed = :c"),
                              {"c": str(cod.get("code"))}).first()
            if fila:
                return fila[0], fila[1]
    for c in conceptos:
        nombre = _texto(c).strip()
        if len(nombre) >= 4:
            fila = db.execute(text("""SELECT id, nombre FROM especialidades WHERE activo
                                      AND (nombre ILIKE :n OR :t ILIKE '%' || nombre || '%') ORDER BY length(nombre) DESC LIMIT 1"""),
                              {"n": f"%{nombre}%", "t": nombre}).first()
            if fila:
                return fila[0], fila[1]
    fila = db.execute(text("SELECT id, nombre FROM especialidades WHERE codigo = 'MED_INT'")).first()
    return (fila[0], fila[1]) if fila else (None, "")


def _usuario_sistema(db) -> int:
    fila = db.execute(text("""SELECT u.id FROM usuarios u JOIN roles r ON r.id = u.rol_id
                              WHERE r.codigo IN ('servicio_integracion', 'admin') AND u.activo
                              ORDER BY (r.codigo = 'servicio_integracion') DESC, u.id LIMIT 1""")).first()
    return fila[0]


def socio_de_cliente(db, client_id: str) -> dict | None:
    fila = db.execute(text("SELECT * FROM interop_socios WHERE client_id = :c AND activo"), {"c": client_id}).mappings().first()
    return dict(fila) if fila else None


# =============================================================== B: recibir un Bundle
def validar_bundle(bundle) -> tuple[list[str], dict]:
    """Valida forma y códigos SIN tocar la base. Devuelve (errores, partes)."""
    e: list[str] = []
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        return ["El cuerpo debe ser un Bundle FHIR (resourceType: Bundle)"], {}
    if bundle.get("type") not in TIPOS_BUNDLE:
        e.append(f"Bundle.type debe ser uno de: {', '.join(sorted(TIPOS_BUNDLE))}")
    recursos = _recursos(bundle)
    por_tipo: dict[str, list] = {}
    for fu, r in recursos:
        por_tipo.setdefault(r.get("resourceType"), []).append((fu, r))
    pacientes, srs = por_tipo.get("Patient", []), por_tipo.get("ServiceRequest", [])
    if len(pacientes) != 1:
        e.append(f"El Bundle debe traer exactamente un Patient (trae {len(pacientes)})")
    if len(srs) != 1:
        e.append(f"El Bundle debe traer exactamente un ServiceRequest (trae {len(srs)})")
    if e:
        return e, {}
    (fu_p, pat), (fu_sr, sr) = pacientes[0], srs[0]
    claves_p = _claves(fu_p, pat)

    if sr.get("status") not in ("active", "draft"):
        e.append("ServiceRequest.status debe ser 'active' (o 'draft')")
    if sr.get("intent") not in ("order", "plan", "proposal", "original-order", "directive"):
        e.append("ServiceRequest.intent debe ser 'order'")
    if not (_codigos(sr.get("code") or {}) or (sr.get("code") or {}).get("text")):
        e.append("ServiceRequest.code es obligatorio (SNOMED CT, por ejemplo 3457005 'Patient referral')")
    sujeto = ((sr.get("subject") or {}).get("reference")) or ""
    if sujeto not in claves_p:
        e.append("ServiceRequest.subject debe referenciar al Patient del Bundle")
    motivo = "; ".join(t for t in (_texto(sr.get("reasonCode") or []),
                                   " ".join(str(n.get("text") or "") for n in sr.get("note") or [] if isinstance(n, dict)))
                       if t.strip())
    if len(motivo.strip()) < 5:
        e.append("ServiceRequest.reasonCode (o note) debe decir el motivo de la remisión")
    id_ext = _id_de(fu_sr, sr)
    if not id_ext:
        e.append("ServiceRequest necesita identifier o id: con él se evita recibir dos veces la misma remisión")
    prioridad = PRIORIDAD_DESDE_FHIR.get(str(sr.get("priority") or "routine"))
    if prioridad is None:
        e.append("ServiceRequest.priority debe ser routine, urgent, asap o stat")

    obs_validas = []
    for fu, o in por_tipo.get("Observation", []):
        oid = _id_de(fu, o) or "?"
        loinc = _codigos(o.get("code") or {}, fhir_svc.SYS_LOINC)
        if not loinc:
            e.append(f"Observation {oid}: code.coding debe traer un código LOINC ({fhir_svc.SYS_LOINC})")
        q = o.get("valueQuantity")
        if q is not None and (isinstance(q.get("value"), bool) or not isinstance(q.get("value"), (int, float))):
            e.append(f"Observation {oid}: valueQuantity.value debe ser un número")
        elif q is None and not (o.get("valueString") or o.get("valueCodeableConcept") or o.get("component")):
            e.append(f"Observation {oid}: falta el valor (valueQuantity, valueString, valueCodeableConcept o component)")
        if ((o.get("subject") or {}).get("reference") or "") not in claves_p:
            e.append(f"Observation {oid}: subject debe referenciar al Patient del Bundle")
        if o.get("status") not in ("final", "amended", "corrected", "preliminary"):
            e.append(f"Observation {oid}: status debe ser final, amended, corrected o preliminary")
        obs_validas.append((oid, o))

    dr = None
    reportes = por_tipo.get("DiagnosticReport", [])
    if len(reportes) > 1:
        e.append("El Bundle trae más de un DiagnosticReport; se espera solo el reporte aprobado")
    elif reportes:
        fu_dr, dr = reportes[0]
        if dr.get("status") not in ("final", "amended", "corrected"):
            e.append("DiagnosticReport.status debe ser 'final': solo se remite un reporte APROBADO, "
                     "nunca uno pendiente o rechazado")
        if ((dr.get("subject") or {}).get("reference") or "") not in claves_p:
            e.append("DiagnosticReport.subject debe referenciar al Patient del Bundle")

    datos_p = _validar_paciente(pat, e)
    tarea = next((r for _, r in por_tipo.get("Task", [])), None)
    consent = next((r for _, r in por_tipo.get("Consent", [])), None)
    if consent is not None and consent.get("status") not in (None, "active"):
        e.append("Consent.status debe ser 'active': sin consentimiento vigente no se recibe la remisión")
    return e, {"pat": datos_p, "sr": sr, "id_ext": id_ext, "motivo": motivo.strip()[:1000],
               "prioridad": prioridad, "obs": obs_validas, "dr": dr,
               "task_id": tarea.get("id") if tarea else None, "patient_id": pat.get("id"),
               "consent": {"estado": consent.get("status"), "tipo": (consent.get("provision") or {}).get("type")} if consent else None}


def _aplanar_observacion(o: dict) -> dict:
    """Signos vitales con componentes (presión arterial) → un texto legible; unidad no UCUM → la que traiga."""
    o = dict(o)
    if o.get("valueQuantity") is None and o.get("component"):
        partes = []
        for c in o["component"]:
            q = c.get("valueQuantity") or {}
            if q.get("value") is not None:
                partes.append(f"{_texto(c.get('code') or {}) or 'valor'}: {q.get('value')} {q.get('unit') or q.get('code') or ''}".strip())
        o["valueString"] = "; ".join(partes)[:200] or None
    elif o.get("valueQuantity") is None and o.get("valueCodeableConcept"):
        o["valueString"] = _texto(o["valueCodeableConcept"])[:200]
    q = o.get("valueQuantity")
    if q and not q.get("code"):
        o["valueQuantity"] = {**q, "code": q.get("unit") or "1"}
    return o


def recibir_bundle(db, client_id: str, bundle, request=None) -> tuple[int, dict]:
    """
    (http, Task). Todo ocurre en la transacción del llamador: si algo falla, no queda nada escrito.
    201 creada · 409 ya recibida o sin especialista · 422 inválida (ErrorInterop con la lista de problemas).
    """
    from app.api.remisiones import asignar_especialista      # noqa: PLC0415
    from app.services import fhir_reactivo, reactores        # noqa: PLC0415

    errores, partes = validar_bundle(bundle)
    if errores:
        raise I.ErrorInterop(422, "Bundle inválido", "invalid", errores)
    socio = socio_de_cliente(db, client_id)
    ya = db.execute(text("SELECT id FROM remisiones WHERE direccion = 'entrante' AND client_id = :c AND id_externo = :i"),
                    {"c": client_id, "i": partes["id_ext"]}).first()
    if ya:
        raise I.ErrorInterop(409, f"La remisión {partes['id_ext']} ya fue recibida (Task {ya[0]})", "duplicate")

    usuario = _usuario_sistema(db)
    errores = []
    paciente_id, creado = _paciente(db, partes["pat"], errores, usuario)
    if errores:
        raise I.ErrorInterop(422, "Paciente incompleto", "invalid", errores)
    esp_id, esp_nombre = _especialidad(db, partes["sr"])
    especialista = asignar_especialista(db, esp_id, None) if esp_id else None
    if especialista is None:
        # Otra institución puede pedir una especialidad que aquí no tenemos con turno: la recibe el especialista
        # con menos carga, que la redirige internamente o la rechaza con motivo. Mejor eso que perderla.
        from app.api.remisiones import CARGA_SQL      # noqa: PLC0415
        fila = db.execute(text(f"SELECT c.id, c.especialidad_id FROM ({CARGA_SQL}) c "
                               "ORDER BY c.abiertas, c.ultima NULLS FIRST, c.id LIMIT 1")).first()
        if fila:
            especialista = fila[0]
            esp_nombre = f"{esp_nombre} (sin especialistas con turno; asignada por carga)"
            esp_id = fila[1]
    if especialista is None:
        raise I.ErrorInterop(409, f"No hay especialistas disponibles de {esp_nombre or 'esa especialidad'} para recibirla",
                             "business-rule")

    guardadas = 0
    for oid, o in partes["obs"]:
        o = _aplanar_observacion(o)
        try:
            with db.begin_nested():
                if fhir_reactivo.guardar_resultado(db, o, paciente_id, f"{client_id}:{oid}"[:64]):
                    guardadas += 1
        except Exception:  # noqa: BLE001 -- un resultado que no se pudo guardar no tumba la remisión
            log.warning("Observation %s del socio no se guardó", oid)
    dr = partes["dr"]
    datos = {"socio": socio["nombre"] if socio else client_id, "socio_codigo": socio["codigo"] if socio else None,
             "institucion": ((partes["sr"].get("requester") or {}).get("display")),
             "solicitante": ((partes["sr"].get("requester") or {}).get("display")),
             "service_request": partes["id_ext"], "codigo": _texto(partes["sr"].get("code") or {}),
             # Para responder con referencias que el socio entienda (ServiceRequest/<id>, Task/<id>, Patient/<id>).
             "sr_id": partes["sr"].get("id"), "task_id": partes.get("task_id"), "patient_id": partes.get("patient_id"),
             "consentimiento_socio": partes.get("consent"),
             "observaciones": guardadas, "paciente_nuevo": creado, "recibida_at": _ahora(),
             "diagnostic_report": {"estado": dr.get("status"), "codigo": _texto(dr.get("code") or {}),
                                   "conclusion": str(dr.get("conclusion") or "")[:2000]} if dr else None}
    fila = db.execute(text("""
        INSERT INTO remisiones (paciente_id, medico_id, remitido_por, especialista_id, especialidad_id, tipo_solicitud,
                                motivo, prioridad, direccion, socio_id, client_id, id_externo, datos_externos)
        VALUES (:p, NULL, :u, :e, :ed, 'valoracion', :m, :pr, 'entrante', :s, :c, :i, CAST(:d AS jsonb)) RETURNING *"""),
        {"p": paciente_id, "u": usuario, "e": especialista, "ed": esp_id, "m": partes["motivo"], "pr": partes["prioridad"],
         "s": socio["id"] if socio else None, "c": client_id, "i": partes["id_ext"], "d": json.dumps(datos)}).mappings().first()
    registrar(db, None, "remision", fila["id"], "remision_recibida", request=request,
              valor_nuevo={"origen": "externo", "client_id": client_id, "socio": datos["socio"],
                           "id_externo": partes["id_ext"], "especialista_id": especialista, "especialidad": esp_nombre,
                           "observaciones": guardadas, "reporte_aprobado": bool(dr), "paciente_nuevo": creado})
    reactores.al_remision_externa(db, dict(fila))
    return 201, task_de(db, dict(fila))


def task_de(db, r: dict) -> dict:
    """La remisión vista como Task FHIR (lo que el socio consulta o recibe)."""
    extra = r.get("datos_externos") or {}
    tarea = {"resourceType": "Task", "id": str(r["id"]),
             "identifier": [{"system": SYS_REMISION_EXT, "value": str(r["id"])}],
             "status": ESTADO_A_TASK.get(r["estado"], "requested"), "intent": "order",
             "priority": PRIORIDAD_A_FHIR.get(r["prioridad"], "routine"),
             "code": {"coding": [SNOMED_REMISION]},
             "authoredOn": (r["created_at"].isoformat() if r.get("created_at") else _ahora()),
             "lastModified": _ahora(),
             "owner": {"display": INSTITUCION}}
    if r.get("direccion") == "entrante":
        tarea["basedOn"] = [{"identifier": {"value": r["id_externo"]}, "type": "ServiceRequest"}]
        tarea["focus"] = {"identifier": {"value": r["id_externo"]}, "type": "ServiceRequest"}
    if r["estado"] in ("rechazada", "atendida") and r.get("respuesta"):
        tarea["statusReason"] = {"text": r["respuesta"][:500]}
    if r["estado"] == "rechazada":
        tarea["businessStatus"] = {"text": "Rechazada por el especialista"}
    elif r["estado"] == "aceptada":
        tarea["businessStatus"] = {"text": "Aceptada: el especialista atenderá al paciente"}
    if extra.get("service_request"):
        tarea["note"] = [{"text": f"Remisión {extra['service_request']} de {extra.get('socio') or 'socio'}"}]
    return tarea


# =============================================================== A: recibir la respuesta (Task)
def recibir_task(db, client_id: str, task, request=None) -> tuple[int, dict]:
    from app.services import reactores     # noqa: PLC0415
    if not isinstance(task, dict) or task.get("resourceType") != "Task":
        raise I.ErrorInterop(422, "Task inválido", "invalid", ["El cuerpo debe ser un Task FHIR (resourceType: Task)"])
    estado = TASK_A_ESTADO.get(str(task.get("status") or ""))
    if estado is None:
        raise I.ErrorInterop(422, "Task inválido", "invalid",
                             ["Task.status debe ser accepted, rejected, completed, in-progress, cancelled..."])
    # ¿A cuál remisión nuestra responde? Por el identificador que enviamos en el ServiceRequest.
    def _valores(refs) -> list[str]:
        salida = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            ident = ref.get("identifier") if "identifier" in ref else (ref if "value" in ref else None)
            if isinstance(ident, dict) and ident.get("value"):
                salida.append(str(ident["value"]))
            if ref.get("reference"):
                salida.append(str(ref["reference"]).rsplit("/", 1)[-1])
        return salida
    # Primero lo que apunta a NUESTRO ServiceRequest (basedOn/focus); el identifier del Task es del socio.
    candidatos = (_valores([*(task.get("basedOn") or []), task.get("focus") or {}]) or _valores(task.get("identifier") or [])
                  or [str(task.get("id") or "")])      # o el id del Task que les enviamos (rem-<n>)
    numeros = [int(c.rsplit("-", 1)[-1]) for c in candidatos if re.fullmatch(r"(?:[A-Za-z]+-)?\d{1,9}", c)]
    if not numeros:
        raise I.ErrorInterop(422, "Task inválido", "invalid",
                             [f"Task.basedOn (o focus) debe traer el identifier del ServiceRequest que le enviamos "
                              f"(system {SYS_REMISION_EXT})"])
    socio = socio_de_cliente(db, client_id)
    if not socio:
        raise I.ErrorInterop(403, "Su credencial no está asociada a ninguna institución socia de Pulso", "forbidden")
    r = db.execute(text("""SELECT * FROM remisiones WHERE id = ANY(:ids) AND direccion = 'saliente' AND socio_id = :s
                           ORDER BY id LIMIT 1"""), {"ids": numeros, "s": socio["id"]}).mappings().first()
    if r is None:
        raise I.ErrorInterop(404, "No hay una remisión nuestra enviada a su institución con ese identificador", "not-found")
    nota = (task.get("statusReason") or {}).get("text") or _texto(task.get("businessStatus") or {}) or \
        " ".join(str(n.get("text") or "") for n in task.get("note") or [] if isinstance(n, dict)) or None
    anterior = r["estado"]
    fila = db.execute(text("""
        UPDATE remisiones SET estado = :e, respuesta = COALESCE(:n, respuesta),
               aceptada_at = CASE WHEN :e = 'aceptada' THEN COALESCE(aceptada_at, now()) ELSE aceptada_at END,
               cerrada_at = CASE WHEN :e IN ('rechazada', 'atendida') THEN now() ELSE cerrada_at END,
               datos_externos = COALESCE(datos_externos, '{}'::jsonb) || CAST(:d AS jsonb)
        WHERE id = :i RETURNING *"""),
        {"e": estado, "n": (nota or "")[:1000] or None, "i": r["id"],
         "d": json.dumps({"task_socio": {"id": task.get("id"), "status": task.get("status"), "recibido_at": _ahora()}})}
    ).mappings().first()
    registrar(db, None, "remision", r["id"], "respuesta_socio", request=request,
              valor_anterior={"estado": anterior},
              valor_nuevo={"origen": "externo", "client_id": client_id, "socio": socio["nombre"], "estado": estado})
    if anterior != estado:
        reactores.al_respuesta_socio(db, dict(fila), socio["nombre"])
    return 200, task_de(db, dict(fila))


# =============================================================== llamar al socio
class ErrorEnvio(Exception):
    pass


def _token_socio(socio: dict, cliente: httpx.Client) -> str:
    secreto = descifrar(bytes(socio["credencial_secreto_cifrado"]), f"interop_socios.secreto.{socio['codigo']}") \
        if socio.get("credencial_secreto_cifrado") else ""
    url = socio["base_url"].rstrip("/") + socio["ruta_token"]
    datos = {"grant_type": "client_credentials", "client_id": socio.get("credencial_id") or "", "client_secret": secreto}
    r = cliente.post(url, data=datos)
    if r.status_code >= 400:   # algunos servidores piden JSON o Basic en vez de formulario
        r = cliente.post(url, json=datos)
    if r.status_code >= 400:
        raise ErrorEnvio(f"El socio rechazó nuestras credenciales en {socio['ruta_token']}: HTTP {r.status_code} {r.text[:200]}")
    try:
        cuerpo = r.json()
        return cuerpo.get("access_token") or cuerpo.get("token") or cuerpo["accessToken"]
    except Exception:  # noqa: BLE001
        raise ErrorEnvio(f"La respuesta de {socio['ruta_token']} no trae access_token") from None


def _cabeceras(socio: dict, cliente: httpx.Client, ruta: str, cuerpo: bytes) -> dict:
    cab = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json, application/json"}
    modo = socio["modo_auth"]
    secreto = descifrar(bytes(socio["credencial_secreto_cifrado"]), f"interop_socios.secreto.{socio['codigo']}") \
        if socio.get("credencial_secreto_cifrado") else ""
    if modo == "oauth2":
        cab["Authorization"] = f"Bearer {_token_socio(socio, cliente)}"
    elif modo == "bearer":
        cab["Authorization"] = f"Bearer {secreto}"
    elif modo == "api_key":
        cab[socio["api_key_cabecera"] or "X-API-Key"] = secreto
    # Nonce y hora en TODOS los mensajes: varios socios los exigen aunque no firmen (anti-replay).
    ts, nonce = str(int(time.time())), secrets.token_urlsafe(18)
    cab.update({I.CABECERAS["timestamp"]: ts, I.CABECERAS["nonce"]: nonce})
    if socio.get("firma_secreto_cifrado"):
        firma = descifrar(bytes(socio["firma_secreto_cifrado"]), f"interop_socios.firma.{socio['codigo']}")
        cab[I.CABECERAS["firma"]] = I.firmar(firma, ts, nonce, "POST", ruta, cuerpo)
    return cab


def _post_socio(socio: dict, ruta: str, recurso: dict) -> httpx.Response:
    cuerpo = json.dumps(recurso, separators=(",", ":"), default=str).encode("utf-8")
    with httpx.Client(timeout=20, follow_redirects=True) as cliente:
        cab = _cabeceras(socio, cliente, ruta, cuerpo)
        return cliente.post(socio["base_url"].rstrip("/") + ruta, content=cuerpo, headers=cab)


# =============================================================== A: armar y enviar el Bundle
def consentimiento_vigente(db, paciente_id: str, socio_id: int) -> dict | None:
    fila = db.execute(text("""SELECT * FROM consentimientos WHERE paciente_id = :p AND socio_id = :s
                              AND finalidad = 'remision' AND revocado_at IS NULL"""),
                      {"p": paciente_id, "s": socio_id}).mappings().first()
    return dict(fila) if fila else None


def _observaciones_relevantes(db, paciente_id: str, ids: list[int] | None) -> list[dict]:
    """Las elegidas por el médico o, si no eligió, el último valor de cada examen de los últimos 30 días."""
    if ids:
        filas = db.execute(text("""SELECT * FROM observaciones WHERE paciente_id = :p AND id = ANY(:ids)
                                   ORDER BY fecha_hora DESC LIMIT :n"""), {"p": paciente_id, "ids": ids, "n": OBS_MAX})
    else:
        filas = db.execute(text("""
            SELECT DISTINCT ON (codigo_loinc) * FROM observaciones
            WHERE paciente_id = :p AND fecha_hora > now() - make_interval(days => :d) AND codigo_loinc IS NOT NULL
            ORDER BY codigo_loinc, fecha_hora DESC LIMIT :n"""), {"p": paciente_id, "d": OBS_DIAS, "n": OBS_MAX})
    return [dict(f) for f in filas.mappings().all()]


def construir_bundle(db, r: dict, reporte: dict | None, obs: list[dict], consentimiento: dict | None = None) -> dict:
    """
    Bundle transaction mínimo (R32): sin teléfono, correo ni dirección. Todo va con PUT e ids ESTABLES
    (rem-<n>, pac-<…>, …) para que el socio nos pueda responder apuntando a ServiceRequest/rem-<n> o Task/rem-<n>.
    Lleva el Consent del paciente para esa institución y un Task 'requested' (lo piden varios contratos).
    """
    p = db.execute(text("SELECT * FROM pacientes WHERE documento_bidx = :p"), {"p": r["paciente_id"]}).mappings().first()
    doc = descifrar(bytes(p["documento_cifrado"]), "pacientes.documento")
    n = r["id"]
    id_pac = f"pac-{p['sid_federado'] or r['paciente_id'].strip()[:24]}"[:64]
    ref_pac, ref_sr, ref_task = f"Patient/{id_pac}", f"ServiceRequest/rem-{n}", f"Task/rem-{n}"

    def entrada(recurso: dict) -> dict:
        tipo, rid = recurso["resourceType"], recurso["id"]
        return {"fullUrl": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'pulso/{tipo}/{rid}')}",
                "resource": recurso, "request": {"method": "PUT", "url": f"{tipo}/{rid}"}}

    paciente = {"resourceType": "Patient", "id": id_pac,
                "identifier": [{"use": "official",
                                "type": {"coding": [{"system": SYS_TIPO_DOC, "code": p["tipo_documento"]}], "text": p["tipo_documento"]},
                                "system": SYS_DOC, "value": doc}],
                "name": [{"use": "official", "family": descifrar(bytes(p["apellido_cifrado"]), "pacientes.apellido"),
                          "given": [descifrar(bytes(p["nombre_cifrado"]), "pacientes.nombre")]}],
                "gender": {"M": "male", "F": "female"}.get(p["sexo"], "other"),
                "birthDate": p["fecha_nacimiento"].isoformat()}
    if p.get("sid_federado"):
        paciente["identifier"].append({"use": "secondary", "system": I.SYS_FEDERADO, "value": p["sid_federado"]})
    entradas = [entrada(paciente)]
    soporte = []
    for o in obs:
        recurso = {"resourceType": "Observation", "id": f"obs-{o['id']}",
                   "identifier": [{"system": SYS_OBS_EXT, "value": str(o["id"])}],
                   "status": "final",
                   "category": [{"coding": [{"system": fhir_svc.SYS_OBS_CAT, "code": o.get("categoria") or "laboratory"}]}],
                   "code": {"coding": [{"system": fhir_svc.SYS_LOINC, "code": o["codigo_loinc"],
                                        "display": o.get("display_loinc") or o["codigo_loinc"]}]},
                   "subject": {"reference": ref_pac},
                   "effectiveDateTime": o["fecha_hora"].isoformat() if o.get("fecha_hora") else _ahora()}
        if o.get("valor_numerico") is not None:
            unidad = o.get("unidad_ucum") or "1"
            recurso["valueQuantity"] = {"value": float(o["valor_numerico"]), "unit": unidad,
                                        "system": fhir_svc.SYS_UCUM, "code": unidad}
        elif o.get("valor_texto"):
            recurso["valueString"] = o["valor_texto"]
        else:
            continue
        entradas.append(entrada(recurso))
        soporte.append({"reference": f"Observation/{recurso['id']}"})
    if reporte:
        res = reporte.get("resultado") or {}
        prob = reporte.get("probabilidad")
        conclusion = (f"Modelo {reporte.get('modelo_version') or ''}: "
                      f"{res.get('clase') or ('riesgo alto' if reporte.get('critico') else 'riesgo bajo')}"
                      + (f", probabilidad {float(prob):.0%}" if prob is not None else "")
                      + (f". Revisión clínica: {reporte['observaciones']}" if reporte.get("observaciones") else "")
                      + ". Reporte revisado y APROBADO por un clínico.")
        dr = {"resourceType": "DiagnosticReport", "id": f"rep-{reporte['id']}",
              "identifier": [{"system": SYS_REMISION_EXT + "/reporte-ia", "value": str(reporte["id"])}],
              "status": "final",
              "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "OTH",
                                        "display": "Other"}], "text": "Apoyo diagnóstico con IA"}],
              "code": {"coding": [{"system": fhir_svc.SYS_LOINC, "code": "11526-1", "display": "Pathology study"}],
                       "text": "Reporte de IA aprobado"},
              "subject": {"reference": ref_pac},
              "issued": (reporte.get("revisado_at") or datetime.now(timezone.utc)).isoformat(),
              "conclusion": conclusion}
        entradas.append(entrada(dr))
        soporte.append({"reference": f"DiagnosticReport/{dr['id']}"})
    esp = None
    if r.get("especialidad_id"):
        esp = db.execute(text("SELECT nombre, codigo_snomed FROM especialidades WHERE id = :i"), {"i": r["especialidad_id"]}).first()
    sr = {"resourceType": "ServiceRequest", "id": f"rem-{n}",
          "identifier": [{"system": SYS_REMISION_EXT, "value": str(n)}],
          "status": "active", "intent": "order", "priority": PRIORIDAD_A_FHIR.get(r["prioridad"], "routine"),
          "code": {"coding": [SNOMED_REMISION], "text": "Remisión a especialista"},
          "subject": {"reference": ref_pac}, "authoredOn": _ahora(),
          "requester": {"display": INSTITUCION},
          "reasonCode": [{"text": r["motivo"]}],
          "supportingInfo": soporte}
    if esp:
        sr["performerType"] = {"coding": ([{"system": fhir_svc.SYS_SNOMED, "code": esp[1], "display": esp[0]}] if esp[1] else []),
                               "text": esp[0]}
    entradas.insert(1, entrada(sr))
    if consentimiento:
        desde = consentimiento["otorgado_at"].isoformat() if consentimiento.get("otorgado_at") else _ahora()
        entradas.append(entrada({
            "resourceType": "Consent", "id": f"con-{consentimiento['id']}", "status": "active",
            "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": "patient-privacy"}]},
            "category": [{"coding": [{"system": fhir_svc.SYS_LOINC, "code": "59284-0", "display": "Patient Consent"}]}],
            "patient": {"reference": ref_pac}, "dateTime": desde,
            "policyRule": {"text": "Ley 1581 de 2012: autorización para transferir datos a la institución destino"},
            "provision": {"type": "permit", "period": {"start": desde},
                          "purpose": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason", "code": "TREAT"}]}}))
    entradas.append(entrada({
        "resourceType": "Task", "id": f"rem-{n}", "identifier": [{"system": SYS_REMISION_EXT, "value": str(n)}],
        "status": "requested", "intent": "order", "priority": PRIORIDAD_A_FHIR.get(r["prioridad"], "routine"),
        "code": {"coding": [SNOMED_REMISION]}, "focus": {"reference": ref_sr}, "basedOn": [{"reference": ref_sr}],
        "for": {"reference": ref_pac}, "authoredOn": _ahora(), "requester": {"display": INSTITUCION}}))
    return {"resourceType": "Bundle", "type": "transaction", "timestamp": _ahora(), "entry": entradas}


def enviar(db, remision_id: int) -> dict:
    """Envía (o reenvía) una remisión saliente. Guarda el resultado; no lanza por fallas del socio."""
    r = dict(db.execute(text("SELECT * FROM remisiones WHERE id = :i AND direccion = 'saliente'"), {"i": remision_id}).mappings().first())
    socio = dict(db.execute(text("SELECT * FROM interop_socios WHERE id = :i"), {"i": r["socio_id"]}).mappings().first())
    extra = r.get("datos_externos") or {}
    reporte = None
    if extra.get("reporte_id"):
        reporte = db.execute(text("SELECT * FROM reportes_ia WHERE id = :i"), {"i": extra["reporte_id"]}).mappings().first()
        reporte = dict(reporte) if reporte else None
    obs = _observaciones_relevantes(db, r["paciente_id"], extra.get("observaciones_ids"))
    bundle = construir_bundle(db, r, reporte, obs, consentimiento_vigente(db, r["paciente_id"], r["socio_id"]))
    try:
        resp = _post_socio(socio, socio["ruta_bundle"], bundle)
        ok = resp.status_code < 300
        try:
            cuerpo = resp.json()
        except ValueError:
            cuerpo = {"texto": resp.text[:300]}
        detalle = f"HTTP {resp.status_code}"
        if not ok:
            detalle += ": " + "; ".join(str(i.get("diagnostics") or i.get("details") or "") for i in cuerpo.get("issue") or [])[:600] \
                if isinstance(cuerpo, dict) and cuerpo.get("issue") else f": {json.dumps(cuerpo, ensure_ascii=False)[:600]}"
        id_socio = (resp.headers.get("x-remision-externa") or (cuerpo.get("id") if isinstance(cuerpo, dict) else None)
                    or resp.headers.get("location"))
    except (httpx.HTTPError, ErrorEnvio) as e:
        ok, detalle, id_socio = False, f"No se pudo contactar al socio: {e}"[:700], None
    db.execute(text("""UPDATE remisiones SET estado_envio = :e, detalle_envio = :d, id_externo = COALESCE(:x, id_externo),
                       datos_externos = COALESCE(datos_externos, '{}'::jsonb) || CAST(:j AS jsonb) WHERE id = :i"""),
               {"e": "enviada" if ok else "fallida", "d": detalle, "x": str(id_socio)[:128] if id_socio else None, "i": remision_id,
                "j": json.dumps({"enviada_at": _ahora(), "observaciones_enviadas": len(obs), "reporte_enviado": bool(reporte)})})
    registrar(db, None, "remision", remision_id, "envio_externo", resultado="exito" if ok else "error",
              valor_nuevo={"origen": "pulso", "socio": socio["codigo"], "http": detalle[:80],
                           "observaciones": len(obs), "reporte": bool(reporte)})
    return {"ok": ok, "detalle": detalle, "id_socio": id_socio, "observaciones": len(obs), "reporte": bool(reporte)}


def responder_socio(remision_id: int) -> None:
    """B: tras aceptar o rechazar una remisión entrante, le envía el Task al socio (en segundo plano)."""
    from app.core.deps import SessionLocal    # noqa: PLC0415
    with SessionLocal() as db:
        r = db.execute(text("SELECT * FROM remisiones WHERE id = :i AND direccion = 'entrante'"), {"i": remision_id}).mappings().first()
        if r is None or r["socio_id"] is None:
            return          # sin socio configurado: el socio consulta GET /interop/Task/{id}
        socio = dict(db.execute(text("SELECT * FROM interop_socios WHERE id = :i AND activo"), {"i": r["socio_id"]}).mappings().first() or {})
        if not socio:
            return
        tarea = task_de(db, dict(r))
        extra = r["datos_externos"] or {}
        sr_ref = {"reference": f"ServiceRequest/{extra['sr_id']}"} if extra.get("sr_id") else {}
        tarea["basedOn"] = [{**sr_ref, "identifier": {"value": r["id_externo"]}, "type": "ServiceRequest"}]
        tarea["focus"] = {**sr_ref, "identifier": {"value": r["id_externo"]}, "type": "ServiceRequest"}
        if extra.get("patient_id"):
            tarea["for"] = {"reference": f"Patient/{extra['patient_id']}"}
        if extra.get("task_id"):
            tarea["id"] = extra["task_id"]           # el socio reconoce su propio Task
        tarea["identifier"] = [{"system": SYS_REMISION_EXT, "value": str(r["id"])}]
        try:
            resp = _post_socio(socio, socio["ruta_task"], tarea)
            ok, detalle = resp.status_code < 300, f"Respuesta al socio: HTTP {resp.status_code}"
            if not ok:
                detalle += f" {resp.text[:300]}"
        except (httpx.HTTPError, ErrorEnvio) as e:
            ok, detalle = False, f"No se pudo avisar al socio: {e}"[:500]
        db.execute(text("UPDATE remisiones SET estado_envio = :e, detalle_envio = :d WHERE id = :i"),
                   {"e": "respondida" if ok else "fallida", "d": detalle, "i": remision_id})
        registrar(db, None, "remision", remision_id, "respuesta_a_socio", resultado="exito" if ok else "error",
                  valor_nuevo={"origen": "pulso", "socio": socio["codigo"], "estado": r["estado"], "http": detalle[:80]})
        db.commit()


# =============================================================== socios
def crear_socio(db, codigo: str, nombre: str, base_url: str, modo_auth: str = "oauth2", credencial_id: str | None = None,
                credencial_secreto: str | None = None, firma_secreto: str | None = None, client_id: str | None = None,
                **rutas) -> int:
    if modo_auth not in ("oauth2", "api_key", "bearer", "ninguno"):
        raise ValueError("modo_auth: oauth2, api_key, bearer o ninguno")
    fila = db.execute(text("""
        INSERT INTO interop_socios (codigo, nombre, base_url, modo_auth, credencial_id, credencial_secreto_cifrado,
                                    firma_secreto_cifrado, client_id, ruta_bundle, ruta_task, ruta_token, api_key_cabecera)
        VALUES (:c, :n, :u, :m, :ci, :cs, :fs, :cl, :rb, :rt, :rk, :h) RETURNING id"""),
        {"c": codigo, "n": nombre, "u": base_url.rstrip("/"), "m": modo_auth, "ci": credencial_id,
         "cs": cifrar(credencial_secreto, f"interop_socios.secreto.{codigo}") if credencial_secreto else None,
         "fs": cifrar(firma_secreto, f"interop_socios.firma.{codigo}") if firma_secreto else None, "cl": client_id,
         "rb": rutas.get("ruta_bundle") or "/interop/Bundle", "rt": rutas.get("ruta_task") or "/interop/Task",
         "rk": rutas.get("ruta_token") or "/oauth/token", "h": rutas.get("api_key_cabecera") or "X-API-Key"}).first()
    return fila[0]


def actualizar_socio(db, codigo: str, **campos) -> bool:
    """Cambia la URL (el túnel cambia al reiniciar), rutas, modo o credenciales sin redesplegar."""
    sets, params = [], {"c": codigo}
    for k in ("nombre", "base_url", "ruta_bundle", "ruta_task", "ruta_token", "modo_auth", "credencial_id",
              "api_key_cabecera", "activo", "client_id"):
        if campos.get(k) is not None:
            sets.append(f"{k} = :{k}")
            params[k] = campos[k].rstrip("/") if k == "base_url" else campos[k]
    if campos.get("credencial_secreto") is not None:
        sets.append("credencial_secreto_cifrado = :cs")
        params["cs"] = cifrar(campos["credencial_secreto"], f"interop_socios.secreto.{codigo}") if campos["credencial_secreto"] else None
    if campos.get("firma_secreto") is not None:
        sets.append("firma_secreto_cifrado = :fs")
        params["fs"] = cifrar(campos["firma_secreto"], f"interop_socios.firma.{codigo}") if campos["firma_secreto"] else None
    if not sets:
        return False
    return bool(db.execute(text(f"UPDATE interop_socios SET {', '.join(sets)}, updated_at = now() WHERE codigo = :c"),
                           params).rowcount)


def vista_socio(s: dict) -> dict:
    """Lo que se puede mostrar de un socio: nunca sus secretos."""
    return {"id": s["id"], "codigo": s["codigo"], "nombre": s["nombre"], "base_url": s["base_url"],
            "ruta_bundle": s["ruta_bundle"], "ruta_task": s["ruta_task"], "ruta_token": s["ruta_token"],
            "modo_auth": s["modo_auth"], "credencial_id": s.get("credencial_id"),
            "tiene_secreto": bool(s.get("credencial_secreto_cifrado")), "firma": bool(s.get("firma_secreto_cifrado")),
            "client_id": s.get("client_id"), "activo": s["activo"]}
