---
name: entrega-de-turno
description: Armar el resumen del turno de quien pregunta: pendientes, alertas, reportes por revisar, remisiones y quirófano.
---
# Entrega de turno

**Cuándo:** "¿cómo va mi turno?", "¿qué tengo pendiente?", "prepárame la entrega".

**Pasos**
1. Llama a `resumen_de_mi_trabajo` (pacientes a cargo, críticos, alertas sin reconocer, reportes por revisar, remisiones abiertas).
2. Si el perfil ve quirófanos, llama a `agenda_quirurgica` para cirugías en curso, retrasadas o canceladas.
3. Si el perfil ve camas (coordinador, enfermería jefe), llama a `ocupacion_hospital`.
4. Si hay un paciente en la ficha abierta, añade solo lo que `consultar_paciente` confirme.

**Cómo responder:** primero lo que el sistema marca como crítico o sin reconocer, después lo que está por revisar, y al final lo informativo. Indica que son datos del momento de la consulta. Si una herramienta responde NO_AUTORIZADO, di qué parte no está disponible para su perfil.

**No hagas:** inventar nombres de pacientes (el resumen solo trae conteos) ni decir que algo está resuelto si el sistema no lo muestra.
