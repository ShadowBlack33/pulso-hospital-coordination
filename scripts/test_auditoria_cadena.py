#!/usr/bin/env python3
"""
Pruebas de la auditoría encadenada (Semana 14). Necesitan una base con db/05_seguridad_e_interop.sql
aplicado (la de .env, o la del contenedor). AGREGAN registros de prueba a log_auditoria y simulan
a un atacante con acceso a la base DENTRO de transacciones que se deshacen: nada se edita ni se
borra de verdad. Aun así, por seguridad, se niega a correr contra una base que no sea local
(localhost, 127.0.0.1, db, postgres) salvo que se pase --forzar.

  1. Sellado: enlazan en orden, también varios en un mismo flush y desde varios hilos a la vez.
  2. Un rollback no deja huecos ni registros fantasma en la cadena.
  3. Un atacante con acceso a la base: editar, borrar, reordenar, insertar sin huella y hasta
     "recalcular" con SHA-256 simple se detectan, con el id del primer registro roto.
  4. Limitación declarada: borrar los ÚLTIMOS registros no rompe ningún enlace (se detecta con
     `ultima_huella` y `total` guardados aparte).
  5. La clave importa: con otra clave la cadena no valida.
  6. Endpoint de verificación para el admin.

Uso:  python scripts/test_auditoria_cadena.py
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
for var in ("JWT_SECRET_KEY", "APP_ENCRYPTION_KEY", "APP_BLIND_INDEX_KEY", "IA_SEUDONIMO_KEY"):
    os.environ.setdefault(var, base64.urlsafe_b64encode(b"k" * 32).decode())

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.deps import SessionLocal  # noqa: E402
from app.services import auditoria_cadena as C  # noqa: E402
from app.services.auditoria import registrar  # noqa: E402

host = (get_settings().database_url.split("@")[-1].split("/")[0].split(":")[0] or "").lower()
if host not in {"localhost", "127.0.0.1", "db", "postgres", ""} and "--forzar" not in sys.argv:
    sys.exit(f"Esta prueba escribe registros de prueba; la base es «{host}», no local. Usa --forzar si estás seguro.")

fallos = 0


def check(nombre: str, ok: bool, detalle=None) -> None:
    global fallos
    print(f"  {'OK  ' if ok else 'FALLA'}  {nombre}")
    if not ok:
        fallos += 1
        if detalle is not None:
            print("        ", str(detalle)[:400])


def escribir(db, n: int, prefijo: str = "prueba") -> None:
    for i in range(n):
        registrar(db, None, "prueba_cadena", f"{prefijo}-{i}", "prueba", valor_nuevo={"i": i, "texto": "ñandú ✓"})
    db.commit()


@contextmanager
def atacante(db):
    """Acceso a la base con el trigger de inmutabilidad desactivado; todo se deshace al salir."""
    db.execute(text("ALTER TABLE log_auditoria DISABLE TRIGGER trg_auditoria_no_update"))
    try:
        yield
    finally:
        db.rollback()


def pruebas_ids(db, n: int = 12) -> list[int]:
    return [r[0] for r in db.execute(text(
        "SELECT id FROM log_auditoria WHERE entidad_tipo = 'prueba_cadena' AND huella IS NOT NULL "
        "ORDER BY id DESC LIMIT :n"), {"n": n})][::-1]


db = SessionLocal()
db.execute(text("SELECT 1 FROM log_auditoria WHERE huella IS NULL LIMIT 1"))     # falla si falta la migración
previos = db.execute(text("SELECT COUNT(*) FROM log_auditoria WHERE huella IS NULL")).scalar()

print("\n[1] Sellado")
antes = C.verificar(db)
check("La cadena existente es íntegra antes de empezar", antes["integra"], antes)
escribir(db, 5)
r = C.verificar(db)
check("5 registros nuevos: la cadena sigue íntegra y crece en 5", r["integra"] and r["sellados"] == antes["sellados"] + 5, r)
ult = db.execute(text("SELECT id, huella_anterior, huella FROM log_auditoria WHERE huella IS NOT NULL ORDER BY id DESC LIMIT 2")).all()
check("Cada registro enlaza con la huella del anterior", ult[0][1] == ult[1][2], ult)
check("Las huellas son HMAC hexadecimales de 64 caracteres", all(len(u[2]) == 64 for u in ult))
check("Los registros anteriores a la cadena se cuentan aparte, sin romperla",
      r["anteriores_a_la_cadena"] == previos, (r["anteriores_a_la_cadena"], previos))
for i in range(4):          # varios en el mismo flush
    registrar(db, None, "prueba_cadena", f"flush-{i}", "prueba", valor_nuevo={"i": i})
db.commit()
ids = [x[0] for x in db.execute(text("SELECT id FROM log_auditoria WHERE entidad_id LIKE 'flush-%' ORDER BY id DESC LIMIT 4"))][::-1]
check("Varios en un mismo flush quedan enlazados en orden", C.verificar(db)["integra"] and ids == sorted(ids), ids)


def hilo(k: int) -> None:
    s = SessionLocal()
    try:
        escribir(s, 20, f"hilo{k}")
    finally:
        s.close()


total_antes = C.verificar(db)["total"]
hilos = [threading.Thread(target=hilo, args=(k,)) for k in range(8)]
[h.start() for h in hilos]
[h.join() for h in hilos]
r = C.verificar(db)
check("8 hilos × 20 registros a la vez: cadena íntegra y sin pérdidas",
      r["integra"] and r["total"] == total_antes + 160, (r["integra"], r["total"] - total_antes, r["problemas"]))

print("\n[2] Rollback")
registrar(db, None, "prueba_cadena", "fantasma", "prueba", valor_nuevo={"x": 1})
db.flush()
db.rollback()
escribir(db, 2, "tras-rollback")
r = C.verificar(db)
check("Un rollback no deja huecos ni registros fantasma", r["integra"] and not db.execute(text(
    "SELECT 1 FROM log_auditoria WHERE entidad_id = 'fantasma'")).first(), r)

print("\n[3] Un atacante con acceso a la base")
ids = pruebas_ids(db)
medio, siguiente = ids[5], ids[6]
with atacante(db):
    db.execute(text("UPDATE log_auditoria SET valor_nuevo = '{\"i\": 999}'::jsonb WHERE id = :i"), {"i": medio})
    r = C.verificar(db)
    check("Editar el contenido de un registro se detecta, con su id",
          not r["integra"] and (r["problemas"][0]["id"], r["problemas"][0]["tipo"]) == (medio, "huella"), r["problemas"])
with atacante(db):
    db.execute(text("UPDATE log_auditoria SET resultado = 'exito', operacion = 'borrado_ok' WHERE id = :i"), {"i": medio})
    r = C.verificar(db)
    check("Cambiar la operación o el resultado se detecta", not r["integra"] and r["problemas"][0]["id"] == medio, r["problemas"])
with atacante(db):
    db.execute(text("DELETE FROM log_auditoria WHERE id = :i"), {"i": medio})
    r = C.verificar(db)
    check("Borrar un registro del medio rompe el enlace del siguiente",
          not r["integra"] and {"id": siguiente, "tipo": "enlace"} == {k: r["problemas"][0][k] for k in ("id", "tipo")}, r["problemas"])
with atacante(db):
    # El atacante edita un registro y recalcula TODA la cadena con SHA-256 simple (sin la clave).
    db.execute(text("UPDATE log_auditoria SET valor_nuevo = '{\"i\": 999}'::jsonb WHERE id = :i"), {"i": medio})
    previa = db.execute(text("SELECT huella_anterior FROM log_auditoria WHERE id = :i"), {"i": medio}).scalar()
    for (i,) in db.execute(text("SELECT id FROM log_auditoria WHERE id >= :i AND huella IS NOT NULL ORDER BY id"), {"i": medio}).all():
        nueva = hashlib.sha256((previa + str(i)).encode()).hexdigest()
        db.execute(text("UPDATE log_auditoria SET huella_anterior = :a, huella = :h WHERE id = :i"), {"a": previa, "h": nueva, "i": i})
        previa = nueva
    r = C.verificar(db)
    check("Recalcular la cadena con SHA-256 no sirve: sin la clave de auditoría se detecta",
          not r["integra"] and r["problemas"][0]["id"] == medio, r["problemas"])
with atacante(db):
    db.execute(text("INSERT INTO log_auditoria (id, entidad_tipo, operacion, resultado, timestamp) "
                    "VALUES (nextval('log_auditoria_id_seq'), 'prueba_cadena', 'colado', 'exito', now())"))
    r = C.verificar(db)
    check("Un registro colado sin huella dentro de la cadena se detecta", any(p["tipo"] == "sin_huella" for p in r["problemas"]), r["problemas"])
check("Todo lo anterior se deshizo: la cadena real sigue íntegra", C.verificar(db)["integra"])

print("\n[4] Revisar solo los últimos N")
with atacante(db):
    db.execute(text("UPDATE log_auditoria SET valor_nuevo = '{\"i\": 999}'::jsonb WHERE id = :i"), {"i": ids[-1]})
    check("Con ultimos=5 se ve una edición reciente", not C.verificar(db, ultimos=5)["integra"])
with atacante(db):
    db.execute(text("UPDATE log_auditoria SET valor_nuevo = '{\"i\": 999}'::jsonb WHERE id = :i"), {"i": ids[0]})
    r = C.verificar(db, ultimos=5)
    check("Con ultimos=5 no se mira lo antiguo (para eso está la revisión completa)", r["integra"] and r["total"] == 5, r)

print("\n[5] Limitación declarada")
with atacante(db):
    total = C.verificar(db)["total"]
    db.execute(text("DELETE FROM log_auditoria WHERE id = (SELECT MAX(id) FROM log_auditoria)"))
    r = C.verificar(db)
    check("Borrar el ÚLTIMO registro no rompe un enlace: se ve comparando total y ultima_huella con una copia aparte",
          r["integra"] and r["total"] == total - 1, r)

print("\n[6] La clave importa")
clave_original = os.environ.get("APP_AUDIT_KEY")
os.environ["APP_AUDIT_KEY"] = base64.urlsafe_b64encode(b"otra-clave-distinta-de-32-bytes!!"[:32]).decode()
r = C.verificar(db, ultimos=5)
check("Con otra clave, la cadena no valida (la clave no está en la base)", not r["integra"] and r["problemas"][0]["tipo"] == "huella", r["problemas"])
if clave_original is None:
    del os.environ["APP_AUDIT_KEY"]
else:
    os.environ["APP_AUDIT_KEY"] = clave_original
check("Con la clave original vuelve a validar", C.verificar(db, ultimos=5)["integra"])
check("Sin APP_AUDIT_KEY se deriva de APP_ENCRYPTION_KEY y no es esa misma clave",
      C._clave() != base64.urlsafe_b64decode(os.environ["APP_ENCRYPTION_KEY"]) and len(C._clave()) == 32)

print("\n[7] Endpoint para el admin")
from app.api import admin as A  # noqa: E402
from app.core.deps import ContextoAcceso  # noqa: E402
from app.models import Usuario  # noqa: E402

u = db.query(Usuario).first()
ctx = ContextoAcceso(u, {}, "all", "auditoria", "read", db, None)
r = A.verificar_auditoria(ultimos=None, ctx=ctx, db=db)
check("GET /admin/auditoria/verificar responde íntegra y deja su propio registro",
      r["integra"] and db.execute(text("SELECT 1 FROM log_auditoria WHERE operacion = 'verificar_cadena' LIMIT 1")).first() is not None, r)
filas = A.cadena_auditoria(limite=5, ctx=ctx, db=db)["registros"]
check("GET /admin/auditoria/cadena trae 5 registros con huellas cortas enlazadas",
      len(filas) == 5 and all(len(f["huella"]) == 12 for f in filas) and filas[-1]["anterior"] == filas[-2]["huella"], filas)

db.close()
print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLAS'}")
sys.exit(1 if fallos else 0)
