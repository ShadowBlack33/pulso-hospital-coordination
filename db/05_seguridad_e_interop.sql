-- ============================================================================
-- 05_seguridad_e_interop.sql · Semana 14 (Proteger y conectar): todo lo de base de datos en un solo archivo
--
--   1. Auditoría encadenada (STRIDE: repudio). Cada registro de log_auditoria queda sellado con
--      una huella HMAC-SHA256 que cubre su contenido y la huella del anterior (64 ceros en el
--      primero). Editar, borrar o reordenar rompe la cadena en ese punto y el admin lo ve en
--      GET /admin/auditoria/verificar. La huella la calcula la aplicación
--      (app/services/auditoria_cadena.py); la clave no está en la base, así que quien solo tenga
--      acceso a la base no puede recalcular una cadena que parezca válida. Los registros
--      anteriores quedan sin huella ("anteriores a la cadena").
--   2. Índice ciego de teléfono y correo (pacientes y personal): HMAC del valor normalizado
--      (app/core/crypto.py: blind_index_contacto) con APP_BLIND_INDEX_KEY y un prefijo por tabla
--      y campo. A diferencia de documento_bidx NO son únicos (una familia comparte teléfono).
--   3. Interoperabilidad (docs/INTEROP.md): clientes OAuth2, nonces anti-replay, mensajes
--      recibidos (aceptados o pendientes de conciliar) e identificador federado del paciente.
--   4. Remisiones entre instituciones (R30-R32): socios (a quién enviamos y cómo nos
--      autenticamos con cada uno), remisiones salientes y entrantes, y consentimiento del
--      paciente por institución destino.
--   5. Agente y defensa contra inyección (R24-R25): el payload exacto enviado al LLM,
--      incidentes de inyección con usuario e IP, y bloqueos temporales.
--
-- Se puede correr cuantas veces haga falta. Base nueva: después de esquema_completo.sql y
-- 03_rag.sql y ANTES de cargar datos. Base que ya existe (la compartida en Neon): aplicarlo ANTES
-- de desplegar el código nuevo, porque el código ya escribe estas columnas:
--   python scripts/aplicar_sql.py db/05_seguridad_e_interop.sql
--   python scripts/reindexar_contacto.py                      # teléfono y correo ya cargados
--   python scripts/interop_cliente.py reindexar-federado      # id federado de los pacientes ya cargados
-- Los clientes de interoperabilidad NO se crean aquí: python scripts/interop_cliente.py crear <nombre>
-- ============================================================================
BEGIN;

-- ---------------------------------------------------------------- 1. Auditoría encadenada
ALTER TABLE public.log_auditoria
    ADD COLUMN IF NOT EXISTS huella_anterior character varying(64),
    ADD COLUMN IF NOT EXISTS huella           character varying(64);

-- Dos registros no pueden tener la misma huella (cada uno incluye su id y la huella anterior).
CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_huella
    ON public.log_auditoria (huella) WHERE huella IS NOT NULL;

COMMENT ON COLUMN public.log_auditoria.huella_anterior IS
    'Huella del registro anterior de la cadena (64 ceros en el primero).';
COMMENT ON COLUMN public.log_auditoria.huella IS
    'HMAC-SHA256 hexadecimal del registro y de huella_anterior; la clave no está en la base.';

-- ---------------------------------------------------------------- 2. Índices ciegos de contacto
ALTER TABLE public.pacientes
    ADD COLUMN IF NOT EXISTS telefono_bidx character varying(64),
    ADD COLUMN IF NOT EXISTS email_bidx    character varying(64);

ALTER TABLE public.personal
    ADD COLUMN IF NOT EXISTS telefono_bidx character varying(64),
    ADD COLUMN IF NOT EXISTS email_bidx    character varying(64);

CREATE INDEX IF NOT EXISTS idx_pacientes_telefono_bidx ON public.pacientes (telefono_bidx) WHERE telefono_bidx IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pacientes_email_bidx    ON public.pacientes (email_bidx)    WHERE email_bidx IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_personal_telefono_bidx  ON public.personal  (telefono_bidx) WHERE telefono_bidx IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_personal_email_bidx     ON public.personal  (email_bidx)    WHERE email_bidx IS NOT NULL;

COMMENT ON COLUMN public.pacientes.telefono_bidx IS 'HMAC-SHA256 del teléfono normalizado (sin indicativo 57). No único.';
COMMENT ON COLUMN public.pacientes.email_bidx    IS 'HMAC-SHA256 del correo en minúsculas. No único.';
COMMENT ON COLUMN public.personal.telefono_bidx  IS 'HMAC-SHA256 del teléfono normalizado (sin indicativo 57). No único.';
COMMENT ON COLUMN public.personal.email_bidx     IS 'HMAC-SHA256 del correo en minúsculas. No único.';

-- ---------------------------------------------------------------- 3. Interoperabilidad
CREATE TABLE IF NOT EXISTS public.interop_clientes (
    client_id              character varying(64) PRIMARY KEY,
    nombre                 character varying(120) NOT NULL,
    secreto_hash           text NOT NULL,
    firma_secreto_cifrado  bytea NOT NULL,
    scopes                 text NOT NULL DEFAULT 'system/Observation.write',
    activo                 boolean NOT NULL DEFAULT true,
    created_at             timestamp with time zone NOT NULL DEFAULT now(),
    ultimo_uso             timestamp with time zone
);

CREATE TABLE IF NOT EXISTS public.interop_nonces (
    client_id  character varying(64) NOT NULL,
    nonce      character varying(128) NOT NULL,
    expira     timestamp with time zone NOT NULL,
    PRIMARY KEY (client_id, nonce)
);
CREATE INDEX IF NOT EXISTS idx_interop_nonces_expira ON public.interop_nonces (expira);

CREATE TABLE IF NOT EXISTS public.interop_mensajes (
    id              bigserial PRIMARY KEY,
    client_id       character varying(64) NOT NULL,
    id_externo      character varying(64) NOT NULL,
    sid_federado    character varying(32),
    estado          character varying(12) NOT NULL,
    observacion_id  integer,
    cuerpo          jsonb,
    created_at      timestamp with time zone NOT NULL DEFAULT now(),
    conciliado_at   timestamp with time zone,
    CONSTRAINT ck_interop_estado CHECK (estado IN ('aceptada', 'pendiente'))
);
-- Un mismo mensaje (client_id + id) no se acepta dos veces: la segunda vez es 409.
CREATE UNIQUE INDEX IF NOT EXISTS ux_interop_mensaje ON public.interop_mensajes (client_id, id_externo);
CREATE INDEX IF NOT EXISTS idx_interop_pendientes ON public.interop_mensajes (sid_federado) WHERE estado = 'pendiente';

ALTER TABLE public.pacientes ADD COLUMN IF NOT EXISTS sid_federado character varying(32);
CREATE INDEX IF NOT EXISTS idx_pacientes_sid_federado ON public.pacientes (sid_federado) WHERE sid_federado IS NOT NULL;
COMMENT ON COLUMN public.pacientes.sid_federado IS 'HMAC-SHA256(secreto federado, TIPO|documento), 32 hex. No único: lo calcula el sistema, no el usuario.';


-- ---------------------------------------------------------------- 4. Remisiones entre instituciones
-- Un socio es otra institución. De él guardamos cómo LLAMARLO (base_url, rutas y credenciales que
-- nos dio, cifradas) y con qué cliente ENTRA a Pulso (client_id de interop_clientes).
CREATE TABLE IF NOT EXISTS public.interop_socios (
    id                         serial PRIMARY KEY,
    codigo                     character varying(40) NOT NULL UNIQUE,
    nombre                     character varying(120) NOT NULL,
    base_url                   text NOT NULL,
    ruta_bundle                text NOT NULL DEFAULT '/interop/Bundle',
    ruta_task                  text NOT NULL DEFAULT '/interop/Task',
    ruta_token                 text NOT NULL DEFAULT '/oauth/token',
    modo_auth                  character varying(12) NOT NULL DEFAULT 'oauth2',
    credencial_id              character varying(200),
    credencial_secreto_cifrado bytea,
    api_key_cabecera           character varying(60) NOT NULL DEFAULT 'X-API-Key',
    firma_secreto_cifrado      bytea,
    client_id                  character varying(64) REFERENCES public.interop_clientes(client_id),
    activo                     boolean NOT NULL DEFAULT true,
    created_at                 timestamp with time zone NOT NULL DEFAULT now(),
    updated_at                 timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT ck_socio_auth CHECK (modo_auth IN ('oauth2', 'api_key', 'bearer', 'ninguno'))
);

-- La firma HMAC por mensaje se exige solo a los clientes que la soportan; a los demás les basta
-- el token o la API key (R31). Scopes nuevos para remisiones y respuestas.
ALTER TABLE public.interop_clientes ADD COLUMN IF NOT EXISTS exige_firma boolean NOT NULL DEFAULT true;

-- Remisiones: 'interna' (como siempre), 'saliente' (a otra institución: no hay especialista
-- nuestro) o 'entrante' (de otra institución: no hay médico nuestro).
ALTER TABLE public.remisiones ALTER COLUMN especialista_id DROP NOT NULL;
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS direccion character varying(10) NOT NULL DEFAULT 'interna';
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS socio_id integer REFERENCES public.interop_socios(id);
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS client_id character varying(64);
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS id_externo character varying(128);
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS estado_envio character varying(12);
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS detalle_envio text;
ALTER TABLE public.remisiones ADD COLUMN IF NOT EXISTS datos_externos jsonb;
ALTER TABLE public.remisiones DROP CONSTRAINT IF EXISTS ck_rem_direccion;
ALTER TABLE public.remisiones ADD CONSTRAINT ck_rem_direccion CHECK (
    direccion IN ('interna', 'saliente', 'entrante')
    AND (direccion = 'saliente' OR especialista_id IS NOT NULL)
    AND (direccion <> 'saliente' OR socio_id IS NOT NULL));
-- Una misma remisión de un socio no entra dos veces (409).
CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_entrante ON public.remisiones (client_id, id_externo)
    WHERE direccion = 'entrante';

-- Consentimiento para compartir con UNA institución (Ley 1581/2012: transferencia de datos
-- sensibles). Sin uno vigente no se remite. Revocar no borra: deja la fecha.
CREATE TABLE IF NOT EXISTS public.consentimientos (
    id              serial PRIMARY KEY,
    paciente_id     character(64) NOT NULL REFERENCES public.pacientes(documento_bidx),
    socio_id        integer NOT NULL REFERENCES public.interop_socios(id),
    finalidad       character varying(20) NOT NULL DEFAULT 'remision',
    soporte         character varying(200),
    otorgado_at     timestamp with time zone NOT NULL DEFAULT now(),
    registrado_por  integer REFERENCES public.usuarios(id),
    revocado_at     timestamp with time zone,
    revocado_por    integer REFERENCES public.usuarios(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_consentimiento_vigente
    ON public.consentimientos (paciente_id, socio_id, finalidad) WHERE revocado_at IS NULL;

-- Las salientes no tienen especialista nuestro: el permiso para crearlas es el mismo de remitir.

-- ---------------------------------------------------------------- 5. Agente y defensa contra inyección
-- El JSON exacto (ya seudonimizado) que recibió el LLM en cada llamada del agente (R24).
CREATE TABLE IF NOT EXISTS public.agente_payloads (
    id              bigserial PRIMARY KEY,
    usuario_id      integer,
    conversacion    character varying(120),
    paciente_alias  character varying(64),
    modelo          character varying(80),
    huella          character varying(64) NOT NULL,
    payload         jsonb NOT NULL,
    created_at      timestamp with time zone NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agente_payloads_conv ON public.agente_payloads (conversacion, id);

-- Intentos de inyección detectados en la pregunta del usuario (R25): quién y desde qué IP.
CREATE TABLE IF NOT EXISTS public.incidentes_seguridad (
    id          bigserial PRIMARY KEY,
    usuario_id  integer,
    ip          character varying(64),
    canal       character varying(20) NOT NULL,
    reglas      text NOT NULL,
    created_at  timestamp with time zone NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_incidentes_usuario ON public.incidentes_seguridad (usuario_id, created_at);
CREATE INDEX IF NOT EXISTS idx_incidentes_ip ON public.incidentes_seguridad (ip, created_at);

-- Bloqueo temporal por usuario o por IP; el admin lo puede levantar antes.
CREATE TABLE IF NOT EXISTS public.bloqueos_temporales (
    id             bigserial PRIMARY KEY,
    usuario_id     integer,
    ip             character varying(64),
    motivo         character varying(60) NOT NULL,
    hasta          timestamp with time zone NOT NULL,
    levantado_at   timestamp with time zone,
    levantado_por  integer,
    created_at     timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT ck_bloqueo_sujeto CHECK (usuario_id IS NOT NULL OR ip IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_bloqueos_vigentes ON public.bloqueos_temporales (hasta) WHERE levantado_at IS NULL;

COMMIT;
