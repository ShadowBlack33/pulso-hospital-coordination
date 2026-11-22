# Interoperabilidad entre instituciones

Pulso · Hospital San Rafael · Proyecto final (R29 a R33) y semana 14

Este documento es el **contrato** para que cualquier equipo se conecte con Pulso. Tiene dos partes:

- **Parte A · Remisiones entre instituciones (proyecto final).** El médico de una institución remite un
  paciente al especialista de la otra; todo viaja en FHIR R4 y la respuesta vuelve sola.
- **Parte B · Resultados sueltos (semana 14).** Un laboratorio envía una `Observation` para un paciente
  identificado con el id federado. Se conserva tal como estaba (secciones 1 a 13).

Lo que se pide del otro lado es lo mínimo que cualquier implementación FHIR puede dar: un Bundle con
recursos estándar, códigos LOINC/SNOMED/UCUM y una credencial de sistema. La firma HMAC es **opcional por
institución** (se exige solo si se acuerda), y la forma en que Pulso se autentica con cada socio (OAuth2,
API key, bearer fijo o nada) y las rutas del socio se configuran sin tocar código.

---

# Parte A · Remisiones entre instituciones

## A1. El flujo

```mermaid
sequenceDiagram
  autonumber
  participant M as Médico de A
  participant A as Sistema A
  participant B as Sistema B
  participant E as Especialista de B
  M->>A: Remitir a la institución B (con consentimiento del paciente para B)
  A->>B: POST /oauth/token (client_credentials)
  B-->>A: access_token (10 min)
  A->>B: POST /interop/Bundle  (Patient + ServiceRequest + Observation* + DiagnosticReport aprobado)
  alt inválido
    B-->>A: 422 OperationOutcome (un issue por problema; nada se guarda)
  else válido
    B-->>A: 201 Task "requested" + Location /interop/Task/{id}
    B--)E: aviso en vivo + contador (remision_externa_recibida)
    E->>B: acepta, o rechaza con motivo
    B->>A: POST /interop/Task (status accepted | rejected)
    A--)M: aviso en vivo (remision_externa_respondida)
  end
  Note over A,B: Los dos lados auditan con origen externo
```

Si el socio no puede recibir el Task (no implementó `/interop/Task`), puede **consultar** el estado con
`GET /interop/Task/{id}` usando el id que le devolvimos.

## A2. Rutas de Pulso para otros sistemas

Base: `https://<url-publica>/api` (detrás de nginx; directo a la API, sin `/api`).

| Método y ruta | Para qué | Scope | Respuestas |
|---|---|---|---|
| `GET /interop/metadata` | Qué aceptamos y cómo autenticarse (público, sin datos) | — | 200 `CapabilityStatement` |
| `POST /oauth/token` | Token de sistema (OAuth2 `client_credentials`, formulario o HTTP Basic) | — | 200, 400, 401 |
| `POST /interop/Bundle` | Nos remiten un paciente | `system/ServiceRequest.write` | 201 Task, 400, 401, 403, 409, 413, 422 |
| `POST /interop/Task` | Nos responden una remisión que les enviamos | `system/Task.write` | 200 Task, 401, 403, 404, 422 |
| `GET /interop/Task/{id}` | Consultan el estado de una remisión que nos enviaron | `system/ServiceRequest.write` | 200 Task, 401, 404 |
| `POST /interop/Observation` | Un resultado suelto (Parte B) | `system/Observation.write` | 201, 202, 401, 403, 409, 422 |

**Credencial** (una de las dos, por institución):

- `Authorization: Bearer <access_token>` obtenido en `/oauth/token`, o
- `X-API-Key: <client_id>.<client_secret>`.

Las credenciales de sistema **solo** abren estas rutas: no sirven en la API clínica (`/pacientes`,
`/remisiones`…), y un token de usuario no sirve aquí (R31). Si la institución acordó firma, además van
`X-Timestamp`, `X-Nonce` y `X-Signature` (sección 4); la ruta firmada es la de la tabla (sin `/api`).

## A3. El Bundle que recibimos

`resourceType: Bundle` y `type` `transaction` o `collection` (también se aceptan `message`, `batch`,
`document`). Las referencias pueden ser `urn:uuid:...` (fullUrl) o `Tipo/id`.

| Recurso | Cuántos | Obligatorio |
|---|---|---|
| `Patient` | 1 | `identifier` con el documento de identidad y su tipo (`type.coding.code` CC, TI, CE, PA, RC… o el final del `system`), o el id federado de la Parte B. Si el paciente no existe en Pulso: `name` (given y family), `birthDate`, `gender`. **No envíe** teléfono, correo ni dirección: no se guardan. |
| `ServiceRequest` | 1 | `identifier` (o `id`) único: con él se evita recibir dos veces la misma remisión; `status: active`; `intent: order`; `code` (SNOMED CT, p. ej. `3457005` *Patient referral*); `subject` al Patient; `reasonCode` (o `note`) con el motivo; `priority` routine \| urgent \| asap \| stat. Opcional: `performerType` con la especialidad en SNOMED (p. ej. `419192003` medicina interna) o en texto; `requester.display` con el nombre de su institución. |
| `Observation` | 0 a 20 | `code` LOINC; `valueQuantity` con `system: http://unitsofmeasure.org` y `code` UCUM (o `valueString`); `subject` al Patient; `status` final. |
| `DiagnosticReport` | 0 o 1 | `status: final` (el reporte de IA **aprobado**; uno `preliminary` se rechaza); `subject` al Patient; `conclusion`. |

Ejemplo mínimo:

```json
{"resourceType": "Bundle", "type": "transaction", "entry": [
  {"fullUrl": "urn:uuid:p1", "resource": {"resourceType": "Patient",
    "identifier": [{"type": {"coding": [{"code": "CC"}]}, "system": "urn:co:cc", "value": "1144123456"}],
    "name": [{"family": "Pérez", "given": ["Rosa"]}], "gender": "female", "birthDate": "1958-07-09"}},
  {"fullUrl": "urn:uuid:sr1", "resource": {"resourceType": "ServiceRequest",
    "identifier": [{"system": "urn:equipo3:remision", "value": "REM-0042"}],
    "status": "active", "intent": "order", "priority": "urgent",
    "code": {"coding": [{"system": "http://snomed.info/sct", "code": "3457005", "display": "Patient referral"}]},
    "performerType": {"coding": [{"system": "http://snomed.info/sct", "code": "419192003", "display": "Internal medicine"}]},
    "subject": {"reference": "urn:uuid:p1"}, "requester": {"display": "Clínica del Equipo 3"},
    "reasonCode": [{"text": "Hiperpotasemia persistente; valoración por medicina interna"}]}},
  {"fullUrl": "urn:uuid:o1", "resource": {"resourceType": "Observation", "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3", "display": "Potassium"}]},
    "subject": {"reference": "urn:uuid:p1"}, "effectiveDateTime": "2026-11-21T08:00:00Z",
    "valueQuantity": {"value": 6.7, "unit": "mmol/L", "system": "http://unitsofmeasure.org", "code": "mmol/L"}}},
  {"fullUrl": "urn:uuid:dr1", "resource": {"resourceType": "DiagnosticReport", "status": "final",
    "code": {"text": "Reporte de IA"}, "subject": {"reference": "urn:uuid:p1"},
    "conclusion": "Riesgo alto de deterioro. Revisado y aprobado por un clínico."}}]}
```

Qué hace Pulso con un Bundle válido, en **una sola transacción** (si algo falla, no queda nada):

1. Identifica al paciente por id federado o por documento (índice ciego) o lo registra con lo mínimo.
2. Asigna el especialista de la especialidad pedida con menos remisiones abiertas; si esa especialidad no
   tiene especialistas con turno, al de menor carga (que puede rechazarla con motivo).
3. Guarda las Observation en la historia y el reporte del socio con la remisión.
4. Audita (`remision_recibida`, origen externo) y avisa en vivo al especialista.
5. Responde `201` con un `Task` (`status: requested`) y `Location: /interop/Task/{id}`.

## A4. El Task de respuesta

Lo que Pulso **envía** al socio cuando su especialista acepta o rechaza, y lo que **acepta** del socio
para una remisión nuestra:

```json
{"resourceType": "Task", "status": "accepted", "intent": "order",
 "basedOn": [{"identifier": {"system": "https://pulso.hsanrafael.co/fhir/identifier/remision", "value": "57"}}],
 "statusReason": {"text": "La valoramos mañana a las 8:00"}}
```

`status`: `accepted`, `rejected` (con `statusReason.text` como motivo), `completed`, `in-progress` o
`cancelled`. `basedOn` (o `focus`) lleva el `identifier` del ServiceRequest que **nosotros** enviamos
(system `https://pulso.hsanrafael.co/fhir/identifier/remision`, value = nuestro número de remisión).

## A5. Lo que Pulso envía (R32: consentimiento y minimización)

- **Consentimiento:** sin un `Consent` vigente del paciente **para esa institución**, `POST /remisiones/externas`
  responde 409 y no sale nada. El clínico lo registra en la ficha (o el propio paciente) y se puede revocar.
  `GET /pacientes/{id}/consentimientos?formato=fhir` lo devuelve como recurso `Consent`.
- **Reporte de IA:** solo uno **aprobado**; uno pendiente o rechazado da 409.
- **Patient:** documento con su tipo, nombre, sexo, fecha de nacimiento y el id federado. **Nunca**
  teléfono, correo ni dirección.
- **Observation:** las que elija el médico o, si no elige, el último valor de cada examen de los últimos
  30 días (máximo 20).
- Si el socio no responde, la remisión queda guardada (`estado_envio = fallida`) y el médico la reenvía.

## A6. Cómo conectarse con cualquier equipo (checklist)

1. **Ellos → nosotros.** Les damos la URL de Pulso y, por un canal privado, su credencial:
   ```bash
   python scripts/interop_cliente.py socio-crear equipo3 "Clínica del Equipo 3" https://SU-URL/api --sin-firma
   ```
   imprime una sola vez `client_id`, `client_secret` y `api_key`. Ellos leen `GET /interop/metadata`.
2. **Nosotros → ellos.** Les pedimos su URL base, sus rutas (dónde reciben el Bundle y el Task, dónde se
   pide el token) y la credencial que nos dan. Se cargan del entorno, sin pasar por la línea de comandos:
   ```bash
   export SOCIO_CREDENCIAL_ID=... SOCIO_CREDENCIAL_SECRETO=...
   python scripts/interop_cliente.py socio-credenciales equipo3 --auth oauth2   # o api_key / bearer / ninguno
   ```
   Si sus rutas son otras, `socio-crear` acepta `--ruta-bundle`, `--ruta-task`, `--ruta-token` y `--cabecera`
   (nombre de su cabecera de API key).
3. **Su túnel cambió de URL:** `python scripts/interop_cliente.py socio-url equipo3 https://NUEVA/api` o, en
   la pantalla de Administración → Instituciones socias, editar la URL. No hay que redesplegar.
4. **Ensayo sin depender de ellos:** `python scripts/test_remisiones_externas.py` registra a Pulso como su
   propio socio y hace el viaje completo.

## A7. Errores

Siempre un `OperationOutcome` con un `issue` por problema (`diagnostics` dice qué corregir):

| HTTP | Cuándo |
|---|---|
| 400 | El cuerpo no es JSON |
| 401 | Sin credencial, credencial inválida o vencida, firma inválida, nonce repetido, cliente inactivo |
| 403 | La credencial no tiene el scope de esa ruta, o no está asociada a una institución |
| 404 | El Task o la remisión no existe (o no es de su institución) |
| 409 | La misma remisión ya se recibió; no hay especialistas para recibirla |
| 413 | Cuerpo de más de 1 MB |
| 422 | El recurso no cumple el contrato (LOINC, UCUM, SNOMED, referencias, reporte no aprobado…) |

Cada rechazo se audita (`rechazo_externo`) y, si no es una credencial mala o un duplicado, avisa al admin
(`interop_rechazo`).

---

# Parte B · Resultados sueltos (semana 14)

## 1. Qué hace y qué no

Pulso **recibe** una `Observation` FHIR R4 (un resultado: glucosa, potasio, saturación…) de un sistema autorizado y la
guarda en la historia del paciente correcto. Si el valor es crítico, además crea una alerta para el equipo clínico.

No hace (todavía): enviar resultados a otros sistemas, recibir otros recursos FHIR (`Patient`, `Encounter`…), ni
buscar pacientes por solicitud externa. El otro sistema nunca consulta datos de Pulso.

```
 Otro sistema                                   Pulso
 ────────────                                   ─────
 1. POST /oauth/token  ───────────────────────► verifica client_id + client_secret (Argon2id)
    client_credentials                          ◄── access token HS256, 10 min, scope system/Observation.write
 2. POST /interop/Observation ────────────────► token (firma, vencimiento, aud, scope)       → 401 / 403
    Authorization: Bearer …                     firma HMAC del mensaje + reloj + nonce      → 401
    X-Timestamp · X-Nonce · X-Signature         validación FHIR (LOINC, UCUM, id federado)  → 422
                                                mismo id ya recibido                        → 409
                                                paciente federado desconocido               → 202 (pendiente)
                                                ◄── 201 guardada (y alerta si es crítica)
```

## 2. Las cuatro capas, y por qué cada una

| Capa | Responde | Si falta, un atacante puede… |
|---|---|---|
| **OAuth2 client_credentials** | ¿Quién eres? | mandar resultados falsos sin identificarse |
| **Firma HMAC por mensaje** | ¿Este mensaje es tuyo y llegó intacto? | con un token robado (dura 10 min) mandar lo que quiera; o cambiar un valor en el camino |
| **Marca de tiempo + nonce** | ¿No es una repetición? | grabar un mensaje legítimo y reenviarlo mil veces |
| **Identificador federado** | ¿De qué paciente hablas? | (sin él, la cédula viajaría en cada mensaje) |

El token y la firma usan secretos **distintos** por cliente: filtrar uno no da el otro.

## 3. Autenticación: `POST /oauth/token`

Cuerpo `application/x-www-form-urlencoded` (RFC 6749 §4.4); también se acepta HTTP Basic para `client_id:client_secret`.

```
grant_type=client_credentials&client_id=cli_…&client_secret=…&scope=system/Observation.write
```

Respuesta `200` (con `Cache-Control: no-store`):

```json
{"access_token": "eyJhbGciOiJIUzI1NiIs…", "token_type": "Bearer", "expires_in": 600, "scope": "system/Observation.write"}
```

El token es un JWT **HS256** (el algoritmo está fijado: no se acepta `none` ni otro) con `iss=pulso`, `aud=pulso-interop`,
`sub=<client_id>`, `scope`, `type=interop`, `iat`, `exp` (10 minutos) y `jti`. Es de un tipo distinto al de los
usuarios: **no abre la API normal, y un token de usuario no abre esta puerta**. `aud`, `iss` y `exp` son obligatorios
(la biblioteca JWT, por defecto, acepta un token sin `aud`; está forzado y hay prueba que lo cubre).

Errores (RFC 6749 §5.2): `invalid_client` (401, **idéntico** para secreto malo, cliente inexistente o inactivo, y con el
mismo tiempo de cómputo, para no revelar qué clientes existen), `unsupported_grant_type` (400), `invalid_scope` (400).
`/api/oauth/token` tiene en nginx el mismo límite de intentos que el login.

## 4. Firma del mensaje

Cabeceras de cada `POST /interop/Observation` **[ACORDAR nombres con la pareja]**:

| Cabecera | Contenido |
|---|---|
| `Authorization` | `Bearer <access_token>` |
| `X-Timestamp` | segundos Unix (UTC) en que se firmó; tolerancia ±300 s |
| `X-Nonce` | cadena única por mensaje, 16 a 128 caracteres (`A-Z a-z 0-9 _ -`); se recomienda `secrets.token_urlsafe(18)` |
| `X-Signature` | `v1=` + hexadecimal de `HMAC-SHA256(firma_secreto, cadena_a_firmar)` |

**Cadena a firmar** (seis líneas, separadas por `\n`, sin salto al final) **[ACORDAR]**:

```
v1
<X-Timestamp>
<X-Nonce>
POST
/interop/Observation
<SHA-256 hexadecimal de los bytes EXACTOS del cuerpo>
```

Por qué cada línea: la versión permite cambiar el esquema sin romper a nadie; el nonce y la hora hacen único el mensaje;
método y ruta impiden usar una firma de un endpoint en otro; el hash del cuerpo hace que cambiar un solo byte la invalide.
La ruta es siempre `/interop/Observation`, aunque el tráfico entre por `/api/…` (así la firma no depende del proxy).
La firma se calcula sobre los **bytes** que se envían: no se re-serializa el JSON antes de firmar.

```python
import hashlib, hmac, secrets, time
cuerpo = json.dumps(observation, separators=(",", ":")).encode()
ts, nonce = str(int(time.time())), secrets.token_urlsafe(18)
cadena = "\n".join(["v1", ts, nonce, "POST", "/interop/Observation", hashlib.sha256(cuerpo).hexdigest()])
firma = "v1=" + hmac.new(firma_secreto.encode(), cadena.encode(), hashlib.sha256).hexdigest()
```

Orden de verificación en Pulso: token (401/403) → reloj → formato del nonce → firma (comparación en tiempo constante) →
**solo entonces** se anota el nonce (así quien no sabe firmar no puede llenar la tabla) → cuerpo (422) → duplicado (409).

## 5. La Observation

```json
{"resourceType": "Observation", "id": "lab-0001", "status": "final",
 "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
 "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0", "display": "Glucose [Mass/volume] in Blood"}]},
 "subject": {"identifier": {"system": "https://pulso.hsanrafael.co/fhir/identifier/paciente-federado",
                            "value": "fa0d2121aa10d0c2bb7a5acd8b596f2f"}},
 "effectiveDateTime": "2026-11-10T09:30:00Z",
 "valueQuantity": {"value": 95, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"}}
```

Obligatorios: `resourceType`, `id` (con él se evita duplicar; el par `client_id + id` es único), `status`, un código **LOINC**,
`valueQuantity.value` numérico con unidad **UCUM**, `subject.identifier` con el id federado y `effectiveDateTime` ISO 8601.
Una cédula, un nombre o cualquier otro `subject` se rechaza con `422`: el documento no debe viajar.

## 6. Respuestas

| HTTP | Cuándo | Cuerpo |
|---|---|---|
| **201** | Guardada. Si el valor es crítico, `alerta_critica: true` | `{"estado":"aceptada","id":…,"observacion_id":…}` |
| **202** | Aceptada, pero el paciente federado aún no existe en Pulso | `{"estado":"pendiente",…}`; se asocia sola cuando se registre |
| **401** | Sin token, token malo/vencido, firma inválida, reloj fuera de ventana, nonce repetido, cliente inactivo | `OperationOutcome` (`code: security`) |
| **403** | Token válido sin el scope `system/Observation.write` | `OperationOutcome` |
| **409** | Ese `id` ya se recibió de ese cliente | `OperationOutcome` (`code: duplicate`) |
| **413** | Cuerpo de más de 256 KB | `OperationOutcome` |
| **422** | No es JSON, o la Observation no cumple (dice **qué** corregir, una línea por problema) | `OperationOutcome` (`code: invalid`) |
| **503** | El servidor no tiene configurado el secreto federado o la clave JWT | `OperationOutcome` |

Los errores van como `OperationOutcome` de FHIR R4 (`Content-Type: application/fhir+json`), no como texto suelto.

## 7. Identificador federado

Dos sistemas tienen al mismo paciente con la misma cédula, pero ninguno quiere mandar la cédula. Los dos calculan:

```
sid = HMAC-SHA256(secreto_federado, "TIPO|documento")  →  primeros 32 caracteres hexadecimales
```

con el tipo en mayúsculas (`CC`, `TI`…) y el documento sin puntos, espacios ni guiones, en mayúsculas.
Con el secreto de la clase (`secreto-federado-del-curso-solo-demo`), `CC|1144123456` da `fa0d2121aa10d0c2bb7a5acd8b596f2f`
(coincide con el ejemplo de la diapositiva; está en la prueba).

- **Es un seudónimo, no un cifrado:** quien tenga el secreto puede calcular el id de cualquier cédula (hay solo ~10⁹
  cédulas posibles). Por eso el secreto federado se comparte solo con los pares, por un canal privado, y se rota si se filtra.
- Pulso lo calcula al **registrar** a un paciente (`pacientes.sid_federado`) y, para los ya cargados, con
  `python scripts/interop_cliente.py reindexar-federado`. No sale en la auditoría (`<protegido>`) ni en las respuestas.
- 32 hex = 128 bits: colisiones por azar son imposibles en la práctica; si aun así dos pacientes compartieran id, el mensaje
  queda pendiente en lugar de adivinar.

## 8. Qué hace Pulso con un mensaje aceptado

1. Lo guarda en `observaciones` (`origen = fhir_externo`, ligada al encuentro activo si lo hay).
2. Avisa por la bandeja a los clínicos a cargo del paciente (los mismos del sistema reactivo existente).
3. Si el valor es crítico (tabla `CRITICOS` por LOINC, o `interpretation` HH/LL), crea una **alerta crítica** activa
   (`resultado_critico_externo`) para cada clínico responsable, o para el rol médico si no hay ninguno; sigue el mismo
   reconocimiento y escalamiento que las alertas de IA. La alerta **no lleva nombre ni documento**.
4. Audita (`observation_recibida`, `rechazo_observation`, `token_emitido`, `token_rechazado`) en la cadena de auditoría. Los
   rechazos se escriben con una sesión aparte para que no se pierdan con el rollback. Nunca se registran tokens, firmas ni secretos.
5. Si el paciente no existe: guarda el mensaje como **pendiente** (cuerpo incluido, que solo lleva el id federado) y responde
   202. Al registrar al paciente se concilia solo; también con `python scripts/interop_cliente.py conciliar`.

## 9. STRIDE de esta puerta

| Amenaza | Cómo se materializaría | Control |
|---|---|---|
| **S**poofing | Hacerse pasar por el laboratorio | OAuth2 + firma HMAC con un secreto distinto; `invalid_client` uniforme |
| **T**ampering | Cambiar un valor en tránsito | El hash del cuerpo va dentro de la firma |
| **R**epudiation | «Yo no mandé ese resultado» | Auditoría encadenada con `client_id`, id del mensaje y estado |
| **I**nformation disclosure | La cédula viaja en el mensaje | Id federado; `422` si llega una cédula; la alerta no lleva datos personales |
| **D**enial of service | Inundar `/oauth/token` o `/interop/Observation` | `limit_req` en nginx, cuerpo máximo de 256 KB, el nonce solo se guarda tras una firma válida |
| **E**levation of privilege | Usar el token en otra parte (o uno de usuario aquí) | `aud`, `type` y `scope` obligatorios; un solo scope, de escritura de Observation |
| (Replay) | Reenviar un mensaje grabado | Ventana de 5 min + nonce de un solo uso + `id` único (409) |

## 10. Puesta en marcha

Orden obligatorio: **primero la migración, después el código**, porque el código nuevo ya escribe estas columnas.

```bash
python scripts/aplicar_sql.py db/05_seguridad_e_interop.sql     # auditoría, índices ciegos, interoperabilidad, remisiones, consentimiento y agente
python scripts/reindexar_contacto.py                      # teléfono/correo ya cargados
# En .env: INTEROP_SECRETO_FEDERADO (≥16 caracteres, el mismo que los pares) y, opcional, APP_AUDIT_KEY
python scripts/interop_cliente.py reindexar-federado      # id federado de los pacientes ya cargados
python scripts/interop_cliente.py crear "Nombre del sistema par"
```

`crear` muestra `client_id`, `client_secret` y `firma_secreto` **una sola vez**; en la base solo queda el hash Argon2id del
primero y el segundo cifrado con AES-256-GCM (hace falta en claro para verificar firmas). Se entregan al otro equipo por un
canal privado, nunca por git ni por el chat del curso. `desactivar <client_id>` corta el acceso de inmediato: aunque el
token no haya vencido, la verificación de firma consulta si el cliente sigue activo.

## 11. Cómo se probó

- `python scripts/test_interop.py`: id federado (incluido el ejemplo de la clase), OAuth2, separación de tokens, 201/202/409/
  413/422, alerta crítica, manipulación, replay, reloj, cliente desactivado, conciliación y auditoría. Corre en CI.
- `python scripts/interop_probar.py`: hace de «el otro sistema» contra una API en marcha. Las credenciales van en variables de
  entorno (`PULSO_URL`, `PULSO_CLIENT_ID`, `PULSO_CLIENT_SECRET`, `PULSO_FIRMA_SECRETO`), no en la línea de comandos.
  Sirve para que la pareja pruebe su cliente contra nuestro servidor y nosotros el nuestro contra el suyo.

Salida real contra la API local (token recortado; el id federado, abreviado):

```
### 1. Mensaje normal (glucosa 95 mg/dL)
1) POST /oauth/token
   → 200 {'access_token': 'eyJhbGciOiJIUzI1NiIs…', 'token_type': 'Bearer', 'expires_in': 600, 'scope': 'system/Observation.write'}
2) POST /interop/Observation  (id lab-0001, sid 6521c2b0…)
   intento 1: 201 {"estado": "aceptada", "id": "lab-0001", "observacion_id": 3885, "alerta_critica": false}

### 2. Valor crítico (glucosa 38 mg/dL → alerta)
   intento 1: 201 {"estado": "aceptada", "id": "lab-0002", "observacion_id": 3886, "alerta_critica": true}

### 3. Mismo mensaje otra vez (duplicado)
   intento 1: 409 {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "duplicate",
                   "diagnostics": "La Observation lab-0001 ya fue recibida de este cliente"}]}

### 4. Copia exacta con el mismo nonce (replay)
   intento 1: 201 {"estado": "aceptada", "id": "lab-0003", "observacion_id": 3887, "alerta_critica": false}
   intento 2: 401 {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "security",
                   "diagnostics": "Nonce repetido: este mensaje ya se había enviado"}]}

### 5. Firma con una hora de atraso
   intento 1: 401 … "diagnostics": "X-Timestamp fuera de la ventana de 300 s (revisa el reloj)"

### 6. Valor cambiado después de firmar
   intento 1: 401 … "diagnostics": "Firma inválida"

### 7. Paciente federado que aún no existe (pendiente)
   intento 1: 202 {"estado": "pendiente", "mensaje": "Recibida. El paciente federado aún no existe en Pulso; se asociará cuando se registre.", "id": "lab-0006"}
```

## 12. Limitaciones conocidas

- **HS256 y HMAC son secretos compartidos:** quien comprometa el servidor de Pulso puede emitir tokens y verificar (y por tanto
  producir) firmas de cualquier cliente. Con firmas asimétricas (RS256 / mTLS) el servidor solo tendría claves públicas. Se
  eligió HMAC por simplicidad y porque es lo que pide la especificación de la clase.
- **El id federado es un seudónimo reversible por fuerza bruta** para quien tenga el secreto federado (ver §7).
- **Sin mTLS ni lista de IP:** la identidad del cliente descansa en sus secretos. nginx limita la tasa, no filtra por origen.
- **No hay rotación automática** de `client_secret` ni de `firma_secreto`: se crea un cliente nuevo y se desactiva el viejo.
- **`interop_nonces` crece** hasta la siguiente petición (cada una borra los vencidos); con volumen alto conviene una tarea de limpieza.
- **Un mensaje pendiente guarda su cuerpo** (solo id federado y valor clínico) hasta conciliarse. Si el paciente nunca llega, queda.
- **La tabla `CRITICOS`** de valores críticos es la misma del sistema reactivo, no una configuración por cliente.
- **Un solo recurso (`Observation`) y una sola dirección** (entrada).

## 13. Para acordar con el equipo pareja [ACORDAR]

1. Nombres de las cabeceras (`X-Timestamp`, `X-Nonce`, `X-Signature`) y formato `v1=<hex>`.
2. La cadena a firmar de §4, línea por línea (el error más común es firmar un JSON re-serializado).
3. Ruta firmada: `/interop/Observation` (sin `/api`).
4. Fórmula del id federado y normalización del documento (§7), y el **secreto federado**, por canal privado.
5. Sistema de identificadores (`…/paciente-federado`) y qué LOINC / unidades UCUM van a mandar.
6. Quién emite el `client_id` y cuándo se rotan los secretos.
