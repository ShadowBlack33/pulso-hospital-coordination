# Mapa normativo · Pulso (Hospital San Rafael)

R22 · Responsable: Juan José García (Ingeniería Biomédica).

Este documento conecta cada norma colombiana que aplica al sistema con lo que exige,
el control técnico que la responde, cómo se comprueba y qué falta. Todo el sistema
trabaja con **datos sintéticos** sobre un hospital hipotético: el mapa muestra que el
diseño cumple o deja el camino listo para cumplir, no que exista una habilitación real.

> **Regla de redacción.** Solo se citan artículos que verificamos en la fuente. Donde
> dice *(verificar numeral)* el contenido de la norma es correcto pero el número exacto
> del artículo se confirma en la fuente oficial antes de citarlo en voz alta.

## 1. Resumen

| Norma | Tema | Qué responde en Pulso |
|---|---|---|
| Ley 1581 de 2012 | Protección de datos personales; los de salud son sensibles | Cifrado, control de acceso por rol, seudonimización hacia la IA, auditoría, consentimiento |
| Decreto 1377 de 2013 (compilado en el Decreto 1074 de 2015) | Reglamenta la Ley 1581: autorización y responsabilidad demostrada | Registro del consentimiento con fecha, trazabilidad completa |
| Ley 2015 de 2020 | Historia clínica electrónica interoperable | HL7 FHIR R4 con HAPI, recursos y terminologías estándar |
| Resolución 866 de 2021 | Elementos de datos clínicos para la interoperabilidad | Paciente, encuentro, observación con LOINC, procedimiento con SNOMED CT, diagnóstico CIE-10 |
| Resolución 1995 de 1999 | Manejo de la historia clínica | Integridad (soft delete, historial), confidencialidad, acceso restringido, PDF de la historia |
| Resolución 839 de 2017 | Retención de la historia clínica | Nada se borra físicamente; plazo de 15 años como política |
| Resolución 3100 de 2019 | Habilitación de servicios de salud | Soporte a los estándares de procesos prioritarios e historia clínica (no habilita por sí mismo) |
| Resolución 256 de 2016 | Sistema de información para la calidad | Indicador de cancelación de cirugía programada en el tablero |
| Resolución 4816 de 2008 | Tecnovigilancia | Bloqueo de quirófanos y camas por mantenimiento con motivo y auditoría |

## 2. Matriz norma → exigencia → control → evidencia

### Ley 1581 de 2012 · Protección de datos personales

| Exigencia | Control en Pulso | Cómo se comprueba | Qué falta |
|---|---|---|---|
| Los datos de salud son **datos sensibles** (art. 5) y su tratamiento tiene reglas especiales (art. 6) | Nombre, documento, teléfono, correo y dirección se guardan cifrados con AES-256-GCM; el documento se busca por índice ciego (HMAC), nunca en claro | `docs/SEGURIDAD.md`; en la base las columnas `*_cifrado` no son legibles | Claves en variables de entorno, no en un gestor de llaves (KMS) |
| Principio de **acceso y circulación restringida** (art. 4) | Control por rol en la API: el médico ve solo sus pacientes, el contable no ve datos clínicos, el paciente solo los suyos | Pruebas de R03 en `scripts/test_e2e.py` (403 al contable en la historia, 403/404 al paciente con datos ajenos) | — |
| Principio de **finalidad** (art. 4): usar los datos solo para lo que se recogieron | Capa de seudonimización hacia la IA: al modelo solo van variables clínicas y un id seudónimo | R12: `GET /ia/reportes/{id}/payload` muestra el JSON exacto; prueba "canario" en `scripts/probar_seudonimizacion.py` | — |
| Principios de **seguridad** y **confidencialidad** (art. 4) | Contraseñas con Argon2id, sesión de 15 min revocable, bloqueo a los 3 intentos, CORS cerrado, cabeceras de seguridad, límite de peticiones | R02 y R03 en el tester; `front/nginx/default.conf` | Doble factor de autenticación |
| **Autorización** previa, expresa e informada del titular (art. 9) | Campo `consentimiento_datos` con su fecha al crear el paciente | Ficha del paciente y auditoría de la creación | Con datos sintéticos el consentimiento es simulado; en producción iría firma o documento soporte |
| **Deberes del responsable**: conservar la información con seguridad y evitar su pérdida o acceso no autorizado (art. 17) | Auditoría append-only protegida por trigger y, desde la semana 14, **encadenada con HMAC** y verificable por el admin (detecta ediciones, borrados intermedios y reordenamientos hechos con acceso directo a la base); soft delete (nada se borra); teléfono y correo con índice ciego | R13 y R14; `scripts/test_auditoria_cadena.py`; `scripts/test_indices_contacto.py`; `docs/SEGURIDAD.md` §3.1 y §7.5 | Borrado de los últimos registros solo se ve con una copia externa de la última huella; copias de respaldo y alta disponibilidad |

### Decreto 1377 de 2013 · Reglamento de la Ley 1581

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Autorización para datos sensibles, que se pueda consultar después *(verificar numeral)* | Consentimiento con fecha guardado en el paciente | Ficha y auditoría | Texto de la autorización versionado |
| **Responsabilidad demostrada**: poder probar las medidas tomadas *(verificar numeral)* | Auditoría de cada acción relevante (quién, qué, cuándo, desde dónde), sin guardar datos sensibles en el log | `GET /auditoria` (solo admin); `docs/SEGURIDAD.md` | Política escrita de tratamiento de datos |

### Ley 2015 de 2020 · Historia clínica electrónica interoperable

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| La historia clínica electrónica debe ser **interoperable** entre prestadores | Servidor HL7 FHIR R4 (HAPI); pacientes, encuentros, observaciones, ubicaciones y remisiones (`ServiceRequest`) en recursos estándar | R15 y R21; `docs/MAPEO_FHIR.md` | Conexión real con el mecanismo nacional de interoperabilidad (IHCE) |
| Intercambio entre sistemas **autenticado y verificable** (semana 14) | Puerta `POST /interop/Observation`: OAuth2 `client_credentials` (token de 10 min con scope), firma HMAC por mensaje, marca de tiempo y nonce anti-repetición, identificador federado en vez de la cédula, errores como `OperationOutcome` | `docs/INTEROP.md`; `scripts/test_interop.py`; `scripts/interop_probar.py` | Firmas asimétricas o mTLS; pruebas cruzadas con el sistema del equipo pareja |
| Seguridad y confidencialidad en el intercambio | `/fhir` cerrado detrás de la API: sin token solo `metadata`; los resultados externos (`POST Observation`) exigen el token de la cuenta de integración; el `Patient` expuesto lleva un MRN interno, sin cédula ni teléfono; `DELETE` nunca | Puerta de nginx (`/_puerta_fhir`); `docs/SEGURIDAD.md` | SMART on FHIR (OAuth2 con scopes FHIR) |

### Resolución 866 de 2021 · Datos clínicos para la interoperabilidad

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Conjunto mínimo de datos clínicos relevantes para intercambiar | Identificación del paciente, encuentro, signos y laboratorios con LOINC, procedimientos con SNOMED CT, diagnóstico CIE-10 | `docs/MAPEO_FHIR.md` | Validar que cada elemento del anexo técnico esté cubierto; códigos SNOMED de algunos procedimientos pendientes de confirmar |

### Resolución 1995 de 1999 · Manejo de la historia clínica

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Características de **integralidad, secuencialidad y disponibilidad** | Historia del paciente en JSON y PDF con observaciones y reportes de IA **aprobados** | R11: `GET /pacientes/{id}/historia/pdf` | — |
| Lo registrado **no se altera ni se borra** | Soft delete, edición con historial, restauración solo por admin | R14; `GET /pacientes/{id}/historial` | — |
| **Confidencialidad** y acceso solo de quien está autorizado | Acceso por rol y por relación con el paciente; el especialista recibe acceso solo por la remisión que se le asigna (por carga, sin opción de rechazarla) | R03 y R08 | Acceso de emergencia ("break-the-glass") auditado |
| Lo que entra a la historia lo **valida un profesional** | El reporte de IA queda `pendiente` hasta que un médico lo aprueba; el rechazado se conserva como evidencia | R10 | Firma digital del profesional |

### Resolución 839 de 2017 · Retención de la historia clínica

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Conservar la historia clínica por **15 años** desde la última atención | Ningún registro clínico se borra físicamente (soft delete) | R14 | Política automática de archivo y depuración al vencer el plazo |

### Resolución 3100 de 2019 · Habilitación

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Estándares de historia clínica y registros, y de procesos prioritarios para habilitar servicios | Pulso apoya estos estándares (trazabilidad, acceso controlado, coordinación de camas y quirófanos), pero un software no habilita un servicio | Toda la matriz anterior | La habilitación es del prestador; no se citan numerales específicos |

### Resolución 256 de 2016 · Calidad en salud

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Reportar indicadores de calidad, entre ellos la cancelación de cirugía programada (P.2.15) | El tablero del coordinador calcula la proporción del último mes cerrado | Tablero "Hospital" | Reporte oficial a la Superintendencia |

### Resolución 4816 de 2008 · Tecnovigilancia

| Exigencia | Control | Evidencia | Qué falta |
|---|---|---|---|
| Gestionar los eventos e incidentes con dispositivos médicos | Un quirófano o cama se marca bloqueado o en mantenimiento con motivo, queda en la auditoría y no se puede reservar | Cambio de estado en camas y quirófanos; auditoría | Registro de eventos adversos con dispositivos y reporte al INVIMA |

### Proyecto final · Requisitos R23 a R33 → norma → control → evidencia

Controles nuevos del proyecto final. Cada uno responde también a una fila de [STRIDE.md](STRIDE.md).

| Req. | Norma que atiende | Control en Pulso | Evidencia (prueba o documento) | Fila STRIDE |
|---|---|---|---|---|
| R23 · Agente RAG con fuentes | Res. 1995/1999 (la historia como fuente de verdad); Res. 3100/2019 (atención basada en guías) | Toda respuesta clínica sale del corpus o de la historia aprobada y trae `fuentes` (documento y fragmento); si no hay soporte se abstiene | `scripts/test_r24_canario.py` §3; `docs/DATASETS.md` § Corpus | D3 |
| R24 · Herramientas con permisos por rol | Ley 1581/2012 art. 4 lit. f (acceso restringido) y art. 17 (seguridad) | Herramientas sin argumento de paciente; permisos del JWT validados en el backend; contable 403; payload exacto al LLM guardado y auditado (`agente_llm`) | `scripts/test_r24_canario.py` §1-2; `scripts/test_agente.py` | I2, I5, R3 |
| R25 · Defensa contra prompt injection | Ley 1581/2012 art. 17 (evitar acceso no autorizado) | Guardia de cuatro capas para lo que el agente lee; la pregunta maliciosa no llega al modelo, queda registrada con usuario e IP y bloquea temporalmente | `scripts/test_guardia.py`; `scripts/test_r24_canario.py` §4 | E2, E3 |
| R26 · Modelo STRIDE | Ley 1581/2012 art. 17 y Decreto 1377/2013 (medidas de seguridad demostrables) | `docs/STRIDE.md` con diagrama, fronteras y una amenaza por categoría como mínimo | `docs/STRIDE.md` | todas |
| R27 · Cifrado de campos | Ley 1581/2012 art. 4 lit. g (seguridad) | Documento, teléfono, correo y dirección con AES-256-GCM; llave en el entorno | `docs/SEGURIDAD.md` §2-3; `scripts/test_indices_contacto.py` | I1 |
| R28 · Auditoría a prueba de manipulación | Res. 1995/1999 art. 15-16 (inalterabilidad e integridad de la historia) | Cadena de huellas HMAC; `GET /admin/auditoria/verificar` solo para el admin | `scripts/test_auditoria_cadena.py`; `docs/SEGURIDAD.md` §7.5 | T4, R1 |
| R29 · Entradas externas autenticadas | Res. 866/2021 (intercambio seguro); Ley 1581/2012 art. 17 | Webhook FHIR con secreto; entrada del socio con OAuth2 o API key y firma HMAC opcional; validación antes de procesar | `scripts/test_interop.py`; `scripts/test_remisiones_externas.py` §2, §3, §8 | S3, S4, T2, T3 |
| R30 · Remisiones FHIR entre instituciones | Res. 866/2021 (interoperabilidad de la historia clínica entre prestadores); Res. 1995/1999 | Bundle con Patient, ServiceRequest (SNOMED), Observation (LOINC/UCUM) y DiagnosticReport aprobado; 422 con `OperationOutcome` sin datos a medias; Task de respuesta | `scripts/test_remisiones_externas.py`; `docs/INTEROP.md` | T3, R2, D4 |
| R31 · Autenticación entre sistemas | Res. 866/2021 | Credenciales de sistema por institución que solo abren las rutas de entrada | `scripts/test_remisiones_externas.py` §2 | S3, E4 |
| R32 · Consentimiento y minimización | Ley 1581/2012 art. 9 (autorización), art. 26 (transferencia), art. 4 lit. b (finalidad) | `Consent` por institución destino; sin él, 409; Bundle sin teléfono, correo ni dirección; solo el reporte aprobado | `scripts/test_remisiones_externas.py` §5 | I4 |
| R33 · Demostración cruzada | Res. 866/2021 | Ida y vuelta entre las URL públicas de los dos equipos, en vivo y auditada | Ensayo real con el equipo socio (Sistema de apoyo al triaje hospitalario) el 2026-11-21, ida y vuelta en los dos sentidos (`docs/INTEROP.md` §A8); en vivo en la sustentación; `scripts/test_remisiones_externas.py` §6 y §9 | R2 |

## 3. Lo que declaramos como limitación

Se dicen en la sustentación como camino de evolución, no como algo que funcione:

- Acceso de emergencia ("break-the-glass") con motivo y tiempo limitado.
- `Provenance` de FHIR para la procedencia de cada dato (el `Consent` por institución ya existe desde el proyecto final, R32).
- Resumen Digital de Atención (RDA) y sus plazos: pendiente de verificar en la norma vigente.
- Firma digital del profesional y del paciente.
- RIPS y factura electrónica: la facturación de Pulso es una referencia interna, no un RIPS ni una factura electrónica. Antes de citar la norma vigente de RIPS/FEV, confirmar si la Res. 948 de 2026 reemplazó a la Res. 2275 de 2023.
- Doble factor de autenticación y gestión de llaves en un KMS.
- Alta disponibilidad y copias de respaldo.

## 4. Preguntas que puede hacer el jurado

- **¿Qué exige cada norma y qué no cubrimos?** Sección 2, columna "Qué falta".
- **¿Por qué la IA no viola la Ley 1581?** Porque al modelo no le llega ningún dato que identifique al paciente (principio de finalidad) y el payload exacto queda guardado para demostrarlo (R12).
- **¿Puede la IA escribir en la historia clínica?** No. Solo lo que un médico aprueba entra a la historia (Res. 1995: lo registrado lo valida un profesional).
- **¿Qué pasa si se borra un paciente?** Nada se borra físicamente; solo el admin restaura y todo queda auditado (Res. 1995 y Res. 839).
