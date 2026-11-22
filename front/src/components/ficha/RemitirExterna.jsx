import { useEffect, useState } from "react";
import { Building2, CheckCircle2, Loader2, Send, ShieldCheck } from "lucide-react";
import { useAvisos } from "../Avisos";
import * as S from "../../lib/servicios";

const VACIO = { socio: "", motivo: "", prioridad: "prioritaria", especialidad_id: "", reporte_id: "" };

/**
 * Remitir a OTRA institución (proyecto final, R30-R32). Antes de remitir hace falta el
 * consentimiento del paciente para ESA institución (Ley 1581/2012). Sale un Bundle FHIR con lo
 * mínimo (sin teléfono, correo ni dirección) y, si se elige, el reporte de IA APROBADO.
 * La respuesta del otro hospital llega sola a la bandeja.
 */
export default function RemitirExterna({ pacienteId, especialidades = [] }) {
  const { avisar } = useAvisos();
  const [socios, setSocios] = useState([]);
  const [reportes, setReportes] = useState([]);
  const [datos, setDatos] = useState(VACIO);
  const [enviando, setEnviando] = useState(false);
  const [registrando, setRegistrando] = useState(null);
  const [resultado, setResultado] = useState(null);
  const [error, setError] = useState(null);

  const cargar = () => S.sociosParaRemitir(pacienteId).then(setSocios).catch(() => setSocios([]));
  useEffect(() => { cargar(); }, [pacienteId]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    S.reportesDePaciente(pacienteId).then((r) => setReportes((r.datos || r || []).filter((x) => x.estado === "aprobado")))
      .catch(() => setReportes([]));
  }, [pacienteId]);

  if (socios.length === 0) return null;      // sin instituciones socias configuradas no se muestra
  const elegido = socios.find((s) => String(s.codigo) === datos.socio);

  const consentir = async (s) => {
    setRegistrando(s.codigo); setError(null);
    try {
      await S.registrarConsentimiento(pacienteId, s.codigo);
      avisar(`Consentimiento registrado para ${s.nombre}.`);
      await cargar();
    } catch (err) { setError(err.message); }
    finally { setRegistrando(null); }
  };

  const enviar = async (e) => {
    e.preventDefault();
    setEnviando(true); setError(null); setResultado(null);
    try {
      const r = await S.remitirExterna({
        paciente_id: pacienteId, socio: datos.socio, motivo: datos.motivo.trim(), prioridad: datos.prioridad,
        especialidad_id: datos.especialidad_id ? Number(datos.especialidad_id) : null,
        reporte_id: datos.reporte_id ? Number(datos.reporte_id) : null,
      });
      setResultado(r);
      if (r.estado_envio === "enviada") { avisar(`Remisión enviada a ${r.socio}.`); setDatos(VACIO); }
      else avisar(`La remisión quedó guardada, pero ${r.socio} no respondió. Puedes reenviarla desde Remisiones.`, "alerta");
    } catch (err) { setError(err.message); }
    finally { setEnviando(false); }
  };

  const entrada = "mt-1 w-full rounded-2xl bg-cloud-card px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-metric-blue/40";
  return (
    <form onSubmit={enviar} className="space-y-4 rounded-[28px] bg-paper-white p-6 shadow-lift">
      <div>
        <h2 className="flex items-center gap-2 text-lg font-semibold"><Building2 size={18} aria-hidden="true" />Remitir a otra institución</h2>
        <p className="mt-1 text-sm text-gray-strong">Viaja en FHIR solo lo necesario para la atención: datos básicos del paciente, el motivo, sus últimos resultados y, si lo eliges, el reporte de IA aprobado. Nunca teléfono, correo ni dirección.</p>
      </div>
      <ul className="space-y-2 text-sm">
        {socios.map((s) => (
          <li key={s.codigo} className="flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-cloud-card px-4 py-2">
            <span className="font-medium">{s.nombre}</span>
            {s.consentimiento
              ? <span className="flex items-center gap-1 text-xs font-semibold text-green-ink"><ShieldCheck size={14} aria-hidden="true" />Consentimiento vigente</span>
              : <button type="button" onClick={() => consentir(s)} disabled={registrando === s.codigo}
                        className="rounded-full bg-paper-white px-3 py-1 text-xs font-semibold text-metric-blue hover:bg-hero-sky disabled:opacity-50">
                  {registrando === s.codigo ? "Registrando…" : "Registrar consentimiento del paciente"}
                </button>}
          </li>
        ))}
      </ul>
      {resultado && (
        <p className={`flex items-center gap-2 rounded-3xl p-4 text-sm ${resultado.estado_envio === "enviada" ? "bg-green-soft text-green-ink" : "bg-coral-soft text-coral-ink"}`}>
          <CheckCircle2 size={17} aria-hidden="true" />Remisión {resultado.id} a {resultado.socio}: {resultado.estado_envio === "enviada" ? "recibida por la otra institución" : `no se pudo entregar (${resultado.detalle_envio})`}.
        </p>
      )}
      <label className="block text-sm font-medium">Institución
        <select required value={datos.socio} onChange={(e) => setDatos((d) => ({ ...d, socio: e.target.value }))} className={entrada}>
          <option value="" disabled>Elige la institución</option>
          {socios.map((s) => <option key={s.codigo} value={s.codigo} disabled={!s.consentimiento}>{s.nombre}{s.consentimiento ? "" : " (falta consentimiento)"}</option>)}
        </select>
      </label>
      <label className="block text-sm font-medium">Especialidad que se pide (opcional)
        <select value={datos.especialidad_id} onChange={(e) => setDatos((d) => ({ ...d, especialidad_id: e.target.value }))} className={entrada}>
          <option value="">La que defina la otra institución</option>
          {especialidades.map((e) => <option key={e.id} value={e.id}>{e.nombre}</option>)}
        </select>
      </label>
      {reportes.length > 0 && (
        <label className="block text-sm font-medium">Adjuntar reporte de IA aprobado (opcional)
          <select value={datos.reporte_id} onChange={(e) => setDatos((d) => ({ ...d, reporte_id: e.target.value }))} className={entrada}>
            <option value="">Sin reporte</option>
            {reportes.map((r) => <option key={r.reporte_id ?? r.id} value={r.reporte_id ?? r.id}>Reporte {r.reporte_id ?? r.id}{r.probabilidad != null ? ` · ${Math.round(r.probabilidad * 100)} %` : ""}</option>)}
          </select>
        </label>
      )}
      <fieldset className="text-sm font-medium">
        <legend>Prioridad</legend>
        <div className="mt-1 flex flex-wrap gap-2">
          {[["rutinaria", "Rutinaria"], ["prioritaria", "Prioritaria"], ["urgente", "Urgente"]].map(([v, t]) => (
            <label key={v} className={`cursor-pointer rounded-full px-4 py-2 ${datos.prioridad === v ? (v === "urgente" ? "bg-coral-deep text-white" : "bg-charcoal text-white") : "bg-cloud-card"}`}>
              <input type="radio" name="prioridad_externa" value={v} checked={datos.prioridad === v} onChange={() => setDatos((d) => ({ ...d, prioridad: v }))} className="sr-only" />{t}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="block text-sm font-medium">Motivo clínico
        <textarea required minLength={5} rows={3} value={datos.motivo} onChange={(e) => setDatos((d) => ({ ...d, motivo: e.target.value }))}
                  placeholder="Ej.: hiperpotasemia persistente, se solicita valoración por nefrología." className={`${entrada} resize-y`} />
      </label>
      {error && <p role="alert" className="text-sm text-coral-ink">{error}</p>}
      <button type="submit" disabled={enviando || !elegido?.consentimiento}
              className="flex items-center gap-2 rounded-full bg-charcoal px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-ink disabled:opacity-50">
        {enviando ? <Loader2 size={15} className="anim-spin" /> : <Send size={15} />}Remitir a la otra institución
      </button>
    </form>
  );
}
