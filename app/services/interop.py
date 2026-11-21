"""
Interoperabilidad entre sistemas (Semana 14, docs/INTEROP.md).

Otro sistema le manda a Pulso una Observation FHIR R4. Cuatro capas, cada una por una razón:

  1. OAuth2 client_credentials  → ¿quién eres? Un JWT HS256 de 10 minutos, con scope y audiencia.
  2. Firma HMAC por mensaje     → ¿este mensaje es tuyo y llegó intacto? Un token robado no basta.
  3. Nonce y marca de tiempo    → ¿no es una repetición de un mensaje viejo?
  4. Identificador federado     → ¿de qué paciente hablas, sin mandarme su cédula?

Este módulo no sabe de HTTP: lo usa app/api/interop.py y lo prueban scripts/test_interop.py y
scripts/interop_probar.py (que comparten las MISMAS funciones de firma, para no desfasarse).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from sqlalchemy import text

from app.core.config import get_settings
from app.core.crypto import cifrar, descifrar
from app.core.security import hash_password, verificar_password
from app.services import fhir as fhir_svc

ALGORITMO = "HS256"
SCOPE_OBSERVATION = "system/Observation.write"
SCOPE_REMISION = "system/ServiceRequest.write"     # enviarnos una remisión (Bundle)
SCOPE_TASK = "system/Task.write"                   # respondernos una remisión (Task)
SCOPES_VALIDOS = {SCOPE_OBSERVATION, SCOPE_REMISION, SCOPE_TASK}
TODOS_LOS_SCOPES = " ".join(sorted(SCOPES_VALIDOS))

# Ruta que se firma: la de la API, sin el prefijo /api que pone nginx (así la firma no depende del proxy).
RUTA_OBSERVATION = "/interop/Observation"
VERSION_FIRMA = "v1"
CABECERAS = {"timestamp": "X-Timestamp", "nonce": "X-Nonce", "firma": "X-Signature"}

SYS_FEDERADO = "https://pulso.hsanrafael.co/fhir/identifier/paciente-federado"
ESTADOS = {"registered", "preliminary", "final", "amended", "corrected", "cancelled"}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$")
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


class ErrorInterop(Exception):
    """Un rechazo con su código HTTP y, para FHIR, el tipo de problema del OperationOutcome."""

    def __init__(self, http: int, detalle: str, tipo: str = "invalid", errores: list[str] | None = None,
                 oauth: str | None = None):
        super().__init__(detalle)
        self.http, self.detalle, self.tipo, self.errores, self.oauth = http, detalle, tipo, errores or [], oauth


# --------------------------------------------------------------- identificador federado
def _secreto_federado() -> bytes:
    s = get_settings().interop_secreto_federado
    if len(s) < 16:
        raise RuntimeError("Falta INTEROP_SECRETO_FEDERADO (mínimo 16 caracteres)")
    return s.encode("utf-8")


def sid_federado(tipo_documento: str, documento: str) -> str:
    """HMAC-SHA256(secreto federado, «TIPO|documento»), 32 primeros caracteres hexadecimales."""
    tipo = re.sub(r"\s+", "", str(tipo_documento or "")).upper()
    doc = re.sub(r"[^0-9A-Za-z]", "", str(documento or "")).upper()
    if not tipo or not doc:
        raise ValueError("Falta el tipo o el número de documento")
    return hmac.new(_secreto_federado(), f"{tipo}|{doc}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


# --------------------------------------------------------------- firma por mensaje
def cadena_a_firmar(timestamp: str, nonce: str, metodo: str, ruta: str, cuerpo: bytes) -> bytes:
    """
    Lo que se firma: versión, marca de tiempo, nonce, método, ruta y el SHA-256 del cuerpo, separados
    por saltos de línea. Incluir método y ruta evita que una firma de un endpoint valga en otro; incluir
    el hash del cuerpo, que nadie cambie el valor sin invalidarla.
    """
    return "\n".join([VERSION_FIRMA, timestamp, nonce, metodo.upper(), ruta,
                      hashlib.sha256(cuerpo).hexdigest()]).encode("utf-8")


def firmar(secreto: str | bytes, timestamp: str, nonce: str, metodo: str, ruta: str, cuerpo: bytes) -> str:
    clave = secreto.encode("utf-8") if isinstance(secreto, str) else secreto
    mac = hmac.new(clave, cadena_a_firmar(timestamp, nonce, metodo, ruta, cuerpo), hashlib.sha256).hexdigest()
    return f"{VERSION_FIRMA}={mac}"


# --------------------------------------------------------------- clientes
def crear_cliente(db, nombre: str, scopes: str = TODOS_LOS_SCOPES, exige_firma: bool = True) -> dict:
    """Crea un cliente y devuelve sus secretos EN CLARO, una sola vez (en la base solo queda el hash)."""
    lista = sorted(set(scopes.split()))
    if not lista or not set(lista) <= SCOPES_VALIDOS:
        raise ValueError(f"Scopes válidos: {', '.join(sorted(SCOPES_VALIDOS))}")
    client_id = "cli_" + secrets.token_hex(8)
    secreto = secrets.token_urlsafe(32)
    firma = secrets.token_urlsafe(32)
    db.execute(text("""INSERT INTO interop_clientes (client_id, nombre, secreto_hash, firma_secreto_cifrado, scopes, exige_firma)
                       VALUES (:c, :n, :h, :f, :s, :x)"""),
               {"c": client_id, "n": nombre[:120], "h": hash_password(secreto),
                "f": cifrar(firma, f"interop_clientes.firma.{client_id}"), "s": " ".join(lista), "x": exige_firma})
    return {"client_id": client_id, "client_secret": secreto, "firma_secreto": firma, "scopes": " ".join(lista),
            "api_key": f"{client_id}.{secreto}", "exige_firma": exige_firma}


_HASH_FALSO = hash_password("no-existe-este-cliente")      # para gastar el mismo tiempo si el cliente no existe


def autenticar_cliente(db, client_id: str, client_secret: str) -> dict:
    fila = db.execute(text("SELECT client_id, secreto_hash, scopes, activo FROM interop_clientes WHERE client_id = :c"),
                      {"c": client_id[:64]}).mappings().first()
    ok = verificar_password(client_secret or "", fila["secreto_hash"] if fila else _HASH_FALSO)
    if not (fila and ok and fila["activo"]):
        raise ErrorInterop(401, "Credenciales inválidas", oauth="invalid_client")      # igual para todos los casos
    db.execute(text("UPDATE interop_clientes SET ultimo_uso = now() WHERE client_id = :c"), {"c": client_id})
    return dict(fila)


# --------------------------------------------------------------- OAuth2: emitir y leer el token
def emitir_token(db, grant_type: str, client_id: str, client_secret: str, scope: str | None) -> dict:
    if grant_type != "client_credentials":
        raise ErrorInterop(400, "Solo se admite client_credentials", oauth="unsupported_grant_type")
    cliente = autenticar_cliente(db, client_id, client_secret)
    permitidos = set(cliente["scopes"].split())
    pedidos = set((scope or cliente["scopes"]).split())
    if not pedidos or not pedidos <= permitidos:
        raise ErrorInterop(400, "Scope no permitido para este cliente", oauth="invalid_scope")
    cfg = get_settings()
    ahora = datetime.now(timezone.utc)
    minutos = cfg.interop_token_minutos
    token = jwt.encode({"iss": cfg.interop_emisor, "aud": cfg.interop_audiencia, "sub": client_id,
                        "scope": " ".join(sorted(pedidos)), "type": "interop", "iat": ahora,
                        "exp": ahora + timedelta(minutes=minutos), "jti": secrets.token_urlsafe(12)},
                       _secreto_jwt(), algorithm=ALGORITMO)
    return {"access_token": token, "token_type": "Bearer", "expires_in": minutos * 60, "scope": " ".join(sorted(pedidos))}


def _secreto_jwt() -> str:
    from app.core.security import _secreto_jwt as s      # noqa: PLC0415 -- la misma clave que el resto de la API
    return s()


def leer_token(autorizacion: str | None, scope_requerido: str, quien: dict | None = None) -> dict:
    """Valida el Bearer: firma, vencimiento, emisor, audiencia, tipo y scope. 401 si no es válido; 403 si falta el scope."""
    if not autorizacion or not autorizacion.lower().startswith("bearer "):
        raise ErrorInterop(401, "Falta el token (Authorization: Bearer ...)", "login", oauth="invalid_token")
    cfg = get_settings()
    try:
        datos = jwt.decode(autorizacion[7:].strip(), _secreto_jwt(), algorithms=[ALGORITMO],
                           audience=cfg.interop_audiencia, issuer=cfg.interop_emisor,
                           options={"require_aud": True, "require_iss": True, "require_exp": True, "require_sub": True})
    except JWTError:
        raise ErrorInterop(401, "Token inválido o vencido", "login", oauth="invalid_token") from None
    if datos.get("type") != "interop":
        raise ErrorInterop(401, "Token inválido", "login", oauth="invalid_token")
    if quien is not None:
        quien["client_id"] = datos.get("sub")            # para auditar el rechazo con el cliente correcto
    if scope_requerido not in str(datos.get("scope", "")).split():
        raise ErrorInterop(403, f"El token no tiene el scope {scope_requerido}", "forbidden", oauth="insufficient_scope")
    return datos


# --------------------------------------------------------------- verificar la firma y el nonce
def verificar_firma(db, client_id: str, cabeceras: dict[str, str | None], metodo: str, ruta: str, cuerpo: bytes) -> None:
    ts, nonce, firma = (cabeceras.get(k) for k in ("timestamp", "nonce", "firma"))
    if not (ts and nonce and firma):
        raise ErrorInterop(401, f"Faltan cabeceras: {CABECERAS['timestamp']}, {CABECERAS['nonce']}, {CABECERAS['firma']}", "security")
    ventana = get_settings().interop_ventana_segundos
    try:
        marca = int(ts)
    except ValueError:
        raise ErrorInterop(401, "X-Timestamp debe ser segundos Unix", "security") from None
    if abs(datetime.now(timezone.utc).timestamp() - marca) > ventana:
        raise ErrorInterop(401, f"X-Timestamp fuera de la ventana de {ventana} s (revisa el reloj)", "security")
    if not _NONCE.match(nonce):
        raise ErrorInterop(401, "X-Nonce inválido (16 a 128 caracteres: letras, números, _ y -)", "security")
    fila = db.execute(text("SELECT firma_secreto_cifrado FROM interop_clientes WHERE client_id = :c AND activo"),
                      {"c": client_id}).first()
    if not fila:
        raise ErrorInterop(401, "Cliente inactivo", "security")
    secreto = descifrar(bytes(fila[0]), f"interop_clientes.firma.{client_id}") or ""
    esperada = firmar(secreto, ts, nonce, metodo, ruta, cuerpo)
    if not hmac.compare_digest(esperada.encode(), firma.encode()):
        raise ErrorInterop(401, "Firma inválida", "security")
    # El nonce se anota DESPUÉS de verificar la firma: así quien no sabe firmar no puede llenar la tabla.
    db.execute(text("DELETE FROM interop_nonces WHERE expira < now()"))
    nuevo = db.execute(text("""INSERT INTO interop_nonces (client_id, nonce, expira)
                               VALUES (:c, :n, now() + make_interval(secs => :v)) ON CONFLICT DO NOTHING RETURNING 1"""),
                       {"c": client_id, "n": nonce, "v": ventana * 2}).first()
    if not nuevo:
        raise ErrorInterop(401, "Nonce repetido: este mensaje ya se había enviado", "security")


# --------------------------------------------------------------- validar la Observation
def validar_observation(obs) -> list[str]:
    """Lista de problemas (vacía si está bien). Solo lo que Pulso necesita para guardarla sin ambigüedad."""
    if not isinstance(obs, dict):
        return ["El cuerpo debe ser un objeto JSON"]
    e: list[str] = []
    if obs.get("resourceType") != "Observation":
        e.append("resourceType debe ser 'Observation'")
    if not _ID.match(str(obs.get("id") or "")):
        e.append("id es obligatorio (1 a 64 caracteres: letras, números, '-' y '.'); con él se evita duplicar el mensaje")
    if obs.get("status") not in ESTADOS:
        e.append(f"status debe ser uno de: {', '.join(sorted(ESTADOS))}")
    loinc = next((c for c in (obs.get("code") or {}).get("coding") or [] if c.get("system") == fhir_svc.SYS_LOINC), None)
    if not loinc or not re.match(r"^\d{1,7}-\d$", str(loinc.get("code") or "")):
        e.append(f"code.coding debe traer un código LOINC (system {fhir_svc.SYS_LOINC}), por ejemplo 2339-0")
    q = obs.get("valueQuantity")
    if not isinstance(q, dict) or isinstance(q.get("value"), bool) or not isinstance(q.get("value"), (int, float)):
        e.append("valueQuantity.value debe ser un número")
    elif q.get("system") != fhir_svc.SYS_UCUM or not q.get("code"):
        e.append(f"valueQuantity necesita la unidad UCUM: system {fhir_svc.SYS_UCUM} y code (por ejemplo mg/dL)")
    ident = (obs.get("subject") or {}).get("identifier") or {}
    if ident.get("system") != SYS_FEDERADO or not re.match(r"^[0-9a-f]{32}$", str(ident.get("value") or "")):
        e.append(f"subject.identifier debe ser el identificador federado (system {SYS_FEDERADO}, value de 32 hex); "
                 "no se aceptan cédulas ni nombres")
    fecha = obs.get("effectiveDateTime")
    if not fecha or not _ISO.match(str(fecha)):
        e.append("effectiveDateTime es obligatorio y debe ser una fecha ISO 8601")
    return e


# --------------------------------------------------------------- recibir y conciliar
def _paciente_por_sid(db, sid: str) -> str | None:
    filas = db.execute(text("SELECT documento_bidx FROM pacientes WHERE sid_federado = :s AND activo"), {"s": sid}).all()
    return filas[0][0] if len(filas) == 1 else None      # si por azar hubiera dos, mejor conciliar a mano que adivinar


def _procesar(db, obs: dict, client_id: str, paciente_id: str) -> dict:
    from app.services import fhir_reactivo, reactores        # noqa: PLC0415
    interno = dict(obs, id=f"{client_id}:{obs['id']}"[:64])  # el id propio del par no choca con los de otros orígenes
    r = fhir_reactivo.procesar_observacion(db, interno, paciente_id=paciente_id)
    oid = r.get("observacion_id")
    if oid and db.execute(text("SELECT es_critico FROM observaciones WHERE id = :i"), {"i": oid}).scalar():
        dest = fhir_reactivo._clinicos_responsables(db, paciente_id)       # noqa: SLF001
        for u in dest or [None]:
            a = db.execute(text("""
                INSERT INTO alertas (tipo, severidad, titulo, mensaje, entidad_tipo, entidad_id, dirigida_a_usuario,
                                     dirigida_a_rol, paciente_id, estado)
                VALUES ('resultado_critico_externo', 'critica', 'Resultado crítico de otro sistema',
                        :m, 'observacion', :o, :u, CASE WHEN CAST(:u AS integer) IS NULL
                                                        THEN (SELECT id FROM roles WHERE codigo = 'medico') END, :p, 'activa')
                RETURNING *"""), {"m": "Un sistema externo envió un resultado crítico. Revise la historia y reconozca la alerta.",
                                  "o": oid, "u": u, "p": paciente_id}).mappings().first()
            if u is not None:
                reactores.al_alerta_critica(db, a)
        r["alerta_critica"] = True
    return r


def recibir_observation(db, client_id: str, obs: dict) -> tuple[int, dict]:
    """
    (http, respuesta): 201 si se guardó; 202 si se aceptó pero el paciente federado aún no existe aquí;
    409 si ya se había recibido el mismo id. Las validaciones (422) las hace antes el llamador.
    """
    idx = obs["id"]
    sid = obs["subject"]["identifier"]["value"]
    nuevo = db.execute(text("""INSERT INTO interop_mensajes (client_id, id_externo, sid_federado, estado, cuerpo)
                               VALUES (:c, :i, :s, 'pendiente', CAST(:b AS jsonb))
                               ON CONFLICT (client_id, id_externo) DO NOTHING RETURNING id"""),
                       {"c": client_id, "i": idx, "s": sid, "b": json.dumps(obs)}).first()
    if not nuevo:
        raise ErrorInterop(409, f"La Observation {idx} ya fue recibida de este cliente", "duplicate")
    paciente_id = _paciente_por_sid(db, sid)
    if paciente_id is None:
        return 202, {"estado": "pendiente", "mensaje": "Recibida. El paciente federado aún no existe en Pulso; "
                                                       "se asociará cuando se registre.", "id": idx}
    return _aceptar(db, nuevo[0], client_id, obs, paciente_id)


def _aceptar(db, mensaje_id: int, client_id: str, obs: dict, paciente_id: str) -> tuple[int, dict]:
    r = _procesar(db, obs, client_id, paciente_id)
    db.execute(text("""UPDATE interop_mensajes SET estado = 'aceptada', observacion_id = :o, cuerpo = NULL,
                       conciliado_at = now() WHERE id = :i"""), {"o": r.get("observacion_id"), "i": mensaje_id})
    return 201, {"estado": "aceptada", "id": obs["id"], "observacion_id": r.get("observacion_id"),
                 "alerta_critica": bool(r.get("alerta_critica"))}


def conciliar(db, sid: str | None = None) -> dict:
    """Asocia los mensajes pendientes cuyo paciente federado ya existe (de uno solo si se da `sid`)."""
    filas = db.execute(text("""SELECT id, client_id, cuerpo, sid_federado FROM interop_mensajes
                               WHERE estado = 'pendiente' AND (CAST(:s AS text) IS NULL OR sid_federado = :s) ORDER BY id"""),
                       {"s": sid}).mappings().all()
    hechos = pendientes = 0
    for f in filas:
        p = _paciente_por_sid(db, f["sid_federado"])
        if p is None:
            pendientes += 1
            continue
        with db.begin_nested():
            _aceptar(db, f["id"], f["client_id"], f["cuerpo"], p)
        hechos += 1
    return {"conciliados": hechos, "siguen_pendientes": pendientes}


def asignar_sid(db, paciente_id: str, tipo_documento: str, documento: str) -> str | None:
    """Calcula y guarda el identificador federado de un paciente. None si no hay secreto federado configurado."""
    try:
        sid = sid_federado(tipo_documento, documento)
    except (RuntimeError, ValueError):
        return None
    db.execute(text("UPDATE pacientes SET sid_federado = :s WHERE documento_bidx = :p"), {"s": sid, "p": paciente_id})
    return sid


# --------------------------------------------------------------- entrada genérica (R29, R31)
def autenticar_entrada(db, autorizacion: str | None, api_key: str | None, cabeceras: dict[str, str | None],
                       metodo: str, ruta: str, cuerpo: bytes, scope: str, quien: dict | None = None) -> str:
    """
    Autentica a un SISTEMA (nunca a un humano) en una ruta de entrada y devuelve su client_id.

    Dos formas, para poder hablar con cualquier equipo:
      · Authorization: Bearer <token de /oauth/token>   (OAuth2 client_credentials)
      · X-API-Key: <client_id>.<client_secret>            (API key por institución)
    Además, si el cliente tiene exige_firma, el mensaje debe venir firmado con HMAC (X-Timestamp,
    X-Nonce, X-Signature). 401 si la credencial no vale; 403 si le falta el scope de esta ruta.
    """
    if autorizacion and autorizacion.lower().startswith("bearer "):
        cid = leer_token(autorizacion, scope, quien)["sub"]
    elif api_key:
        cid, _, secreto = api_key.strip().partition(".")
        cliente = autenticar_cliente(db, cid, secreto)          # 401 uniforme si no vale
        if quien is not None:
            quien["client_id"] = cid
        if scope not in cliente["scopes"].split():
            raise ErrorInterop(403, f"La credencial no tiene el scope {scope}", "forbidden")
    else:
        raise ErrorInterop(401, "Falta la credencial del sistema (Authorization: Bearer ... o X-API-Key)", "login")
    exige = db.execute(text("SELECT exige_firma FROM interop_clientes WHERE client_id = :c AND activo"),
                       {"c": cid}).scalar()
    if exige is None:
        raise ErrorInterop(401, "Cliente inactivo", "security")
    if exige or cabeceras.get("firma"):          # si viene firma, se verifica aunque no se exija
        verificar_firma(db, cid, cabeceras, metodo, ruta, cuerpo)
    return cid
