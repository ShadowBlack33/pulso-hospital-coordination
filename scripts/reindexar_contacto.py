#!/usr/bin/env python3
"""
Calcula telefono_bidx y email_bidx para las personas que ya estaban cargadas antes de
db/05_seguridad_e_interop.sql: descifra el teléfono y el correo (AES-GCM), calcula su blind
index y lo guarda. Solo toca las filas donde el índice falta o está desactualizado; se puede
repetir sin problema. No cambia ningún dato cifrado ni la fecha de modificación.

Uso:
    python scripts/reindexar_contacto.py --simular     # cuenta, no escribe
    python scripts/reindexar_contacto.py               # escribe

Necesita en el .env: DATABASE_URL, APP_ENCRYPTION_KEY y APP_BLIND_INDEX_KEY (las mismas con las
que se cifraron los datos).
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import text  # noqa: E402

from app.core.crypto import blind_index_contacto, descifrar  # noqa: E402
from app.core.deps import SessionLocal  # noqa: E402

TABLAS = {"pacientes": "documento_bidx", "personal": "id"}      # tabla → llave primaria
LOTE = 500


def reindexar(simular: bool) -> dict[str, dict[str, int]]:
    db = SessionLocal()
    resumen: dict[str, dict[str, int]] = {}
    try:
        for tabla, llave in TABLAS.items():
            r = resumen[tabla] = {"revisadas": 0, "actualizadas": 0, "sin_cambio": 0, "no_descifran": 0}
            filas = db.execute(text(
                f"SELECT {llave} AS k, telefono_cifrado, email_cifrado, telefono_bidx, email_bidx FROM {tabla} "
                "WHERE telefono_cifrado IS NOT NULL OR email_cifrado IS NOT NULL")).mappings().all()
            pendientes = []
            for f in filas:
                r["revisadas"] += 1
                try:
                    tel = descifrar(bytes(f["telefono_cifrado"]), f"{tabla}.telefono") if f["telefono_cifrado"] else None
                    mail = descifrar(bytes(f["email_cifrado"]), f"{tabla}.email") if f["email_cifrado"] else None
                except Exception:  # noqa: BLE001 -- dato cifrado con otra clave o corrupto: se cuenta y se sigue
                    r["no_descifran"] += 1
                    continue
                nuevo = (blind_index_contacto(tel, f"{tabla}.telefono"), blind_index_contacto(mail, f"{tabla}.email"))
                if nuevo == (f["telefono_bidx"], f["email_bidx"]):
                    r["sin_cambio"] += 1
                else:
                    pendientes.append({"k": f["k"], "t": nuevo[0], "e": nuevo[1]})
            r["actualizadas"] = len(pendientes)
            if not simular:
                for i in range(0, len(pendientes), LOTE):
                    db.execute(text(f"UPDATE {tabla} SET telefono_bidx = :t, email_bidx = :e WHERE {llave} = :k"),
                               pendientes[i:i + LOTE])
                db.commit()
        return resumen
    finally:
        db.close()


if __name__ == "__main__":
    simular = "--simular" in sys.argv
    for tabla, r in reindexar(simular).items():
        print(f"{tabla:10s} revisadas {r['revisadas']:6d} · {'a actualizar' if simular else 'actualizadas'} "
              f"{r['actualizadas']:6d} · sin cambio {r['sin_cambio']:6d} · no descifran {r['no_descifran']}")
    print("(simulación: no se escribió nada)" if simular else "Listo.")
