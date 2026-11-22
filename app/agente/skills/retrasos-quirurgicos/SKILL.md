---
name: retrasos-quirurgicos
description: Explicar retrasos y choques en la agenda quirúrgica de hoy, su efecto cascada y qué verificar antes de reprogramar.
---
# Retrasos en quirófano

**Cuándo:** preguntan por retrasos, cirugías canceladas, por qué una sala va atrasada o qué pasa si se demora una cirugía.

**Pasos**
1. Llama a `agenda_quirurgica`: cirugías en curso, programadas, retrasadas y choques de sala.
2. Identifica la cirugía que origina el retraso y cuáles de la misma sala corre (efecto cascada).
3. Si el motivo es del paciente, llama a `consultar_paciente` (laboratorios críticos, ASA) y a `buscar_guias("ayuno preoperatorio")` o `buscar_guias("clasificación ASA")` según el caso.
4. Si preguntan por la lista de verificación o por cancelaciones, llama a `buscar_guias("cirugía segura lista de verificación")` y `buscar_guias("cancelación de cirugía programada indicador")`.

**Cómo responder:** qué sala, cuántas cirugías afecta y desde qué hora, con la causa si el sistema la muestra. Distingue lo que ves en la agenda de lo que supones.

**No hagas:** cancelar o mover cirugías, ni culpar a un equipo: propones, el coordinador y el equipo quirúrgico deciden.
