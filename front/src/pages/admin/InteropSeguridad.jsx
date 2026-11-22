import { useCallback, useEffect, useState } from "react";
import { Building2, Loader2, Save, ShieldAlert, ShieldCheck, Unlock } from "lucide-react";
import { useAvisos } from "../../components/Avisos";
import * as S from "../../lib/servicios";
import { useEventos } from "../../lib/tiempoReal";
import { fechaYHora } from "../../lib/format";

const tarjeta = "rounded-[28px] bg-paper-white p-6 shadow-lift";
const entrada = "w-full rounded-2xl bg-cloud-card px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-metric-blue/40";

/**
 * Instituciones socias (proyecto final, R30-R33). Se ve cómo llamamos a cada una y se cambia su URL
 * sin redesplegar: el túnel de Cloudflare del otro equipo cambia cada vez que lo reinician.
 * Las credenciales no se muestran nunca; se cambian con scripts/interop_cliente.py.
 */
export function Socios() {
  const { avisar } = useAvisos();
  const [lista, setLista] = useState(null);
  const [urls, setUrls] = useState({});
  const [guardando, setGuardando] = useState(null);
  const [error, setError] = useState(null);
  const cargar = useCallback(() => S.sociosAdmin().then((l) => { setLista(l); setUrls(Object.fromEntries(l.map((s) => [s.codigo, s.base_url]))); })
    .catch((e) => setError(e.message)), []);
  useEffect(() => { cargar(); }, [cargar]);

  const guardar = async (s) => {
    setGuardando(s.codigo);
    try { await S.cambiarSocio(s.codigo, { base_url: urls[s.codigo].trim() }); avisar(`URL de ${s.nombre} actualizada.`); await cargar(); }
    catch (e) { avisar(e.message, "error"); }
    finally { setGuardando(null); }
  };
  const alternar = async (s) => {
    try { await S.cambiarSocio(s.codigo, { activo: !s.activo }); await cargar(); } catch (e) { avisar(e.message, "error"); }
  };

  return (
    <section className={tarjeta} aria-label="Instituciones socias">
      <h2 className="flex items-center gap-2 text-lg font-semibold"><Building2 size={18} aria-hidden="true" />Instituciones socias</h2>
      <p className="mt-1 text-sm text-gray-strong">A quién se puede remitir y cómo nos autenticamos con cada una. Otro equipo se conecta a Pulso con lo que publica <code>/api/interop/metadata</code>.</p>
      {error && <p role="alert" className="mt-3 text-sm text-coral-ink">{error}</p>}
      {lista && lista.length === 0 && <p className="mt-4 text-sm text-gray-strong">Aún no hay socios. Se registran con <code>python scripts/interop_cliente.py socio-crear</code>.</p>}
      <ul className="mt-4 space-y-3">
        {(lista || []).map((s) => (
          <li key={s.codigo} className="rounded-3xl bg-cloud-card p-4 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold">{s.nombre} <span className="font-normal text-gray-strong">· {s.codigo}</span></span>
              <button onClick={() => alternar(s)} className={`rounded-full px-3 py-1 text-xs font-semibold ${s.activo ? "bg-green-soft text-green-ink" : "bg-paper-white text-gray-strong"}`}>
                {s.activo ? "Activo" : "Inactivo"}
              </button>
            </div>
            <p className="mt-1 text-xs text-gray-strong">Autenticación: {s.modo_auth}{s.firma ? " + firma HMAC" : ""} · Bundle {s.ruta_bundle} · Task {s.ruta_task} · entra a Pulso con {s.client_id || "—"}</p>
            <div className="mt-2 flex gap-2">
              <input aria-label={`URL de ${s.nombre}`} value={urls[s.codigo] || ""} onChange={(e) => setUrls((u) => ({ ...u, [s.codigo]: e.target.value }))} className={entrada} />
              <button onClick={() => guardar(s)} disabled={guardando === s.codigo || urls[s.codigo] === s.base_url}
                      className="flex shrink-0 items-center gap-1.5 rounded-full bg-charcoal px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">
                {guardando === s.codigo ? <Loader2 size={13} className="anim-spin" /> : <Save size={13} />}Guardar
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Intentos de manipulación del agente o del asistente y bloqueos temporales (R25). */
export function Seguridad() {
  const { avisar } = useAvisos();
  const [datos, setDatos] = useState({ incidentes: [], bloqueos: [], cargando: true });
  const cargar = useCallback(async () => {
    const [i, b] = await Promise.allSettled([S.incidentesSeguridad(), S.bloqueosTemporales()]);
    setDatos({ incidentes: i.value || [], bloqueos: b.value || [], cargando: false });
  }, []);
  useEffect(() => { cargar(); }, [cargar]);
  useEventos((ev) => { if (ev.tipo === "inyeccion_detectada") cargar(); });
  const levantar = async (b) => {
    try { await S.levantarBloqueo(b.id); avisar(`Bloqueo de ${b.username || b.ip} levantado.`); await cargar(); }
    catch (e) { avisar(e.message, "error"); }
  };
  const vigentes = datos.bloqueos.filter((b) => b.vigente);
  return (
    <section className={tarjeta} aria-label="Seguridad del asistente">
      <h2 className="flex items-center gap-2 text-lg font-semibold"><ShieldAlert size={18} aria-hidden="true" />Intentos de manipulación</h2>
      <p className="mt-1 text-sm text-gray-strong">Preguntas que intentaron cambiar las reglas del asistente. No llegaron al modelo. Tres intentos en diez minutos bloquean al usuario por quince.</p>
      {vigentes.length > 0 && (
        <ul className="mt-4 space-y-2">
          {vigentes.map((b) => (
            <li key={b.id} className="flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-coral-soft px-4 py-2 text-sm text-coral-ink">
              <span>Bloqueado: <b>{b.username || "sin usuario"}</b> · IP {b.ip || "—"} · hasta {fechaYHora(b.hasta)}</span>
              <button onClick={() => levantar(b)} className="flex items-center gap-1 rounded-full bg-paper-white px-3 py-1 text-xs font-semibold"><Unlock size={13} aria-hidden="true" />Levantar</button>
            </li>
          ))}
        </ul>
      )}
      {!datos.cargando && datos.incidentes.length === 0 && (
        <p className="mt-4 flex items-center gap-2 text-sm text-green-ink"><ShieldCheck size={16} aria-hidden="true" />Sin intentos registrados.</p>
      )}
      {datos.incidentes.length > 0 && (
        <table className="mt-4 w-full text-left text-sm">
          <thead className="text-xs text-gray-strong"><tr><th className="py-1">Cuándo</th><th>Usuario</th><th>IP</th><th>Canal</th><th>Reglas</th></tr></thead>
          <tbody>
            {datos.incidentes.map((i) => (
              <tr key={i.id} className="border-t border-cloud-card">
                <td className="py-1.5">{fechaYHora(i.created_at)}</td><td>{i.username || "—"}</td><td>{i.ip || "—"}</td>
                <td>{i.canal === "agente" ? "Agente" : "Asistente"}</td><td className="text-xs">{(i.reglas || []).join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
