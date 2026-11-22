---
name: admision-uci
description: Valorar si un paciente necesita cama de UCI (NEWS2, sepsis, criterios de admisión) y si hay cama disponible.
---
# ¿Necesita UCI?

**Cuándo:** preguntan si "este paciente" debe ir a UCI, si "califica" o si hay cama de cuidados intensivos.

**Pasos**
1. Llama a `consultar_paciente`: signos vitales, laboratorios y el reporte de IA aprobado (riesgo de UCI), si lo hay.
2. Llama a `buscar_guias("NEWS2 puntaje respuesta clínica")`. Calcula el NEWS2 solo con los parámetros que estén en los datos y di cuáles faltan; no completes los que no están.
3. Si hay sospecha de infección, carga la skill `sospecha-sepsis` y sigue sus pasos.
4. Llama a `buscar_guias("criterios de admisión a UCI priorización")`.
5. Llama a `ocupacion_hospital` para saber si hay camas de UCI libres.

**Cómo responder:** el criterio que cumple (con citas), el puntaje o los datos que faltan, y la disponibilidad de UCI. Si el reporte de IA y las guías no coinciden, di ambas cosas: el modelo de IA propone, la guía manda.

**No hagas:** asignar cama ni ordenar el traslado; eso lo decide el médico.
