"""
El harness (arnés) del agente: todo lo que rodea al modelo para que sea seguro usarlo en un
hospital. Semana 13, Partes 9 a 13.

    mensaje del médico
        │  POST /agente/chat  (rol, paciente de la ficha verificado)
        ▼
    Harness.atender ── ¿cola llena? → "Lati está ocupada"
        │  candado por hilo · tiempo máximo del turno
        ▼
    create_agent (LangChain) con middleware, en este orden:
      1. prompt dinámico     identidad + reglas + skills + contexto + preferencias
      2. VentanaContexto     turnos anteriores sin herramientas ni marcas [F#]; ventana corta
      3. límite de llamadas  máximo AGENTE_MAX_LLAMADAS al modelo por turno
      4. CadenaModelos       Gemini lite; si uno da 429 pasa al siguiente (pausa compartida con el RAG)
      5. LimiteProveedor     semáforo: máximo AGENTE_MAX_SIMULTANEAS llamadas a Gemini a la vez
      6. HumanInTheLoop      proponer_remision se pausa hasta que el clínico decida
      7. Auditoria           cada herramienta queda en log_auditoria (R13)
        ▼
    herramientas (herramientas.py) ── cada una con los permisos del perfil
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from collections import defaultdict

from langchain.agents import create_agent
from langchain.agents.middleware import (AgentMiddleware, HumanInTheLoopMiddleware, ModelCallLimitMiddleware,
                                         ModelRequest, dynamic_prompt)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from app.agente import skills
from app.agente.contexto import ContextoAgente
from app.agente.herramientas import HERRAMIENTAS, REQUIEREN_APROBACION, espacio_memoria
from app.agente.modelo import crear_modelo, modelos_del_agente
from app.agente.prompt import armar_prompt
from app.core.config import get_settings

log = logging.getLogger("hsr.agente")
_MARCA = re.compile(r"\[F\d+\]")


class SinModeloDisponible(Exception):
    """Todos los modelos del agente fallaron o están sin cuota."""


def _texto(m) -> str:
    """Texto de un mensaje (Gemini 3 devuelve bloques: texto + firmas de pensamiento)."""
    t = getattr(m, "text", None)          # propiedad en langchain-core 1.x
    if isinstance(t, str):
        return str(t)
    c = m.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


# ------------------------------------------------------------------ 1. prompt dinámico
@dynamic_prompt
def prompt_lati(request: ModelRequest) -> str:
    ctx: ContextoAgente = request.runtime.context
    humanos = sum(1 for m in request.state["messages"] if isinstance(m, HumanMessage))
    return armar_prompt(ctx, skills.indice(), ctx.preferencias, primer_turno=humanos <= 1)


# ------------------------------------------------------------------ 2. ventana de contexto
class VentanaContexto(AgentMiddleware):
    """
    Memoria de trabajo. De los turnos anteriores solo pasan las preguntas y las respuestas
    finales (sin resultados de herramientas ni marcas [F#], que apuntaban a fuentes de otro
    turno), y solo las últimas `max_previos`. Ahorra tokens de la cuota gratuita y evita que
    el modelo reutilice datos viejos como si fueran de ahora.
    """

    def __init__(self, max_previos: int = 8):
        super().__init__()
        self.max_previos = max_previos

    def _recortar(self, request: ModelRequest) -> ModelRequest:
        msgs = list(request.messages)
        ultimo = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=0)
        previos = []
        for m in msgs[:ultimo]:
            if isinstance(m, HumanMessage):
                previos.append(m)
            elif isinstance(m, AIMessage) and not m.tool_calls:
                texto = _MARCA.sub("", _texto(m)).strip()
                if texto:
                    previos.append(AIMessage(content=texto))
        previos = previos[-self.max_previos:]
        while previos and not isinstance(previos[0], HumanMessage):
            previos.pop(0)
        # Del turno actual: todo, menos llamadas a herramientas que quedaron sin respuesta
        # (un turno cortado por el tiempo máximo).
        actual = msgs[ultimo:]
        respondidas = {m.tool_call_id for m in actual if isinstance(m, ToolMessage)}
        limpio = [m for m in actual if not (isinstance(m, AIMessage) and m.tool_calls
                                            and any(tc["id"] not in respondidas for tc in m.tool_calls))]
        return request.override(messages=previos + limpio)

    def wrap_model_call(self, request, handler):
        return handler(self._recortar(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._recortar(request))


# ------------------------------------------------------------------ 4. cadena de modelos
def _es_cuota(e: Exception) -> bool:
    texto = f"{type(e).__name__} {e}"
    return getattr(e, "code", None) == 429 or "429" in texto or "RESOURCE_EXHAUSTED" in texto \
        or "ResourceExhausted" in texto or "quota" in texto.lower()


class CadenaModelos(AgentMiddleware):
    """
    Prueba los modelos de AGENTE_MODELOS en orden. Un 429 pone ese modelo en pausa (un minuto,
    o hasta el reinicio diario si fue la cuota del día) en el MISMO registro que usa el RAG
    (rag.marcar_agotado), y se pasa al siguiente. Si ninguno responde: SinModeloDisponible.
    """

    def _candidatos(self) -> list[str]:
        from app.services import rag   # noqa: PLC0415
        return [m for m in modelos_del_agente() if rag.disponible(m)]

    def _fallo(self, nombre: str, e: Exception) -> None:
        from app.services import rag   # noqa: PLC0415
        if _es_cuota(e):
            rag.marcar_agotado(nombre, str(e))
        log.warning("Agente: el modelo %s falló: %s", nombre, str(e)[:200])

    def wrap_model_call(self, request, handler):
        for nombre in self._candidatos():
            try:
                r = handler(request.override(model=crear_modelo(nombre)))
                request.runtime.context.modelo_usado = nombre
                return r
            except GraphBubbleUp:
                raise
            except Exception as e:  # noqa: BLE001
                self._fallo(nombre, e)
        raise SinModeloDisponible("ningún modelo del agente respondió")

    async def awrap_model_call(self, request, handler):
        for nombre in self._candidatos():
            try:
                r = await handler(request.override(model=crear_modelo(nombre)))
                request.runtime.context.modelo_usado = nombre
                return r
            except GraphBubbleUp:
                raise
            except Exception as e:  # noqa: BLE001
                self._fallo(nombre, e)
        raise SinModeloDisponible("ningún modelo del agente respondió")


# ------------------------------------------------------------------ 5. límite del proveedor
class LimiteProveedor(AgentMiddleware):
    """Semáforo: como mucho `maximo` llamadas a Gemini a la vez, venga de quien venga."""

    def __init__(self, maximo: int):
        super().__init__()
        self.maximo = maximo
        self._sem_async = asyncio.Semaphore(maximo)
        self._sem_hilos = threading.Semaphore(maximo)

    def wrap_model_call(self, request, handler):
        with self._sem_hilos:
            return handler(request)

    async def awrap_model_call(self, request, handler):
        async with self._sem_async:
            return await handler(request)


# ------------------------------------------------------------------ 7. auditoría
def _auditar(ctx: ContextoAgente, operacion: str, detalle: dict, resultado: str = "exito") -> None:
    """Escribe en log_auditoria (append-only). _sanear quita identificadores y acorta textos."""
    from app.core.deps import SessionLocal          # noqa: PLC0415
    from app.models import Usuario                  # noqa: PLC0415
    from app.services.auditoria import registrar    # noqa: PLC0415
    db = SessionLocal()
    try:
        registrar(db, db.get(Usuario, ctx.usuario_id), "agente", ctx.paciente_alias, operacion,
                  valor_nuevo=detalle, resultado=resultado)
        db.commit()
    except Exception:  # noqa: BLE001 -- la auditoría no debe tumbar la respuesta, pero sí quedar en el log
        log.exception("No se pudo auditar %s", operacion)
        db.rollback()
    finally:
        db.close()


def _contenido(c) -> str | list:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return [x.get("text", x) if isinstance(x, dict) else str(x) for x in c]
    return str(c)


def serializar_llamada(request) -> dict:
    """El payload EXACTO que va al LLM: prompt de sistema, mensajes (con lo que devolvieron las herramientas),
    nombres de las herramientas y modelo. Es lo que se audita y lo que revisa la prueba de datos canario (R24)."""
    mensajes = []
    sistema = getattr(request, "system_message", None)
    if sistema is not None:
        mensajes.append({"rol": "system", "contenido": _contenido(sistema.content)})
    elif getattr(request, "system_prompt", None):
        mensajes.append({"rol": "system", "contenido": request.system_prompt})
    for m in request.messages or []:
        item = {"rol": m.type, "contenido": _contenido(m.content)}
        if getattr(m, "tool_calls", None):
            item["llamadas"] = [{"nombre": t.get("name"), "args": t.get("args")} for t in m.tool_calls]
        if getattr(m, "name", None) and m.type == "tool":
            item["herramienta"] = m.name
        mensajes.append(item)
    herramientas = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else str(t))
                    for t in (getattr(request, "tools", None) or [])]
    modelo = getattr(request.model, "model", None) or getattr(request.model, "model_name", None) or type(request.model).__name__
    return {"modelo": str(modelo), "mensajes": mensajes, "herramientas": herramientas}


def _guardar_payload(ctx: ContextoAgente, payload: dict) -> None:
    import json as _json                            # noqa: PLC0415
    from sqlalchemy import text as _text            # noqa: PLC0415
    from app.core.deps import SessionLocal          # noqa: PLC0415
    crudo = _json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    huella = hashlib.sha256(crudo.encode()).hexdigest()
    db = SessionLocal()
    try:
        pid = db.execute(_text("""INSERT INTO agente_payloads (usuario_id, conversacion, paciente_alias, modelo, huella, payload)
                                  VALUES (:u, :c, :a, :m, :h, CAST(:p AS jsonb)) RETURNING id"""),
                         {"u": ctx.usuario_id, "c": getattr(ctx, "hilo", None), "a": ctx.paciente_alias,
                          "m": payload["modelo"][:80], "h": huella, "p": crudo}).scalar()
        db.commit()
        ctx.payloads.append(pid)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo guardar el payload enviado al LLM")
        db.rollback()
        return
    finally:
        db.close()
    _auditar(ctx, "agente_llm", {"payload_id": pid, "huella": huella, "mensajes": len(payload["mensajes"]),
                                 "modelo": payload["modelo"]})


class Auditoria(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        _guardar_payload(request.runtime.context, serializar_llamada(request))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        await asyncio.to_thread(_guardar_payload, request.runtime.context, serializar_llamada(request))
        return await handler(request)

    def _detalle(self, request) -> dict:
        return {"herramienta": request.tool_call["name"], "args": request.tool_call.get("args", {})}

    @staticmethod
    def _incidentes(ctx, desde: int) -> dict | None:
        nuevos = ctx.guardia[desde:]
        return {"incidentes": [{"origen": i["origen"], "reglas": i["reglas"], "frases": i["frases"],
                                "extracto": i["extracto"]} for i in nuevos]} if nuevos else None

    def wrap_tool_call(self, request, handler):
        ctx = request.runtime.context
        _auditar(ctx, "agente_herramienta", self._detalle(request))
        antes = len(ctx.guardia)
        respuesta = handler(request)
        if (inc := self._incidentes(ctx, antes)):      # la guardia retiró algo: queda en log_auditoria
            _auditar(ctx, "agente_guardia", {**self._detalle(request), **inc}, resultado="denegado")
        return respuesta

    async def awrap_tool_call(self, request, handler):
        ctx = request.runtime.context
        await asyncio.to_thread(_auditar, ctx, "agente_herramienta", self._detalle(request))
        antes = len(ctx.guardia)
        respuesta = await handler(request)
        if (inc := self._incidentes(ctx, antes)):
            await asyncio.to_thread(_auditar, ctx, "agente_guardia", {**self._detalle(request), **inc}, "denegado")
        return respuesta


# ------------------------------------------------------------------ armar el agente
def construir_agente(checkpointer=None, store=None):
    c = get_settings()
    modelos = modelos_del_agente()
    return create_agent(
        model=crear_modelo(modelos[0]),
        tools=HERRAMIENTAS,
        context_schema=ContextoAgente,
        middleware=[
            prompt_lati,
            VentanaContexto(max_previos=8),
            ModelCallLimitMiddleware(run_limit=c.agente_max_llamadas, exit_behavior="end"),
            CadenaModelos(),
            LimiteProveedor(maximo=c.agente_max_simultaneas),
            HumanInTheLoopMiddleware(interrupt_on={
                n: {"allowed_decisions": ["approve", "reject"]} for n in REQUIEREN_APROBACION}),
            Auditoria(),
        ],
        checkpointer=checkpointer,
        store=store,
    )


# ------------------------------------------------------------------ el harness
OCUPADA = "Lati está ocupada atendiendo a otras personas. Intenta de nuevo en unos segundos."
SIN_CUOTA = ("En este momento no puedo responder: los modelos de lenguaje alcanzaron su límite de uso. "
             "Intenta de nuevo en un minuto.")
LENTA = "La respuesta tardó demasiado y la detuve. Intenta de nuevo, quizá con una pregunta más concreta."


class Harness:
    """
    Atiende muchos usuarios a la vez sobre UN agente:
      · un hilo de conversación por usuario, conversación y paciente (memoria aislada);
      · un candado por hilo (dos mensajes del mismo hilo no corren a la vez);
      · cola acotada: si hay demasiados turnos en curso, responde "ocupada" de inmediato en vez
        de dejar al usuario esperando sin saber qué pasa;
      · tiempo máximo por turno.
    """

    def __init__(self, agente, store=None, memoria: str = "memoria"):
        c = get_settings()
        self.agente = agente
        self.store = store
        self.memoria = memoria
        self.timeout_s = c.agente_timeout_s
        self.max_en_cola = c.agente_max_en_cola
        self._candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._en_curso = 0
        self._fuentes_pendientes: dict[str, list[dict]] = {}   # fuentes vistas antes de una pausa

    @staticmethod
    def hilo(ctx: ContextoAgente, conversacion: str) -> str:
        base = f"u{ctx.usuario_id}:{conversacion}"
        if ctx.paciente_id:   # cada paciente, su propio hilo: nada de un paciente se mezcla con otro
            base += ":" + hashlib.sha256(ctx.paciente_id.encode()).hexdigest()[:10]
        return base

    async def _preferencias(self, ctx: ContextoAgente) -> list[str]:
        if self.store is None:
            return []
        try:
            items = await self.store.asearch(espacio_memoria(ctx.usuario_id), limit=10)
            return [i.value.get("texto", "") for i in items if i.value.get("texto")]
        except Exception:  # noqa: BLE001
            log.warning("No se pudieron leer las preferencias", exc_info=True)
            return []

    async def pendiente(self, hilo: str) -> list[dict] | None:
        """Acciones esperando decisión en ese hilo, o None."""
        estado = await self.agente.aget_state({"configurable": {"thread_id": hilo}})
        if not estado.interrupts:
            return None
        return [{"herramienta": a["name"], "args": a["args"]}
                for a in estado.interrupts[0].value["action_requests"]]

    def _empaquetar(self, salida, ctx: ContextoAgente, hilo: str) -> dict:
        base = {"conversacion": hilo, "herramientas": list(ctx.herramientas), "generador":
                f"gemini:{ctx.modelo_usado}" if ctx.modelo_usado else "ninguno",
                "payload_ids": list(ctx.payloads), "fuentes": []}
        if ctx.guardia:     # el front puede avisar: "se retiraron instrucciones sospechosas de una nota"
            base["guardia"] = [{"origen": i["origen"], "reglas": i["reglas"], "frases": i["frases"]}
                               for i in ctx.guardia]
        if salida.interrupts:
            self._fuentes_pendientes[hilo] = list(ctx.fuentes)
            acciones = salida.interrupts[0].value["action_requests"]
            return {**base, "estado": "pendiente_aprobacion",
                    "acciones": [{"herramienta": a["name"], "args": a["args"]} for a in acciones]}
        self._fuentes_pendientes.pop(hilo, None)
        from app.services import rag   # noqa: PLC0415
        ultimo = salida.value["messages"][-1]
        texto = _texto(ultimo).strip() or "No tengo una respuesta para eso."
        if texto.startswith("Model call limits exceeded"):   # ModelCallLimitMiddleware, en inglés
            texto = ("Necesité demasiados pasos para responder y me detuve. "
                     "Intenta con una pregunta más concreta.")
        humanos = sum(1 for m in salida.value["messages"] if isinstance(m, HumanMessage))
        texto = rag.sin_saludo(texto, [{}] if humanos > 1 else None)
        usadas = rag.citas_en(texto, len(ctx.fuentes))
        citas = []
        for n in usadas:
            f = ctx.fuentes[n - 1]
            citas.append({"id": f"F{n}", "fragmento_id": f["id"], "documento_id": f["documento_id"],
                          "tipo": f["tipo"], "titulo": f["titulo"], "seccion": f["seccion"], "fuente": f["fuente"],
                          "paciente": rag.seudonimo(f["paciente_id"]) if f["paciente_id"] else None,
                          "extracto": (f["texto"] or "")[:300]})
        # R23: cada respuesta dice de qué documento y de qué fragmento salió.
        fuentes = [{"id": c["id"], "documento": c["titulo"] or c["documento_id"], "documento_id": c["documento_id"],
                    "seccion": c["seccion"], "fragmento": c["extracto"], "tipo": c["tipo"]} for c in citas]
        return {**base, "estado": "respondido", "respuesta": texto, "citas": citas, "fuentes": fuentes}

    async def _ejecutar(self, entrada, ctx: ContextoAgente, hilo: str) -> dict:
        if self._en_curso >= self.max_en_cola:
            return {"estado": "ocupado", "conversacion": hilo, "respuesta": OCUPADA}
        self._en_curso += 1
        try:
            ctx.hilo = hilo
            ctx.preferencias = await self._preferencias(ctx)
            config = {"configurable": {"thread_id": hilo}}
            async with self._candados[hilo]:
                try:
                    salida = await asyncio.wait_for(
                        self.agente.ainvoke(entrada, config, context=ctx, version="v2"), timeout=self.timeout_s)
                except asyncio.TimeoutError:
                    return {"estado": "tiempo_agotado", "conversacion": hilo, "respuesta": LENTA,
                            "herramientas": list(ctx.herramientas)}
                except SinModeloDisponible:
                    return {"estado": "sin_cuota", "conversacion": hilo, "respuesta": SIN_CUOTA,
                            "herramientas": list(ctx.herramientas)}
            return self._empaquetar(salida, ctx, hilo)
        finally:
            self._en_curso -= 1

    async def atender(self, ctx: ContextoAgente, mensaje: str, conversacion: str = "principal") -> dict:
        hilo = self.hilo(ctx, conversacion)
        acciones = await self.pendiente(hilo)
        if acciones:
            return {"estado": "pendiente_aprobacion", "conversacion": hilo, "acciones": acciones,
                    "respuesta": "Antes de seguir, aprueba o rechaza la acción pendiente."}
        return await self._ejecutar({"messages": [{"role": "user", "content": mensaje}]}, ctx, hilo)

    async def decidir(self, ctx: ContextoAgente, conversacion: str, decision: str,
                      motivo: str | None = None) -> dict:
        hilo = self.hilo(ctx, conversacion)
        if not await self.pendiente(hilo):
            return {"estado": "sin_pendientes", "conversacion": hilo}
        d: dict = {"type": decision}
        if decision == "reject":
            d["message"] = f"El clínico rechazó la acción. Motivo: {motivo or 'sin motivo'}. No la repitas."
        ctx.fuentes.extend(self._fuentes_pendientes.get(hilo, []))
        return await self._ejecutar(Command(resume={"decisions": [d]}), ctx, hilo)
