"""
Modelo de práctica (patrón del cuaderno de la semana 13): imita el FORMATO de un LLM con
herramientas, no su inteligencia. Sirve para probar el harness sin clave y sin gastar cuota.
Reglas de palabras clave → llamadas a herramientas → respuesta armada con los resultados.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool


class ModeloSimulado(BaseChatModel):
    herramientas: list = []
    latencia: float = 0.0          # segundos que "tarda en pensar" (pruebas de concurrencia)

    @property
    def _llm_type(self) -> str:
        return "simulado"

    def bind_tools(self, tools, **kwargs):
        nombres = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"herramientas": nombres})

    def _plan(self, texto: str) -> list:
        t, plan = texto.lower(), []

        def usar(nombre, args):
            if nombre in self.herramientas:
                plan.append((nombre, args))
        remitir = re.search(r"rem[ií]t", t)
        if remitir:
            usar("cargar_skill", {"nombre": "remision-especialista"})
            usar("listar_especialidades", {})
        if re.search(r"paciente|este caso|sus datos", t):
            usar("consultar_paciente", {})
        if re.search(r"turno|pendiente|alerta|mi trabajo|mis pacientes", t):
            usar("resumen_de_mi_trabajo", {})
        if re.search(r"cama|ocupaci", t):
            usar("ocupacion_hospital", {})
        if re.search(r"quir[oó]fano|cirug[ií]a|agenda", t):
            usar("agenda_quirurgica", {})
        if re.search(r"gu[ií]a|protocolo|qu[eé] hago|recomend|ayuno|asa|uci|sepsis|potasio|criterio", t):
            usar("buscar_guias", {"consulta": texto[:200]})
        if remitir:
            usar("proponer_remision", {"especialidad": "Medicina interna", "motivo": texto[:150],
                                       "tipo_solicitud": "valoracion", "prioridad": "prioritaria"})
        if re.search(r"recuerda que|prefiero", t):
            usar("guardar_preferencia", {"preferencia": texto[:160]})
        return plan

    def _responder(self, mensajes) -> AIMessage:
        humanos = [i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage)]
        if not humanos:
            return AIMessage(content="[simulado] No veo ninguna pregunta.")
        i = humanos[-1]
        pregunta = mensajes[i].content if isinstance(mensajes[i].content, str) else str(mensajes[i].content)
        resultados = [m for m in mensajes[i + 1:] if isinstance(m, ToolMessage)]
        plan = self._plan(pregunta)
        if len(resultados) < len(plan):
            nombre, args = plan[len(resultados)]
            return AIMessage(content="", tool_calls=[{"name": nombre, "args": args,
                                                      "id": "call_" + uuid.uuid4().hex[:8], "type": "tool_call"}])
        if not resultados:
            return AIMessage(content=f"[simulado] Recibí: «{pregunta[:90]}». No necesité herramientas.")
        citas = sorted(set(re.findall(r"\[F\d+\]", " ".join(str(m.content) for m in resultados))))
        resumen = "\n".join(f"- {m.name}: {str(m.content)[:160]}" for m in resultados)
        return AIMessage(content="[simulado] Con lo que devolvieron las herramientas:\n" + resumen
                         + (" " + citas[0] if citas else ""))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.latencia)
        return ChatResult(generations=[ChatGeneration(message=self._responder(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(self.latencia)
        return ChatResult(generations=[ChatGeneration(message=self._responder(messages))])
