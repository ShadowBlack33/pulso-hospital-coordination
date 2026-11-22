---
name: preoperatorio
description: Revisar si un paciente está listo para cirugía (clasificación ASA, ayuno, laboratorios críticos) antes de que entre a sala.
---
# Revisión preoperatoria

**Cuándo:** preguntan si el paciente "está listo" para cirugía, si puede entrar a sala o qué falta antes de operar.

**Pasos**
1. Llama a `consultar_paciente`: edad, ASA, cirugía programada y últimos laboratorios (potasio, hemoglobina, glucosa).
2. Llama a `buscar_guias("clasificación ASA riesgo anestésico")` y confirma qué implica el ASA del paciente.
3. Si preguntan por ayuno, llama a `buscar_guias("ayuno preoperatorio sólidos líquidos claros")` y compara con lo que dijo el usuario. Las horas de ayuno las dice el usuario: no están en la historia.
4. Si hay un laboratorio CRÍTICO, carga la skill `valor-critico` antes de concluir.
5. Llama a `buscar_guias("lista de verificación cirugía segura entrada")` si preguntan por la verificación previa a la anestesia.

**Cómo responder**, en este orden: **¿listo o no?**, qué lo frena (con citas), qué falta por verificar. Si un dato no está, dilo; no lo supongas.

**No hagas:** decidir suspender la cirugía. Sugieres y el equipo quirúrgico decide. Si se retrasa, recuerda que corre las siguientes de la sala (la skill `retrasos-quirurgicos` lo explica con la agenda).
