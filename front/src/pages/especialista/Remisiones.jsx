import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Building2, Check, CheckCheck, Clock, Inbox, Loader2, RotateCw, Send, UserCheck, X } from "lucide-react";
import Encabezado from "../../components/Encabezado";
import Insignia from "../../components/Insignia";
import Esqueleto from "../../components/Esqueleto";
import { useAvisos } from "../../components/Avisos";
import * as S from "../../lib/servicios";
import { useEventos } from "../../lib/tiempoReal";
import { useUsuario } from "../../lib/hooks";
import { esEspecialista } from "../../lib/roles";
import { haceCuanto } from "../../lib/format";

/**
 * Remisiones (R08): el especialista ve las recibidas y las acepta (no puede
 * rechazar las internas: si el médico la pidió es porque el paciente la necesita); el
 * médico ve las que envió y a quién se las asignó el reparto de carga.
 * Proyecto final: también las de OTRAS instituciones. Una entrante sí se puede
 * rechazar con motivo (la otra institución recibe el Task); una saliente muestra si
 * se entregó y se puede reenviar si el socio no respondió.
 */
export default function Remisiones() {
  const usuario = useUsuario();
  const { avisar } = useAvisos();
  const recibe = esEspecialista(usuario);
  const [estado, setEstado] = useState({ cargando: true, lista: [], demo: false, error: null });
  const [nuevas, setNuevas] = useState(new Set());
  const [aceptando, setAceptando] = useState(null);

  const vistas = useRef(null);   // ids ya mostrados: lo que no esté aquí es nuevo

  const cargar = useCallback(async () => {
    try {
      const { datos, demo } = await S.remisiones();
      if (vistas.current) setNuevas(new Set(datos.filter((r) => !vistas.current.has(r.id)).map((r) => r.id)));
      vistas.current = new Set(datos.map((r) => r.id));
      setEstado({ cargando: false, lista: datos, demo, error: null });
    } catch (err) {
      setEstado((e) => ({ ...e, cargando: false, error: err }));
    }
  }, []);

  useEffect(() => { cargar(); }, [cargar]);
  useEventos((ev) => { if (ev.tipo?.startsWith("remision")) cargar(); });

  const aceptar = async (r) => {
    setAceptando(r.id);
    try {
      await S.aceptarRemision(r.id);
      avisar(r.direccion === "entrante"
        ? `Remisión aceptada. ${r.institucion || "La otra institución"} ya fue avisada.`
        : "Remisión aceptada. El médico remitente ya fue avisado.");
      await cargar();
    } catch (err) { avisar(err.message, "error"); }
    finally { setAceptando(null); }
  };

  const accion = async (r, fn, mensaje) => {
    setAceptando(r.id);
    try {
      await fn();
      avisar(mensaje);
      await cargar();
    } catch (err) { avisar(err.message, "error"); await cargar(); }
    finally { setAceptando(null); }
  };
  const atender = (r) => accion(r, () => S.atenderRemision(r.id),
    "Remisión cerrada como atendida. Conservas el acceso a la ficha unos días más.");
  const simulada = (r) => String(r.id).startsWith("sim-");
  // Proyecto final: remisiones con otras instituciones.
  const [rechazando, setRechazando] = useState(null);     // id de la remisión externa que se está rechazando
  const [motivoRechazo, setMotivoRechazo] = useState("");
  const rechazar = (r) => accion(r, () => S.rechazarRemision(r.id, motivoRechazo.trim()),
    "Remisión rechazada. La otra institución recibe la respuesta con el motivo.").then(() => { setRechazando(null); setMotivoRechazo(""); });
  const reenviar = (r) => accion(r, () => S.reenviarRemision(r.id), "Remisión reenviada a la otra institución.");
  const origen = (r) => (r.direccion === "entrante" ? `De ${r.institucion || "otra institución"}`
    : r.direccion === "saliente" ? `Para ${r.institucion || "otra institución"}` : null);

  const pendientes = estado.lista.filter((r) => r.estado === "pendiente");
  const resueltas = estado.lista.filter((r) => r.estado !== "pendiente");

  return (
    <div className="space-y-8 pb-16">
      <Encabezado titulo={recibe ? "Remisiones recibidas" : "Remisiones"}
                  subtitulo={recibe ? "Pacientes que otros médicos te remitieron para valoración." : "Pacientes que remitiste a un especialista. Para remitir, abre la ficha del paciente."}
                  demo={estado.demo} />

      {estado.cargando && <Esqueleto filas={3} alto="h-20" />}
      {estado.error && <p role="alert" className="rounded-3xl bg-coral-soft p-5 text-sm text-coral-ink">{estado.error.message}</p>}

      {!estado.cargando && !estado.error && estado.lista.length === 0 && (
        <div className="flex flex-col items-center gap-2 rounded-[28px] bg-cloud-card py-14 text-center">
          <Inbox size={28} className="text-gray-strong" aria-hidden="true" />
          <p className="font-medium">No hay remisiones</p>
          <p className="text-sm text-gray-strong">Cuando llegue una, aparecerá aquí sin recargar.</p>
        </div>
      )}

      {pendientes.length > 0 && (
        <section aria-label="Pendientes" className="grid gap-4 md:grid-cols-2">
          {pendientes.map((r) => (
            <article key={r.id} className={`flex flex-col rounded-[28px] bg-cloud-card p-6 ${nuevas.has(r.id) ? "anim-pop ring-2 ring-metric-blue" : ""}`}>
              <div className="mb-3 flex items-center justify-between gap-3 text-xs">
                <span className="flex items-center gap-1.5 font-semibold text-metric-blue">
                  {origen(r) ? <><Building2 size={14} aria-hidden="true" />{origen(r)}</>
                    : recibe ? <><UserCheck size={14} aria-hidden="true" />{r.medico}</> : <><Send size={14} aria-hidden="true" />Para {r.especialista}</>}
                </span>
                <span className="flex items-center gap-1 text-gray-strong"><Clock size={12} aria-hidden="true" />{haceCuanto(r.created_at)}</span>
              </div>
              <h2 className="flex flex-wrap items-center gap-2 text-lg font-semibold leading-snug">
                <Link to={`/app/pacientes/${r.paciente_id}`} className="hover:underline">{r.paciente_nombre || "Paciente"}</Link>
                {r.prioridad === "urgente" && <span className="rounded-full bg-coral-deep px-2 py-0.5 text-[11px] font-semibold text-white">Urgente</span>}
              </h2>
              {!recibe && r.especialidad && <p className="text-xs text-gray-strong">{r.especialidad}</p>}
              {r.tipo_solicitud_texto && <p className="mt-1 text-xs font-semibold uppercase tracking-wide text-metric-blue">{r.tipo_solicitud_texto}</p>}
              <p className="mt-1 flex-1 text-sm text-gray-strong">{r.motivo}</p>
              {r.reporte_externo?.conclusion && <p className="mt-2 rounded-2xl bg-paper-white px-3 py-2 text-xs text-gray-strong">Reporte de IA aprobado del otro hospital: {r.reporte_externo.conclusion}</p>}
              {r.direccion === "saliente" && (
                <p className={`mt-2 text-xs ${r.estado_envio === "fallida" ? "text-coral-ink" : "text-gray-strong"}`}>
                  {r.estado_envio === "enviada" ? "Entregada a la otra institución; esperando su respuesta." : r.estado_envio === "fallida" ? `No se pudo entregar: ${r.detalle_envio || ""}` : "Enviando…"}
                </p>
              )}
              {rechazando === r.id && (
                <label className="mt-3 block text-xs font-medium">Motivo del rechazo (lo recibe la otra institución)
                  <textarea rows={2} minLength={5} value={motivoRechazo} onChange={(e) => setMotivoRechazo(e.target.value)}
                            className="mt-1 w-full rounded-2xl bg-paper-white px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-coral-deep/40" />
                </label>
              )}
              <div className="mt-5 flex items-center justify-between gap-2">
                <Insignia grupo="proceso" etiqueta="Pendiente" tamano="sm" />
                {recibe && <Link to={`/app/pacientes/${r.paciente_id}`} className="ml-auto rounded-full px-3 py-2 text-sm font-medium text-metric-blue hover:bg-paper-white">Revisar ficha</Link>}
                {r.direccion === "saliente" && r.estado_envio === "fallida" && (
                  <button onClick={() => reenviar(r)} disabled={aceptando === r.id}
                          className="ml-auto flex items-center gap-1.5 rounded-full bg-charcoal px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
                    <RotateCw size={15} aria-hidden="true" />Reenviar
                  </button>
                )}
                {recibe && r.puede_rechazar && (rechazando === r.id
                  ? <button onClick={() => rechazar(r)} disabled={aceptando === r.id || motivoRechazo.trim().length < 5}
                            className="flex items-center gap-1.5 rounded-full bg-coral-deep px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
                      <X size={15} aria-hidden="true" />Confirmar rechazo
                    </button>
                  : <button onClick={() => { setRechazando(r.id); setMotivoRechazo(""); }}
                            className="rounded-full px-3 py-2 text-sm font-medium text-coral-ink hover:bg-paper-white">Rechazar</button>)}
                {recibe && r.direccion !== "saliente" && (
                  <button onClick={() => aceptar(r)} disabled={aceptando === r.id}
                          className="flex items-center gap-1.5 rounded-full bg-charcoal px-4 py-2 text-sm font-semibold text-white transition hover:bg-ink disabled:opacity-50">
                    {aceptando === r.id ? <Loader2 size={15} className="anim-spin" /> : <Check size={15} />}Aceptar
                  </button>
                )}
              </div>
            </article>
          ))}
        </section>
      )}

      {resueltas.length > 0 && (
        <section aria-label="Atendidas" className="rounded-[28px] bg-paper-white p-6 shadow-lift">
          <h2 className="mb-2 text-lg font-semibold">Resueltas</h2>
          <ul className="divide-y divide-cloud-card">
            {resueltas.map((r) => (
              <li key={r.id} className="flex items-center justify-between gap-3 py-3 text-sm">
                <span>
                  <Link to={`/app/pacientes/${r.paciente_id}`} className="font-medium hover:underline">{r.paciente_nombre || "Paciente"}</Link>
                  <span className="block text-xs text-gray-strong">{origen(r) || (recibe ? `De ${r.medico}` : `Para ${r.especialista}`)} · {r.motivo} · {haceCuanto(r.created_at)}</span>
                  {r.estado === "rechazada" && r.respuesta && <span className="block text-xs text-coral-ink">Motivo del rechazo: {r.respuesta}</span>}
                </span>
                {recibe && r.estado === "aceptada" && !simulada(r) && (
                  <button onClick={() => atender(r)} disabled={aceptando === r.id}
                          className="ml-auto flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold text-metric-blue hover:bg-cloud-card disabled:opacity-50">
                    {aceptando === r.id ? <Loader2 size={13} className="anim-spin" /> : <CheckCheck size={13} />}Marcar atendida
                  </button>
                )}
                <Insignia grupo={r.estado === "rechazada" ? "bloqueado" : "libre"} etiqueta={r.estado === "rechazada" ? "Rechazada" : r.estado === "atendida" ? "Atendida" : "Aceptada"} tamano="sm" />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
