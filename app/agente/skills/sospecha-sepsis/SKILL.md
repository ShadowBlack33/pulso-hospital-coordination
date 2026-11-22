---
name: sospecha-sepsis
description: Revisar si un paciente cumple criterios de tamizaje de sepsis y qué pide la guía como primeras acciones.
---
# Sospecha de sepsis

**Cuándo:** hay fiebre, hipotensión, taquicardia, lactato alto, infección sospechada o preguntan por sepsis o choque séptico.

**Pasos**
1. Llama a `consultar_paciente` para signos vitales, laboratorios y reportes aprobados.
2. Llama a `buscar_guias("sepsis tamizaje criterios")` y revisa qué parámetros pide; di cuáles están en los datos y cuáles faltan.
3. Si el tamizaje es positivo, llama a `buscar_guias("sepsis antibióticos cuándo iniciar")` y a `buscar_guias("sepsis líquidos vasopresores")`.
4. Llama a `buscar_guias("sepsis ingreso a UCI")` y, si corresponde, a `ocupacion_hospital` para ver camas de UCI libres.

**Cómo responder:** ¿cumple el tamizaje?, qué lo respalda (con citas), qué falta medir y qué acciones y tiempos menciona la guía. Todo en forma de sugerencia al clínico.

**No hagas:** indicar antibiótico, dosis ni volumen de líquidos por tu cuenta: solo lo que diga la guía, citado.
