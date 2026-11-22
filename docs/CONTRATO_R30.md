# Contrato de interoperabilidad R30 · Pulso (Hospital San Rafael)

Documento para los **equipos socios** (R33). Describe cómo nos envían remisiones, cómo les respondemos y cómo nos responden a las que les enviamos. Todo viaja en FHIR R4 (JSON), sin correos ni archivos manuales. El detalle técnico interno está en [INTEROP.md](INTEROP.md).

`<base>` = nuestra URL pública + `/api`, por ejemplo `https://<tunel>.trycloudflare.com/api`. La URL del túnel cambia al reiniciarse: se confirma el día de la prueba. `GET <base>/interop/metadata` (sin autenticación) devuelve este mismo contrato en forma de `CapabilityStatement`.

## 1. Autenticación (R31)

Cada institución entra con **credenciales de sistema**, nunca con un usuario humano. Nosotros les creamos un `client_id` y un `client_secret` y se los entregamos por un canal privado; ustedes nos dan los suyos por la misma vía. Los secretos se guardan cifrados y nunca aparecen en la auditoría.

**Opción A · OAuth2 client credentials (recomendada)**

```http
POST <base>/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<id>&client_secret=<secreto>[&scope=bundle:write task:write]
```

Respuesta: `{"access_token": "...", "token_type": "Bearer", "expires_in": 600, "scope": "..."}`. El token dura **10 minutos** y solo sirve para las rutas de entrada `/interop/*`; en la API clínica responde 401.

| Permiso (scope) | Alias aceptado | Para qué |
|---|---|---|
| `system/ServiceRequest.write` | `bundle:write` | Enviarnos remisiones (`/interop/Bundle`) |
| `system/Task.write` | `task:write` | Respondernos (`/interop/Task`) |

Si no piden `scope`, el token trae todos los permisos de su cliente. Un `system/...` que no tengan asignado → 400 `invalid_scope`.

**Opción B · API key:** cabecera `X-API-Key: <client_id>.<client_secret>` en cada petición, sin pasar por `/oauth/token`.

Cada petición de entrada lleva:

| Cabecera | Valor |
|---|---|
| `Authorization` | `Bearer <access_token>` (o `X-API-Key`) |
| `x-nonce` | **Recomendado.** Valor nuevo en cada envío: 16 a 128 caracteres de `A-Z a-z 0-9 - _` (un UUID sirve). Si lo mandan repetido → 409; si es inválido → 400. El nonce se gasta aunque el envío se rechace |
| `Content-Type` | `application/fhir+json` (también aceptamos `application/json`) |

Opcional: si acuerdan firma HMAC, el cliente queda configurado para exigir `X-Timestamp`, `X-Nonce` y `X-Signature` (detalle en `/interop/metadata`).

## 2. Ustedes nos remiten un paciente

```http
POST <base>/interop/Bundle        (permiso bundle:write)
```

Cuerpo: un `Bundle` (`transaction`, `collection`, `batch`, `message` o `document`) con **una remisión**:

| Recurso | Cantidad | Reglas |
|---|---|---|
| `Patient` | exactamente 1 | Basta con un seudónimo (`id` o `identifier`): no hace falta nombre ni documento (R32). Si mandan documento, que traiga su tipo (`CC`, `TI`, `CE`, `PA`…). `birthDate` en `AAAA`, `AAAA-MM` o `AAAA-MM-DD`; `gender` opcional. Teléfono, correo y dirección **se descartan**: no los guardamos |
| `ServiceRequest` | exactamente 1 | `status = active`; `intent` = `order`, `original-order`, `plan`, `proposal` o `directive`; `code` con SNOMED CT (por ejemplo `3457005` o `306206005`) o al menos `text`; `subject` al Patient; **motivo** en `reasonCode[].text` (o `note`), mínimo 5 caracteres; `priority` = `routine`, `urgent`, `asap` o `stat`; `id` o `identifier` **obligatorio** (con él evitamos recibirla dos veces). La especialidad va en `performerType` (SNOMED o `text`, ver tabla abajo) |
| `Observation` | 0 o más | Código **LOINC** (`http://loinc.org`); `status` = `final`, `amended`, `corrected` o `preliminary`; valor en `valueQuantity` (numérico, idealmente UCUM), `valueString`, `valueCodeableConcept` o `component` (p. ej. presión arterial); `subject` al Patient |
| `DiagnosticReport` | 0 o 1 | Solo el reporte de IA **aprobado**: `status` = `final`, `amended` o `corrected`; `subject` al Patient |
| `Consent` | 0 o 1 | Si viene, `status = active`. Lo guardamos como evidencia del consentimiento del paciente |
| `Task` | 0 o 1 | `status = requested`, `focus` al ServiceRequest. **Recomendado**: guardamos su `id` y con él les respondemos |
| Otros (`Practitioner`, `Organization`, `Condition`…) | opcionales | Se ignoran |

- Las entradas pueden ir con `POST` o `PUT` (url = `Tipo/id`). Las referencias pueden ser `Tipo/id` o `urn:uuid:`.
- **Para que podamos responderles, envíen ids estables** (`PUT ServiceRequest/<id>`, `PUT Task/<id>`, `PUT Patient/<id>`): nuestra respuesta apunta a esos ids.
- Sus recursos se guardan con ids propios nuestros: no pueden sobrescribir nada.
- Límite: 1 MB por Bundle.

**Especialidades que atendemos** (`performerType.text` debe contener el nombre, sin importar mayúsculas):
Cirugía general · Cirugía cardiovascular · Neurocirugía · Ortopedia y traumatología · Urología · Ginecobstetricia · Cirugía plástica · Cirugía vascular · Oftalmología · Otorrinolaringología · Anestesiología · Medicina interna · Medicina general · Medicina de urgencias.
Si no traen especialidad la asignamos a Medicina interna. Si la especialidad no tiene especialista con turno, la recibe el especialista con menos carga, que la atiende, la redirige internamente o la rechaza con motivo: **nunca se pierde**.

| Respuesta | Cuándo | Cuerpo |
|---|---|---|
| **201** | Remisión recibida; el especialista asignado la ve en vivo | `Task` `requested` con nuestro id; cabecera `Location: /interop/Task/<id>` |
| 400 | El cuerpo no es JSON, o `x-nonce` inválido | `OperationOutcome` |
| 401 | Sin credencial, token vencido o alterado, institución desactivada | `OperationOutcome` |
| 403 | La credencial no tiene el permiso | `OperationOutcome` |
| 409 | `x-nonce` repetido, esa misma remisión (`ServiceRequest`) ya fue recibida, o no hay ningún especialista disponible | `OperationOutcome` (`duplicate` / `business-rule`) |
| 413 | Más de 1 MB | `OperationOutcome` |
| **422** | Bundle o recurso inválido. **No se guarda nada** (transacción atómica) | `OperationOutcome` con **un `issue` por problema** en `diagnostics` |

Pueden consultar el estado en cualquier momento:

```http
GET <base>/interop/Task/<nuestro id>     (misma credencial; solo ven sus propias remisiones, si no → 404)
```

## 3. Les respondemos (aceptación o rechazo)

Cuando nuestro especialista acepta o rechaza, enviamos un `Task` a **su** endpoint de respuesta, autenticándonos con las credenciales que ustedes nos den (OAuth2, API key o Bearer fijo) y con `X-Nonce` y `X-Timestamp` nuevos:

```json
{
  "resourceType": "Task",
  "id": "<id del Task que ustedes enviaron, si lo enviaron>",
  "status": "accepted",
  "intent": "order",
  "priority": "urgent",
  "code": {"coding": [{"system": "http://snomed.info/sct", "code": "3457005", "display": "Patient referral"}]},
  "focus": {"reference": "ServiceRequest/<su id>", "identifier": {"value": "<su id>"}, "type": "ServiceRequest"},
  "basedOn": [{"reference": "ServiceRequest/<su id>", "identifier": {"value": "<su id>"}, "type": "ServiceRequest"}],
  "for": {"reference": "Patient/<su id>"},
  "statusReason": {"text": "<motivo, si el especialista lo escribió>"},
  "businessStatus": {"text": "Aceptada: el especialista atenderá al paciente"},
  "owner": {"display": "Hospital San Rafael (Pulso)"},
  "lastModified": "2026-11-21T20:05:00-05:00",
  "identifier": [{"system": "https://pulso.hsanrafael.co/fhir/identifier/remision", "value": "<nuestro id>"}]
}
```

`status` es `accepted` o `rejected` (y `completed` cuando el paciente ya fue atendido). Esperamos un **2xx**; con cualquier otro código la remisión queda marcada como fallida en nuestra aplicación, con el código y el cuerpo que nos devolvieron, y mientras tanto ustedes pueden consultar el estado con `GET /interop/Task/<id>`.

**Necesitamos de ustedes:** la URL base, la ruta de su token (si usan OAuth2), la ruta donde reciben el Bundle, la ruta donde reciben la respuesta, y nuestras credenciales en su sistema (`client_id` + `client_secret`, o una API key y el nombre de su cabecera). El administrador las cambia desde la aplicación sin redesplegar, por ejemplo si su túnel cambia.

## 4. Ustedes nos responden (a remisiones que les enviamos)

```http
POST <base>/interop/Task        (permiso task:write)
```

Cuerpo: un `Task` con:
- `status` = `accepted` o `rejected` (también entendemos `in-progress`, `completed`, `cancelled`, `failed`);
- **uno** de estos para saber a cuál remisión responden: `focus` / `basedOn` a nuestro `ServiceRequest/rem-<n>` (o su `identifier`), o `id` igual al del Task que les enviamos (`rem-<n>`);
- el motivo en `statusReason.text` (opcional; también leemos `businessStatus.text` o `note`).

Solo se aceptan respuestas a remisiones que **les enviamos a ustedes**.

| Respuesta | Cuándo |
|---|---|
| **200** | Registrada; el médico que remitió recibe el aviso en vivo. Devolvemos nuestro Task actualizado |
| 200 | La misma respuesta otra vez (reintento): no se repite el aviso |
| 403 | Su credencial no está asociada a una institución socia |
| 404 | No corresponde a ninguna remisión que les hayamos enviado |
| 422 | Task inválido (estado desconocido o sin referencia a nuestra remisión) |
| 400 / 401 / 409 | Igual que en la sección 2 |

## 5. Lo que enviamos nosotros

Nuestro Bundle (`transaction`, todo con `PUT` e ids estables) trae:
- `Patient` `pac-…` con documento, nombre, sexo y fecha de nacimiento; **nunca** teléfono, correo ni dirección (R32).
- `ServiceRequest` `rem-<n>`: SNOMED `3457005`, prioridad, motivo, especialidad en `performerType` y `supportingInfo` a lo que sigue.
- `Observation` `obs-…`: los resultados recientes del paciente (LOINC + UCUM), máximo 20.
- `DiagnosticReport` `rep-…`: el reporte de IA, **solo si un clínico lo aprobó**.
- `Consent` `con-…` activo con su fecha.
- `Task` `rem-<n>` `requested`.

Solo se envía si el paciente autorizó compartir con **esa** institución (Ley 1581 de 2012); sin consentimiento la aplicación responde 409 y no sale nada. Como identificador de su respuesta aceptamos su `X-Remision-Externa`, el `id` del cuerpo o `Location`.

## 6. Auditoría

Los dos lados auditan el intercambio. Nosotros registramos cada recepción, cada rechazo por validación o credencial (con IP y client_id), cada envío, cada respuesta suya y cada respuesta nuestra, dentro de la auditoría encadenada con hashes (R28). Los eventos llegan en vivo a la bandeja: `remision_externa_recibida`, `remision_externa_respondida` e `interop_rechazo`.

## 7. Socios probados

| Institución | Fecha | Resultado |
|---|---|---|
| Sistema de apoyo al triaje hospitalario | 2026-11-21 | Ida y vuelta en ambos sentidos: remisión recibida y aceptada, respuesta registrada en los dos sistemas |
