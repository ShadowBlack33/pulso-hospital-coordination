"""
Auditoría encadenada (Semana 14, STRIDE: repudio y manipulación del registro).

log_auditoria ya es append-only (trigger que impide UPDATE y DELETE), pero quien tenga acceso
de administrador a la base puede desactivar el trigger y editar. La cadena lo hace detectable:

    huella = HMAC-SHA256(clave_de_auditoría, huella_anterior || contenido canónico del registro)

· La huella se calcula en la aplicación, con una clave que la base no tiene. Quien edite la base
  no puede recalcular la cadena: con un SHA-256 simple sí podría; con el HMAC, no.
· Los registros se sellan en orden: una exclusión mutua en la base (pg_advisory_xact_lock)
  serializa a los que escriben, y el id sale de la secuencia dentro de esa exclusión, así que el
  orden por id es el orden de la cadena. El candado se suelta al terminar la transacción.
· Limitación declarada: borrar los ÚLTIMOS registros no rompe ningún enlace. Para detectarlo hay
  que comparar `ultima_huella` y `total` con una copia que el administrador guarde aparte
  (ver docs/SEGURIDAD.md §7.5).

Clave: APP_AUDIT_KEY (32 bytes en base64 url-safe). Si no está, se deriva con HKDF de
APP_ENCRYPTION_KEY (otra clave, aunque del mismo origen); en producción conviene una propia.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import instance_state

from app.models import LogAuditoria

GENESIS = "0" * 64
_CANDADO = 7281001                      # pg_advisory_xact_lock: una sola clave para toda la cadena
_CAMPOS = ("id", "usuario_id", "rol_codigo", "entidad_tipo", "entidad_id", "operacion", "resultado",
           "valor_anterior", "valor_nuevo", "ip_origen", "user_agent", "endpoint", "timestamp")


class ClaveDeAuditoriaNoConfigurada(RuntimeError):
    pass


def _clave() -> bytes:
    propia = os.getenv("APP_AUDIT_KEY", "")
    if propia:
        clave = base64.urlsafe_b64decode(propia + "=" * (-len(propia) % 4))
        if len(clave) != 32:
            raise ClaveDeAuditoriaNoConfigurada("APP_AUDIT_KEY debe decodificar a 32 bytes")
        return clave
    base = os.getenv("APP_ENCRYPTION_KEY", "")
    if not base:
        raise ClaveDeAuditoriaNoConfigurada("Falta APP_AUDIT_KEY (o APP_ENCRYPTION_KEY, de la que se deriva)")
    maestra = base64.urlsafe_b64decode(base + "=" * (-len(base) % 4))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"pulso-auditoria-cadena-v1").derive(maestra)


# ---------------------------------------------------------------- contenido canónico
def _ip(valor) -> str | None:
    """La misma forma que devuelve host(inet) de PostgreSQL, para que escribir y verificar coincidan."""
    if not valor:
        return None
    ip = ipaddress.ip_address(str(valor).split("/")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        return f"::ffff:{ip.ipv4_mapped}"
    return str(ip)


def _json(v):
    """Lo que PostgreSQL guardará y devolverá del jsonb (claves como texto, sin tipos raros)."""
    return None if v is None else json.loads(json.dumps(v, default=str, ensure_ascii=False))


def _utc(ts) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonico(f: dict) -> bytes:
    contenido = {
        "id": int(f["id"]),
        "usuario_id": None if f["usuario_id"] is None else int(f["usuario_id"]),
        "rol_codigo": f["rol_codigo"],
        "entidad_tipo": f["entidad_tipo"],
        "entidad_id": None if f["entidad_id"] is None else str(f["entidad_id"]),
        "operacion": f["operacion"],
        "resultado": f["resultado"],
        "valor_anterior": _json(f["valor_anterior"]),
        "valor_nuevo": _json(f["valor_nuevo"]),
        "ip_origen": _ip(f["ip_origen"]),
        "user_agent": f["user_agent"],
        "endpoint": f["endpoint"],
        "timestamp": _utc(f["timestamp"]),
    }
    return json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def calcular(huella_anterior: str, fila: dict) -> str:
    return hmac.new(_clave(), huella_anterior.encode("ascii") + b"|" + canonico(fila), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- sellar al escribir
@event.listens_for(Session, "before_flush")
def sellar(session: Session, contexto, instancias) -> None:
    """Antes de insertar registros nuevos de log_auditoria: les da id, hora, enlace y huella, en orden."""
    nuevos = [o for o in session.new if isinstance(o, LogAuditoria) and o.huella is None]
    if not nuevos:
        return
    nuevos.sort(key=lambda o: instance_state(o).insert_order)
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CANDADO})
    ultima = session.execute(text(
        "SELECT huella FROM log_auditoria WHERE huella IS NOT NULL ORDER BY id DESC LIMIT 1")).scalar()
    anterior = ultima or GENESIS
    for o in nuevos:
        o.id = session.execute(text("SELECT nextval('log_auditoria_id_seq')")).scalar()
        o.timestamp = datetime.now(timezone.utc)
        o.huella_anterior = anterior
        o.huella = anterior = calcular(anterior, {c: getattr(o, c) for c in _CAMPOS})


# ---------------------------------------------------------------- verificar
def verificar(db: Session, ultimos: int | None = None, max_problemas: int = 10) -> dict:
    """
    Recorre la cadena y recalcula cada huella. Devuelve:
      integra, total (registros revisados), sellados, anteriores_a_la_cadena (sin huella, previos al
      primer sellado), ultima_huella, desde_id, hasta_id y `problemas` (los primeros), cada uno con
      id y tipo: 'huella' (el contenido cambió), 'enlace' (falta o sobra un registro, o se movió) o
      'sin_huella' (un registro sin sellar dentro de la cadena).
    Con `ultimos` revisa solo los últimos N (el primero se compara con su propia huella_anterior).
    """
    filtro, params = "", {}
    if ultimos:
        filtro = "WHERE id >= COALESCE((SELECT MIN(id) FROM (SELECT id FROM log_auditoria ORDER BY id DESC LIMIT :n) t), 0)"
        params = {"n": ultimos}
    filas = db.execute(text(f"""
        SELECT id, usuario_id, rol_codigo, entidad_tipo, entidad_id, operacion, resultado,
               valor_anterior, valor_nuevo, host(ip_origen) AS ip_origen, user_agent, endpoint,
               timestamp, huella_anterior, huella
        FROM log_auditoria {filtro} ORDER BY id
    """), params).mappings()
    r = {"integra": True, "total": 0, "sellados": 0, "anteriores_a_la_cadena": 0, "ultima_huella": None,
         "desde_id": None, "hasta_id": None, "problemas": []}
    esperado, empezo = None, False

    def problema(fila, tipo: str, detalle: str) -> None:
        r["integra"] = False
        if len(r["problemas"]) < max_problemas:
            r["problemas"].append({"id": fila["id"], "tipo": tipo, "detalle": detalle})

    for f in filas:
        f = dict(f)
        r["total"] += 1
        r["desde_id"] = r["desde_id"] or f["id"]
        r["hasta_id"] = f["id"]
        if not f["huella"]:
            if empezo:
                problema(f, "sin_huella", "registro sin sellar dentro de la cadena")
            else:
                r["anteriores_a_la_cadena"] += 1
            continue
        if not empezo:
            empezo = True
            esperado = f["huella_anterior"] if (ultimos or f["huella_anterior"] != GENESIS) else GENESIS
            if not ultimos and f["huella_anterior"] != GENESIS:
                problema(f, "enlace", "el primer registro sellado no empieza en el origen de la cadena")
        r["sellados"] += 1
        if f["huella_anterior"] != esperado:
            problema(f, "enlace", "no enlaza con el registro anterior (falta, sobra o se movió uno)")
        if not hmac.compare_digest(calcular(f["huella_anterior"], f), f["huella"]):
            problema(f, "huella", "el contenido del registro no coincide con su huella")
        esperado = f["huella"]
        r["ultima_huella"] = f["huella"]
    return r


def ultimos_registros(db: Session, limite: int = 20) -> list[dict]:
    """Los últimos registros con huellas cortas, para mostrar la cadena en pantalla."""
    filas = db.execute(text("""
        SELECT id, timestamp, rol_codigo, entidad_tipo, operacion, resultado,
               LEFT(huella_anterior, 12) AS anterior, LEFT(huella, 12) AS huella
        FROM log_auditoria ORDER BY id DESC LIMIT :n"""), {"n": limite}).mappings()
    return [dict(f) for f in filas][::-1]
