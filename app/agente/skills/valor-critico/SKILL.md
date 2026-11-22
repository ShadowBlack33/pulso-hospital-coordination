---
name: valor-critico
description: Actuar ante un laboratorio crítico (potasio, glucosa, hemoglobina): qué guía aplica y cómo escalar la alerta.
---
# Valor crítico

**Cuándo:** `consultar_paciente` muestra un resultado marcado CRÍTICO, o el usuario pregunta por una alerta crítica.

**Pasos**
1. Llama a `consultar_paciente` y anota qué analito está crítico y cuándo se tomó (puede ser de hace horas).
2. Elige la guía por analito: potasio alto → `buscar_guias("hiperpotasemia grave tratamiento ECG")`; glucosa baja → `buscar_guias("hipoglucemia tratamiento niveles")`; hemoglobina baja → `buscar_guias("umbrales de transfusión")`.
3. Llama a `buscar_guias("alerta crítica reconocimiento escalamiento")` para explicar cómo se reconoce y se escala la alerta en Pulso.
4. Si el usuario es especialista o el cuadro lo amerita, ofrece `remision-especialista`.

**Cómo responder:** el valor y su hora, lo que dice la guía (con citas), y el siguiente paso de escalamiento. Si el valor es viejo, dilo: la decisión se toma con un dato reciente.

**No hagas:** repetir dosis o conductas de memoria, descartar el valor por "probable error" ni reconocer la alerta por el médico: la reconoce él en pantalla.
