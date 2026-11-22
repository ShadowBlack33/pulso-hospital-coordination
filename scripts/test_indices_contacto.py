#!/usr/bin/env python3
"""
Pruebas del índice ciego de teléfono y correo (Semana 14). Necesita una base con
db/05_seguridad_e_interop.sql aplicado. Crea UN paciente de prueba (y lo borra al terminar);
se niega a correr contra una base que no sea local salvo con --forzar.

  1. Normalización: '+57 300 123 4567' = '300-123-4567'; el correo no distingue mayúsculas, pero
     'a.b@x.co' y 'ab@x.co' son cuentas distintas. Cada tabla y campo tiene su propio índice.
  2. Crear y editar mantienen el índice al día (pacientes y «mis datos»).
  3. El autocompletar encuentra por correo y por teléfono sin descifrar, y respeta el documento.
  4. Nada del índice ciego sale en la auditoría ni en el historial de cambios.
  5. scripts/reindexar_contacto.py rellena lo que falta y no escribe con --simular.

Uso:  python scripts/test_indices_contacto.py
"""
from __future__ import annotations

import base64
import os
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
for var in ("JWT_SECRET_KEY", "APP_ENCRYPTION_KEY", "APP_BLIND_INDEX_KEY", "IA_SEUDONIMO_KEY"):
    os.environ.setdefault(var, base64.urlsafe_b64encode(b"k" * 32).decode())

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.crypto import blind_index, blind_index_contacto as bic  # noqa: E402

fallos = 0


def check(nombre: str, ok: bool, detalle=None) -> None:
    global fallos
    print(f"  {'OK  ' if ok else 'FALLA'}  {nombre}")
    if not ok:
        fallos += 1
        if detalle is not None:
            print("        ", str(detalle)[:400])


print("\n[1] Normalización")
check("Teléfono: con o sin +57, espacios, guiones o paréntesis es el mismo",
      len({bic(v, "pacientes.telefono") for v in ("3001234567", "+57 300 123 4567", "300-123-4567", "(300) 123 4567",
                                                   "0057 3001234567", "57 3001234567")}) == 1)
check("Teléfono: otro número, otro índice", bic("3001234567", "pacientes.telefono") != bic("3001234568", "pacientes.telefono"))
check("Correo: mayúsculas y espacios en los extremos no cuentan",
      bic("Ana.Ruiz@HSanRafael.co ", "pacientes.email") == bic("ana.ruiz@hsanrafael.co", "pacientes.email"))
check("Correo: 'a.b@x.co' y 'ab@x.co' son cuentas distintas", bic("a.b@x.co", "pacientes.email") != bic("ab@x.co", "pacientes.email"))
check("Cada tabla y campo tiene su propio índice (mismo valor, distinto resultado)",
      len({bic("3001234567", "pacientes.telefono"), bic("3001234567", "personal.telefono"),
           blind_index("3001234567", "pacientes.documento")}) == 3)
check("Sin dato no hay índice", bic(None, "pacientes.email") is None and bic("  ", "pacientes.telefono") is None
      and bic("abc", "pacientes.telefono") is None)
check("El índice es un HMAC hexadecimal de 64 caracteres", len(bic("3001234567", "pacientes.telefono")) == 64)

host = (get_settings().database_url.split("@")[-1].split("/")[0].split(":")[0] or "").lower()
if host not in {"localhost", "127.0.0.1", "db", "postgres", ""} and "--forzar" not in sys.argv:
    print(f"\nLa base es «{host}», no local: se omite la parte con base de datos (usa --forzar para correrla).")
    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLAS'}")
    sys.exit(1 if fallos else 0)

from app.api import pacientes as P  # noqa: E402
from app.api import perfiles as F  # noqa: E402
from app.core.deps import ContextoAcceso, SessionLocal  # noqa: E402
from app.models import Paciente, Usuario  # noqa: E402
from app.services.auditoria import snapshot  # noqa: E402
from scripts.reindexar_contacto import reindexar  # noqa: E402

P._publicar_en_fhir = lambda db, p: None        # sin red: HAPI no participa en esta prueba

db = SessionLocal()
db.execute(text("SELECT telefono_bidx FROM pacientes LIMIT 1"))      # falla si falta la migración
usuario = db.query(Usuario).first()
ctx = ContextoAcceso(usuario, {}, "all", "paciente", "create", db, None)
DOC, TEL, MAIL = "1999000111", "+57 311 555 0199", "Prueba.Contacto@Ejemplo.co"
paciente_id = blind_index(DOC, "pacientes.documento")
db.execute(text("DELETE FROM pacientes WHERE documento_bidx = :p"), {"p": paciente_id})
db.commit()


def limpiar() -> None:
    db.rollback()
    db.execute(text("DELETE FROM pacientes WHERE documento_bidx = :p"), {"p": paciente_id})
    db.commit()


try:
    print("\n[2] Crear y editar mantienen el índice al día")
    resp = P.crear(P.PacienteCrear(tipo_documento="CC", documento=DOC, nombre="Prueba", apellido="Contacto",
                                   telefono=TEL, email=MAIL, fecha_nacimiento=date(1990, 1, 1), sexo="F"),
                   None, ctx, db)
    p = db.get(Paciente, paciente_id)
    check("Al crear se guardan telefono_bidx y email_bidx",
          p.telefono_bidx == bic(TEL, "pacientes.telefono") and p.email_bidx == bic(MAIL, "pacientes.email"),
          (p.telefono_bidx, p.email_bidx))
    check("Y el dato sigue cifrado (no está en claro en la fila)", TEL.encode() not in bytes(p.telefono_cifrado)
          and MAIL.encode() not in bytes(p.email_cifrado))

    print("\n[3] El autocompletar busca sin descifrar")
    ctx_leer = ContextoAcceso(usuario, {}, "all", "paciente", "read", db, None)

    def buscar(q: str) -> list[str]:
        return [x["paciente_id"] for x in P.sugerir(q=q, ctx=ctx_leer, db=db)]
    check("Por correo (mayúsculas distintas)", paciente_id in buscar("prueba.contacto@ejemplo.CO"))
    check("Por teléfono sin indicativo y con guiones", paciente_id in buscar("311-555-0199"))
    check("Por teléfono con +57 y espacios", paciente_id in buscar("+57 311 555 0199"))
    check("Por documento sigue funcionando", buscar(DOC) == [paciente_id])
    check("Un correo o teléfono que no existe no devuelve a nadie",
          buscar("nadie.existe@ejemplo.co") == [] and buscar("3119999999") == [])
    check("Un teléfono parcial no adivina (la búsqueda es exacta)", paciente_id not in buscar("311555"))

    print("\n[2b] Editar")
    P.actualizar(paciente_id, P.PacienteActualizar(telefono="3207770000", email="otro@ejemplo.co"), None, ctx, db)
    check("Tras editar, el teléfono nuevo se encuentra y el viejo ya no",
          paciente_id in buscar("320 777 0000") and paciente_id not in buscar("311-555-0199"))
    check("Tras editar, el correo nuevo se encuentra y el viejo ya no",
          paciente_id in buscar("OTRO@ejemplo.co") and paciente_id not in buscar("prueba.contacto@ejemplo.co"))
    ctx_self = ContextoAcceso(usuario, {"paciente_id": paciente_id}, "self", "paciente", "read", db, None)
    F.actualizar_mis_datos(F.MisDatosActualizar(telefono="3141112222"), None, ctx_self, db)
    check("«Mis datos» (el propio paciente) también mantiene el índice", paciente_id in buscar("314 111 2222"))

    print("\n[4] Nada del índice sale en la auditoría")
    p = db.get(Paciente, paciente_id)
    s = snapshot(p)
    check("El snapshot de auditoría los protege", s["telefono_bidx"] == "<protegido>" and s["email_bidx"] == "<protegido>", s)
    huellas = {p.telefono_bidx, p.email_bidx, bic("3207770000", "pacientes.telefono")}
    filas = db.execute(text("SELECT valor_nuevo::text AS v, valor_anterior::text AS a FROM log_auditoria "
                            "ORDER BY id DESC LIMIT 40")).all()
    check("Ningún registro reciente de la auditoría contiene un índice ciego",
          not any(h and (h in (f.v or "") or h in (f.a or "")) for f in filas for h in huellas))
    h = db.execute(text("SELECT campo, valor_nuevo FROM historial_cambios WHERE campo LIKE '%_bidx' ORDER BY id DESC LIMIT 5")).all()
    check("El historial de cambios guarda '<cifrado>', no el índice", all(x.valor_nuevo == "<cifrado>" for x in h) and h, h)

    print("\n[5] Reindexar lo que ya estaba cargado")
    db.execute(text("UPDATE pacientes SET telefono_bidx = NULL, email_bidx = NULL WHERE documento_bidx = :p"), {"p": paciente_id})
    db.commit()
    sim = reindexar(simular=True)
    check("--simular cuenta pero no escribe", sim["pacientes"]["actualizadas"] >= 1 and paciente_id not in buscar("314 111 2222"), sim)
    real = reindexar(simular=False)
    check("Sin --simular rellena el índice y la búsqueda vuelve a funcionar",
          real["pacientes"]["actualizadas"] >= 1 and paciente_id in buscar("314 111 2222"), real)
    check("Repetirlo no cambia nada", reindexar(simular=True)["pacientes"]["actualizadas"] == 0)
finally:
    limpiar()
    db.close()

print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLAS'}")
sys.exit(1 if fallos else 0)
