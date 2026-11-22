"""
Memoria del agente (Semana 13, Parte 10):

  · Corto plazo — checkpointer: los mensajes de cada hilo de conversación. Hace falta además
    para pausar y reanudar una acción que espera aprobación (sin estado guardado no hay qué
    reanudar).
  · Largo plazo — store: preferencias de trabajo del usuario ("prefiero respuestas cortas").
    Nunca datos de pacientes: esos viven en la historia clínica.

Con AGENTE_MEMORIA=postgres se guardan en la misma base de la API (DATABASE_URL); LangGraph
crea sus tablas (checkpoints, checkpoint_writes, checkpoint_blobs, store...) la primera vez.
Si la base no responde, se usa memoria del proceso y se avisa en el registro: el agente
funciona igual, pero las conversaciones se pierden al reiniciar la API.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app.core.config import get_settings

log = logging.getLogger("hsr.agente")


def _conninfo(url: str) -> str:
    """postgresql+psycopg2://... (SQLAlchemy) → postgresql://... (psycopg 3)."""
    for prefijo in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql://" + url[len(prefijo):]
    return url


async def _abrir_postgres(url: str):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver   # noqa: PLC0415
    from langgraph.store.postgres.aio import AsyncPostgresStore         # noqa: PLC0415
    from psycopg.rows import dict_row                                   # noqa: PLC0415
    from psycopg_pool import AsyncConnectionPool                        # noqa: PLC0415
    # prepare_threshold=None: sin sentencias preparadas, porque el pooler de Neon
    # (PgBouncer en modo transacción) no las soporta.
    pool = AsyncConnectionPool(_conninfo(url), min_size=1, max_size=4, open=False,
                               kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row})
    try:
        await pool.open(wait=True, timeout=15)
        saver, store = AsyncPostgresSaver(pool), AsyncPostgresStore(pool)
        # Con tiempo máximo: si la base se cuelga creando sus tablas, la API no puede quedarse
        # sin arrancar (el front se quedaría cargando). Se sigue con memoria del proceso.
        await asyncio.wait_for(saver.setup(), timeout=30)
        await asyncio.wait_for(store.setup(), timeout=30)
    except BaseException:
        await pool.close()
        raise
    return pool, saver, store


@asynccontextmanager
async def abrir_memoria():
    """Devuelve (checkpointer, store, tipo). Un solo yield: los errores de la app no se tragan aquí."""
    from langgraph.checkpoint.memory import InMemorySaver   # noqa: PLC0415
    from langgraph.store.memory import InMemoryStore         # noqa: PLC0415
    c = get_settings()
    pool = None
    saver, store, tipo = InMemorySaver(), InMemoryStore(), "memoria"
    if c.agente_memoria == "postgres":
        try:
            pool, saver, store = await _abrir_postgres(c.database_url)
            tipo = "postgres"
            log.info("Agente: memoria en PostgreSQL")
        except Exception:  # noqa: BLE001
            log.warning("Agente: no se pudo abrir la memoria en PostgreSQL; se usa memoria del proceso",
                        exc_info=True)
    try:
        yield saver, store, tipo
    finally:
        if pool is not None:
            await pool.close()
