#!/usr/bin/env python3
"""
Probador de la puerta de interoperabilidad (Semana 14). Hace de «el otro sistema»: pide el token, firma
y manda una Observation, y muestra la respuesta. Sirve para que el equipo pareja verifique su cliente
contra el nuestro (y viceversa) y para dejar la salida como evidencia en docs/INTEROP.md.

Las credenciales se leen del ENTORNO, no de la línea de comandos (así no quedan en el historial del shell):
    export PULSO_URL=http://localhost:8000          # sin /api si vas directo a la API; con /api detrás de nginx
    export PULSO_CLIENT_ID=cli_...  PULSO_CLIENT_SECRET=...  PULSO_FIRMA_SECRETO=...

    python scripts/interop_probar.py --tipo CC --documento 1144123456 --loinc 2339-0 --valor 95 --unidad mg/dL
    python scripts/interop_probar.py ... --sid <32 hex>        # si ya tienes el identificador federado
    python scripts/interop_probar.py ... --repetir             # mismo id con otro nonce (espera 201/202 y luego 409)
    python scripts/interop_probar.py ... --replay              # copia exacta del mensaje, mismo nonce (espera 401)
    python scripts/interop_probar.py ... --fuera-de-hora       # firma con 1 hora de atraso (espera 401)
    python scripts/interop_probar.py ... --manipular           # cambia el valor después de firmar (espera 401)
Para calcular el identificador federado desde la cédula hace falta INTEROP_SECRETO_FEDERADO en el entorno.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app.services.interop import (CABECERAS, RUTA_OBSERVATION, SYS_FEDERADO, firmar,  # noqa: E402
                                  sid_federado)

NOMBRES = {"2339-0": "Glucose [Mass/volume] in Blood", "2823-3": "Potassium [Moles/volume] in Serum or Plasma",
           "718-7": "Hemoglobin [Mass/volume] in Blood", "59408-5": "Oxygen saturation in Arterial blood by Pulse oximetry"}


def observation(sid: str, loinc: str, valor: float, unidad: str, id_obs: str) -> dict:
    return {"resourceType": "Observation", "id": id_obs, "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": NOMBRES.get(loinc, loinc)}]},
            "subject": {"identifier": {"system": SYS_FEDERADO, "value": sid}},
            "effectiveDateTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "valueQuantity": {"value": valor, "unit": unidad, "system": "http://unitsofmeasure.org", "code": unidad}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tipo", default="CC")
    ap.add_argument("--documento")
    ap.add_argument("--sid")
    ap.add_argument("--loinc", default="2339-0")
    ap.add_argument("--valor", type=float, default=95.0)
    ap.add_argument("--unidad", default="mg/dL")
    ap.add_argument("--id", dest="id_obs", default=None)
    ap.add_argument("--repetir", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--fuera-de-hora", action="store_true")
    ap.add_argument("--manipular", action="store_true")
    a = ap.parse_args()

    base = os.environ.get("PULSO_URL", "http://localhost:8000").rstrip("/")
    try:
        cid, csec, firma = (os.environ[k] for k in ("PULSO_CLIENT_ID", "PULSO_CLIENT_SECRET", "PULSO_FIRMA_SECRETO"))
    except KeyError as e:
        sys.exit(f"Falta {e.args[0]} en el entorno (ver el uso arriba).")
    sid = a.sid or (sid_federado(a.tipo, a.documento) if a.documento else None)
    if not sid:
        sys.exit("Da --sid, o --documento (con INTEROP_SECRETO_FEDERADO en el entorno).")

    with httpx.Client(timeout=20) as http:
        print("1) POST /oauth/token")
        t = http.post(f"{base}/oauth/token", data={"grant_type": "client_credentials", "client_id": cid,
                                                   "client_secret": csec, "scope": "system/Observation.write"})
        print(f"   → {t.status_code}", {k: (v if k != "access_token" else v[:20] + "…") for k, v in t.json().items()})
        if t.status_code != 200:
            return 1
        token = t.json()["access_token"]

        id_obs = a.id_obs or f"prueba-{uuid.uuid4().hex[:12]}"
        cuerpo = json.dumps(observation(sid, a.loinc, a.valor, a.unidad, id_obs), separators=(",", ":")).encode()
        print(f"2) POST /interop/Observation  (id {id_obs}, sid {sid[:8]}…)")
        nonce = secrets.token_urlsafe(18)
        ts = str(int(time.time()) - (3600 if a.fuera_de_hora else 0))      # una sola vez: un replay es idéntico
        for intento in range(2 if (a.repetir or a.replay) else 1):
            if intento and not a.replay:
                nonce = secrets.token_urlsafe(18)           # --repetir: mismo mensaje, nonce nuevo
            enviar = cuerpo
            sig = firmar(firma, ts, nonce, "POST", RUTA_OBSERVATION, cuerpo)
            if a.manipular:
                enviar = cuerpo.replace(str(a.valor).encode(), str(a.valor * 10).encode(), 1)
            r = http.post(f"{base}{RUTA_OBSERVATION}", content=enviar,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/fhir+json",
                                   CABECERAS["timestamp"]: ts, CABECERAS["nonce"]: nonce, CABECERAS["firma"]: sig})
            print(f"   intento {intento + 1}: {r.status_code}", json.dumps(r.json(), ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
