"""
Un solo lugar para crear el modelo.

· Con GEMINI_API_KEY: ChatGoogleGenerativeAI (langchain-google-genai). No usamos el endpoint
  compatible con OpenAI para el agente: los modelos Gemini 3 devuelven "firmas de
  pensamiento" en las llamadas a herramientas que hay que reenviar en el turno siguiente, y
  el cliente nativo lo hace solo.
· Sin clave: ModeloSimulado (agente/simulado.py), para pruebas sin gastar cuota.

Los modelos que prueba el agente salen de AGENTE_MODELOS (por defecto, solo los lite: el
plan gratuito da 15 solicitudes por minuto y 500 al día por modelo, y un turno del agente
gasta de 2 a 4 llamadas). El harness los recorre en orden si uno se queda sin cuota.
"""
from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings


def modelos_del_agente() -> list[str]:
    c = get_settings()
    nombres = [m.strip() for m in str(c.agente_modelos).split(",") if m.strip()]
    return list(dict.fromkeys(nombres))          # sin repetidos, en orden


def hay_modelo_real() -> bool:
    return bool(get_settings().gemini_api_key)


@lru_cache(maxsize=16)
def crear_modelo(nombre: str):
    c = get_settings()
    if not c.gemini_api_key:
        from app.agente.simulado import ModeloSimulado   # noqa: PLC0415
        return ModeloSimulado()
    from langchain_google_genai import ChatGoogleGenerativeAI   # noqa: PLC0415
    return ChatGoogleGenerativeAI(
        model=nombre,
        google_api_key=c.gemini_api_key,
        max_retries=0,            # un 429 se resuelve pasando al siguiente modelo, no reintentando
        timeout=c.agente_timeout_modelo_s,
        max_output_tokens=1024,
    )
