---
name: triage-urgencias
description: Orientar el triage de un paciente en urgencias y vigilar la oportunidad de atención (niveles, NEWS2, tiempos).
---
# Triage en urgencias

**Cuándo:** preguntan qué nivel de triage corresponde, si "se puede esperar" o cuánto lleva esperando un paciente.

**Pasos**
1. Llama a `consultar_paciente`: signos vitales, laboratorios y motivo del encuentro, si están.
2. Llama a `buscar_guias("triage urgencias niveles criterios")` y compara los hallazgos con el nivel que describe la guía.
3. Llama a `buscar_guias("NEWS2 puntaje respuesta clínica")` si hay signos vitales completos; di qué parámetros faltan.
4. Si preguntan por tiempos de espera, llama a `buscar_guias("oportunidad triage II indicador")` y compara con lo que dijo el usuario.
5. Si el cuadro apunta a sepsis o a UCI, carga `sospecha-sepsis` o `admision-uci`.

**Cómo responder:** nivel que sugiere la guía (con citas), qué hallazgo lo justifica y qué dato falta para estar seguros. Aclara que el nivel lo asigna quien hace el triage.

**No hagas:** asignar el nivel como definitivo ni bajar la prioridad de un paciente por falta de datos: si faltan, sugiere medirlos.
