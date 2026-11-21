#!/usr/bin/env python3
"""
Administración de la puerta de interoperabilidad (Semana 14). Se corre en el servidor de Pulso, con el
.env cargado (DATABASE_URL, APP_ENCRYPTION_KEY, JWT_SECRET_KEY, INTEROP_SECRETO_FEDERADO).

    python scripts/interop_cliente.py crear "Laboratorio Clínica Norte"
        → crea el cliente y MUESTRA UNA SOLA VEZ client_id, client_secret y firma_secreto.
          Se los entregas al otro equipo por un canal privado (nunca por el chat del curso ni git).
    python scripts/interop_cliente.py listar
    python scripts/interop_cliente.py desactivar <client_id>
    python scripts/interop_cliente.py reindexar-federado [--simular]
        → calcula sid_federado de los pacientes ya cargados (descifra tipo y documento) y concilia pendientes.
    python scripts/interop_cliente.py conciliar
        → asocia los mensajes pendientes cuyo paciente ya existe.

Remisiones entre instituciones (proyecto final). Un SOCIO es otra institución: cómo la llamamos y con
qué credencial entra a Pulso. Las credenciales QUE EL SOCIO NOS DIO se leen del entorno, no de la línea
de comandos (no quedan en el historial):
    SOCIO_CREDENCIAL_ID=...  SOCIO_CREDENCIAL_SECRETO=...  [SOCIO_FIRMA_SECRETO=...]

    python scripts/interop_cliente.py socio-crear equipo3 "Clínica del Equipo 3" https://xxxx.trycloudflare.com/api \
           [--auth oauth2|api_key|bearer|ninguno] [--ruta-bundle /interop/Bundle] [--ruta-task /interop/Task] \
           [--ruta-token /oauth/token] [--cabecera X-API-Key] [--sin-firma]
        → registra al socio y le crea SU credencial para entrar a Pulso (se muestra una sola vez).
          --sin-firma: el socio no tiene que firmar con HMAC (le basta token o API key).
    python scripts/interop_cliente.py socio-url equipo3 https://nueva-url.trycloudflare.com/api
        → el túnel del socio cambió: se actualiza sin redesplegar.
    python scripts/interop_cliente.py socio-credenciales equipo3      (lee SOCIO_* del entorno)
    python scripts/interop_cliente.py socios
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import text  # noqa: E402

from app.core.crypto import descifrar  # noqa: E402
from app.core.deps import SessionLocal  # noqa: E402
from app.services import interop  # noqa: E402


def reindexar_federado(db, simular: bool) -> dict:
    filas = db.execute(text("SELECT documento_bidx, tipo_documento, documento_cifrado, sid_federado FROM pacientes "
                            "WHERE documento_cifrado IS NOT NULL")).mappings().all()
    r = {"revisados": 0, "actualizados": 0, "sin_cambio": 0, "no_descifran": 0}
    for f in filas:
        r["revisados"] += 1
        try:
            sid = interop.sid_federado(f["tipo_documento"], descifrar(bytes(f["documento_cifrado"]), "pacientes.documento"))
        except Exception:  # noqa: BLE001
            r["no_descifran"] += 1
            continue
        if sid == f["sid_federado"]:
            r["sin_cambio"] += 1
            continue
        r["actualizados"] += 1
        if not simular:
            db.execute(text("UPDATE pacientes SET sid_federado = :s WHERE documento_bidx = :p"), {"s": sid, "p": f["documento_bidx"]})
    if not simular:
        db.commit()
    return r


def _opciones(args: list[str]) -> dict:
    """--clave valor / --bandera → dict."""
    op, i = {}, 0
    while i < len(args):
        if args[i].startswith("--"):
            clave = args[i][2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                op[clave] = args[i + 1]
                i += 2
                continue
            op[clave] = True
        i += 1
    return op


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    orden = argv[1]
    db = SessionLocal()
    try:
        if orden == "crear" and len(argv) >= 3:
            c = interop.crear_cliente(db, argv[2], exige_firma="--sin-firma" not in argv)
            db.commit()
            print("Cliente creado. Guarda estos valores AHORA: no se vuelven a mostrar.\n")
            for k in ("client_id", "client_secret", "firma_secreto", "scopes"):
                print(f"  {k:14s} {c[k]}")
        elif orden == "listar":
            for f in db.execute(text("SELECT client_id, nombre, scopes, activo, ultimo_uso FROM interop_clientes ORDER BY created_at")):
                print(f"{f.client_id}  {'activo  ' if f.activo else 'INACTIVO'}  {f.nombre}  [{f.scopes}]  último uso: {f.ultimo_uso or '—'}")
        elif orden == "desactivar" and len(argv) >= 3:
            n = db.execute(text("UPDATE interop_clientes SET activo = false WHERE client_id = :c"), {"c": argv[2]}).rowcount
            db.commit()
            print("Desactivado." if n else "No existe ese client_id.")
        elif orden == "reindexar-federado":
            simular = "--simular" in argv
            print(reindexar_federado(db, simular), "(simulación: no se escribió nada)" if simular else "")
            if not simular:
                print(interop.conciliar(db))
                db.commit()
        elif orden == "conciliar":
            print(interop.conciliar(db))
            db.commit()
        elif orden == "socio-crear" and len(argv) >= 5:
            from app.services import remisiones_externas as RX    # noqa: PLC0415
            op = _opciones(argv[5:])
            cliente = interop.crear_cliente(db, f"Socio {argv[3]}", exige_firma="sin-firma" not in op)
            RX.crear_socio(db, argv[2], argv[3], argv[4], modo_auth=op.get("auth", "oauth2"),
                           credencial_id=os.environ.get("SOCIO_CREDENCIAL_ID"),
                           credencial_secreto=os.environ.get("SOCIO_CREDENCIAL_SECRETO"),
                           firma_secreto=os.environ.get("SOCIO_FIRMA_SECRETO"), client_id=cliente["client_id"],
                           ruta_bundle=op.get("ruta-bundle"), ruta_task=op.get("ruta-task"),
                           ruta_token=op.get("ruta-token"), api_key_cabecera=op.get("cabecera"))
            db.commit()
            print(f"Socio {argv[2]} registrado. Entréguele por un canal privado SU credencial para entrar a Pulso:\n")
            for k in ("client_id", "client_secret", "api_key", "scopes"):
                print(f"  {k:14s} {cliente[k]}")
            if cliente["exige_firma"]:
                print(f"  {'firma_secreto':14s} {cliente['firma_secreto']}   (firma HMAC obligatoria)")
            else:
                print("  firma HMAC: no exigida (basta token o API key)")
        elif orden == "socio-url" and len(argv) >= 4:
            from app.services import remisiones_externas as RX    # noqa: PLC0415
            print("Actualizado." if RX.actualizar_socio(db, argv[2], base_url=argv[3]) else "No existe ese socio.")
            db.commit()
        elif orden == "socio-credenciales" and len(argv) >= 3:
            from app.services import remisiones_externas as RX    # noqa: PLC0415
            ok = RX.actualizar_socio(db, argv[2], credencial_id=os.environ.get("SOCIO_CREDENCIAL_ID"),
                                     credencial_secreto=os.environ.get("SOCIO_CREDENCIAL_SECRETO"),
                                     firma_secreto=os.environ.get("SOCIO_FIRMA_SECRETO"),
                                     modo_auth=_opciones(argv[3:]).get("auth"))
            db.commit()
            print("Actualizado." if ok else "No existe ese socio o no había nada que cambiar.")
        elif orden == "socios":
            from app.services import remisiones_externas as RX    # noqa: PLC0415
            for f in db.execute(text("SELECT * FROM interop_socios ORDER BY codigo")).mappings():
                v = RX.vista_socio(dict(f))
                print(f"{v['codigo']:12s} {'activo  ' if v['activo'] else 'INACTIVO'} {v['base_url']}  auth={v['modo_auth']} "
                      f"secreto={'sí' if v['tiene_secreto'] else 'no'} firma={'sí' if v['firma'] else 'no'} "
                      f"entra_con={v['client_id']}")
        else:
            print(__doc__)
            return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
