#!/usr/bin/env python3
"""
Pruebas del catálogo de skills del agente (Semana 14). No necesitan base de datos ni modelo.

  1. Formato: cada carpeta tiene SKILL.md con nombre igual a la carpeta y una descripción corta.
  2. Referencias: las herramientas y las skills que nombra cada skill existen.
  3. Reglas: ninguna skill trae cifras clínicas propias (umbrales, dosis): eso sale de las guías
     con cita (regla 1 del prompt).
  4. Cobertura: cada consulta `buscar_guias("...")` que sugiere una skill recupera la guía esperada
     entre las 3 primeras con BM25 (sin red ni embeddings), y supera el umbral abierto del RAG
     (4,0): la herramienta no respondería SIN_EVIDENCIA.

Uso:  python scripts/test_skills.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app.agente import skills as S  # noqa: E402
from app.services import rag_core as core  # noqa: E402

fallos = 0


def check(nombre: str, ok: bool, detalle=None) -> None:
    global fallos
    print(f"  {'OK  ' if ok else 'FALLA'}  {nombre}")
    if not ok:
        fallos += 1
        if detalle is not None:
            print("        ", str(detalle)[:300])


carpetas = sorted(p.parent for p in (RAIZ / "app/agente/skills").glob("*/SKILL.md"))
cat = S.catalogo()

print("\n[1] Formato")
check(f"Hay {len(carpetas)} skills y el catálogo las ve todas", len(cat) == len(carpetas) >= 10, (len(cat), len(carpetas)))
check("El nombre coincide con la carpeta", all(c.name in cat for c in carpetas), [c.name for c in carpetas if c.name not in cat])
meta = {n: S._leer(a) for n, a in cat.items()}
check("Cada descripción cabe en una línea (150 caracteres o menos)",
      all(0 < len(m["description"]) <= 150 for m, _ in meta.values()), {n: len(m["description"]) for n, (m, _) in meta.items()})
check("Cada skill dice cuándo usarla, tiene pasos y qué no hacer",
      all("**Cuándo:**" in c and "**Pasos**" in c and "**No hagas:**" in c for _, c in meta.values()),
      [n for n, (_, c) in meta.items() if not ("**Cuándo:**" in c and "**Pasos**" in c and "**No hagas:**" in c)])
check("El índice del prompt tiene una línea por skill", len(S.indice().splitlines()) == len(cat))

print("\n[2] Referencias")
fuente = (RAIZ / "app/agente/herramientas.py").read_text(encoding="utf-8")
herramientas = set(re.findall(r"@tool\n(?:@.*\n)*def (\w+)\(", fuente))
malas_h, malas_s = [], []
for n, (_, cuerpo) in meta.items():
    for h in re.findall(r"`(\w+)(?:\(|`)", cuerpo):
        if h in {"buscar_guias", "consultar_paciente", "resumen_de_mi_trabajo", "ocupacion_hospital", "agenda_quirurgica",
                 "listar_especialidades", "proponer_remision", "cargar_skill", "guardar_preferencia"} and h not in herramientas:
            malas_h.append((n, h))
    for s in re.findall(r"skill `([a-z-]+)`", cuerpo):
        if s not in cat:
            malas_s.append((n, s))
check("Las herramientas que nombran existen", not malas_h and len(herramientas) >= 9, (malas_h, herramientas))
check("Las skills que se llaman entre sí existen", not malas_s, malas_s)

print("\n[3] Sin cifras clínicas propias")
CIFRA = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:mEq|mg|mmol|g/dL|mmHg|ml|mL|%|horas?|h\b|min)", re.I)
check("Ninguna skill trae umbrales ni dosis", not any(CIFRA.search(c) for _, c in meta.values()),
      [(n, CIFRA.search(c).group()) for n, (_, c) in meta.items() if CIFRA.search(c)])

print("\n[4] Las consultas sugeridas encuentran su guía")
ESPERADA = [("NEWS2", {"G10"}), ("admisión a UCI", {"G05"}), ("sepsis", {"G11"}), ("clasificación ASA", {"G04"}),
            ("ayuno", {"G12"}), ("hiperpotasemia", {"G02"}), ("hipoglucemia", {"G01"}), ("transfusión", {"G03"}),
            ("alerta crítica", {"G07"}), ("triage urgencias", {"G09"}), ("oportunidad triage", {"G15", "G09"}),
            ("cirugía segura", {"G06"}), ("cancelación de cirugía", {"G15"}), ("reserva acceso", {"G13"}),
            ("copia de la historia", {"G13"}), ("correcciones de la historia", {"G13"}), ("derechos del paciente", {"G14"}),
            ("protección de datos", {"G18"})]
guias = [core.leer_documento(p) for p in sorted((RAIZ / "docs/rag/guias").glob("*.md"))]
frags = [f for d in guias for f in core.fragmentar(d)]
bm = core.BM25([f.texto_busqueda for f in frags])
consultas = sorted({(n, q) for n, (_, c) in meta.items() for q in re.findall(r'buscar_guias\("([^"]+)"\)', c)})
for skill, q in consultas:
    esperadas = next((ids for clave, ids in ESPERADA if clave.lower() in q.lower()), None)
    top = bm.ranking(q, 3)
    docs = [frags[i].documento_id for i, _ in top]
    ok = esperadas is not None and bool(esperadas & set(docs)) and top[0][1] >= 4.0
    check(f"{skill}: «{q}» → {docs[0]} ({top[0][1]:.1f})", ok, (esperadas, docs, [round(s, 1) for _, s in top]))

print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLAS'}")
sys.exit(1 if fallos else 0)
