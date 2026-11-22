# Datasets del proyecto (R06, R07, R23)

Pulso trabaja sobre un caso hipotético (Hospital San Rafael), pero dos de sus
componentes usan **datos reales de acceso abierto**, para que no todo sea
inventado: las radiografías de tórax (imagen) y los casos quirúrgicos de
VitalDB (tabular). Todo lo demás (pacientes, camas, cirugías, tarifas) es
sintético y está declarado como tal.

> Las cantidades corresponden a lo cargado el 18 de octubre de 2026 en el
> equipo que hace la demostración. Si se vuelve a cargar, se actualizan con
> los comandos que aparecen en cada sección.

---

## Imagen

### Ficha

| | |
|---|---|
| **Nombre** | NIH ChestX-ray14 (también llamado ChestX-ray8 en su artículo original) |
| **Proveedor** | NIH Clinical Center (Estados Unidos) |
| **URL oficial** | https://nihcc.app.box.com/v/ChestXray-NIHCC |
| **Contenido completo** | 112.120 radiografías de tórax en proyección frontal, de 30.805 pacientes distintos, con 14 hallazgos etiquetados (atelectasia, cardiomegalia, derrame pleural, infiltrado, masa, nódulo, neumonía, neumotórax, consolidación, edema, enfisema, fibrosis, engrosamiento pleural y hernia) más la categoría "sin hallazgo" |
| **Cómo se etiquetó** | Las etiquetas se extrajeron automáticamente de los informes radiológicos (minería de texto); no las revisó un radiólogo imagen por imagen. Los autores estiman una exactitud de etiquetas superior al 90 % |
| **Licencia / condiciones de uso** | El NIH publica el conjunto sin restricciones de uso. Pide tres cosas: enlazar a su sitio de descarga, citar el artículo de abajo y reconocer al NIH Clinical Center como proveedor. Los informes radiológicos originales no son públicos |
| **Cita** | Wang X., Peng Y., Lu L., Lu Z., Bagheri M., Summers R. *ChestX-ray8: Hospital-scale chest X-ray database and benchmarks on weakly-supervised classification and localization of common thorax diseases.* CVPR 2017. DOI 10.1109/CVPR.2017.369 |

### Qué se cargó en Pulso

- **Cantidad cargada: 24 imágenes** (el valor por defecto del script
  son 24, de una muestra pública de unos 10 MB).
- Origen de la muestra: el repositorio `jrzech/reproduce-chexnet`, que
  redistribuye imágenes del dataset con sus etiquetas. Con `--carpeta` y
  `--etiquetas` se puede cargar un volumen mayor desde la descarga oficial
  (`Data_Entry_2017.csv` y las carpetas `images_00X`).
- Cada imagen se sube al PACS (Orthanc) como un estudio DICOM de modalidad DX,
  asignada a un paciente sintético del mismo sexo y, en lo posible, de la misma
  década de edad. La descripción del estudio lleva el hallazgo del dataset.
  El `PatientID` de DICOM es el índice ciego del paciente, nunca la cédula.
- Es seguro repetirlo: si el paciente ya tiene esa radiografía, se la salta.

Comandos:

```bash
# Automático: el contenedor `jornada` carga la muestra pública (24 imágenes)
# la primera vez que se levanta el sistema (CARGAR_RADIOGRAFIAS=false lo omite).
# Para repetirlo a mano:
docker compose run --rm jornada

# Solo las radiografías, con otra cantidad (desde Docker)
docker compose run --rm --entrypoint python jornada scripts/cargar_radiografias.py --cantidad 60

# Más volumen, desde la descarga oficial (en el equipo, con el .env apuntando a localhost)
python scripts/cargar_radiografias.py --cantidad 200 \
  --carpeta D:/nih/images --etiquetas D:/nih/Data_Entry_2017.csv

# Ver la asignación sin subir nada
python scripts/cargar_radiografias.py --simular
```

Para saber cuántas quedaron en el PACS (la clave es `ORTHANC_PASSWORD` del `.env`):

```powershell
curl.exe -s -u api:TU_CLAVE http://127.0.0.1:8042/statistics
```

El campo `CountStudies` es la cantidad de estudios y `CountInstances` la de
imágenes: es el número que se reporta arriba.

### Por qué responde al problema

El problema de partida es la cancelación de cirugías por falta de
coordinación. La radiografía de tórax preoperatoria forma parte de la
valoración de riesgo quirúrgico y anestésico, así que aparece en el recorrido
del paciente antes de entrar a quirófano: la ficha del paciente muestra su
radiografía y solo la ve quien puede ver a ese paciente.

### Limitaciones (declaradas)

- **No es un problema de capacidad quirúrgica.** ChestX-ray14 sirve para
  probar el flujo de imágenes (PACS, permisos, visor), no para predecir
  cancelaciones.
- Las radiografías son de una cohorte externa (NIH), asignadas a pacientes
  sintéticos. En pantalla se rotulan como **"imagen ilustrativa, cohorte
  externa NIH"**, nunca como "examen del hospital".
- Las etiquetas salen de minería de texto y no están validadas por
  radiólogos: no se usan para diagnosticar.

---

## Tabular

### Ficha

| | |
|---|---|
| **Nombre** | VitalDB: base de signos vitales de alta fidelidad en pacientes quirúrgicos (v1.0.0, PhysioNet) |
| **Proveedor** | Seoul National University Hospital (Corea del Sur). Autores: Hyung-Chul Lee y Chul-Woo Jung |
| **URL** | https://physionet.org/content/vitaldb/1.0.0/ · portal del proyecto: https://vitaldb.net |
| **DOI** | 10.13026/czw8-9p62 |
| **Contenido** | 6.388 casos quirúrgicos (de 7.051 elegibles, tras excluir los que no cumplían los criterios) con datos intraoperatorios, clínicos y de laboratorio |
| **Archivo que usamos** | `clinical_data.csv` (≈ 2,1 MB, una fila por caso, 71 parámetros) |
| **Licencia** | Creative Commons Attribution 4.0 (CC BY 4.0), acceso abierto en PhysioNet. Exige atribución |
| **Cita** | Lee H., Jung C. (2022). *VitalDB, a high-fidelity multi-parameter vital signs database in surgical patients* (v1.0.0). PhysioNet. https://doi.org/10.13026/czw8-9p62 · Artículo: Lee H.C. et al., *Scientific Data* 9, 279 (2022). https://doi.org/10.1038/s41597-022-01411-5 |

Variables del archivo que importan aquí: demografía (`age`, `sex`, `height`,
`weight`, `bmi`), perfil quirúrgico (`asa`, `emop`, `department`, `optype`,
`approach`, `ane_type`), comorbilidades (`preop_htn`, `preop_dm`,
`preop_arrhythmia`, `preop_pft`), laboratorio preoperatorio (`preop_hb`,
`preop_cr`, `preop_alb`, `preop_gluc`, entre otros) y resultados (`opdur`,
`los_icu`, `los_postop`, `death_inhosp`).

### Qué se cargó en Pulso

| Conjunto | Filas |
|---|---|
| Casos reales de VitalDB usados | **6.199** adultos (de 6.388 casos del archivo) |
| Pacientes sintéticos generados por clusters (`origen = sintetico`) | **4.958** |
| Número de clusters elegido | **4**, con estabilidad ARI 0,91 (justificado en `docs/INFORME_CLUSTERS.md`) |

Esos tres números los imprime `scripts/clusters_vitaldb.py` al terminar, y los
sirve el endpoint `GET /analitica/clusters` (campos `n_reales` y
`n_sinteticos` por cluster).

Cómo se construye (detalle y métricas en `docs/INFORME_CLUSTERS.md`):

1. Se descarga `clinical_data.csv` de PhysioNet.
2. Se agrupan los casos por **variables preoperatorias** (edad, IMC, ASA,
   urgencia, servicio, comorbilidades y laboratorio previo). Los resultados
   posteriores a la cirugía **no** entran al agrupamiento.
3. El número de grupos se elige con el método del codo y el coeficiente de
   silueta, y se interpreta clínicamente cada grupo.
4. Dentro de cada cluster se generan casos sintéticos que respetan la
   distribución del grupo. Cada fila queda marcada con `origen` (`real` o
   `sintetico`) y con su `cluster`.

```bash
pip install -r scripts/requirements-datos.txt
python scripts/clusters_vitaldb.py --modo proporcional   # descarga, agrupa, genera sintéticos en cada grupo
python scripts/cargar_clusters.py             # los sube a MongoDB (para el endpoint)
```

### Por qué responde al problema

La cancelación evitable que levantó el trabajo de campo ocurre, entre otras
razones, porque la **cama de UCI posoperatoria no está lista**. VitalDB es una
base real de pacientes quirúrgicos con las variables que un equipo conoce
**antes** de operar (edad, ASA, tipo de cirugía, laboratorio) y con el desenlace
posterior (`los_icu`, días en UCI). Eso permite entrenar y evaluar un modelo
de necesidad de UCI posquirúrgica (R10, a cargo de IA) con datos reales, y el
aumento por clusters da volumen y cubre los grupos pequeños sin inventar
casos fuera de lo clínicamente plausible.

### Limitaciones (declaradas)

- Los pacientes son de un hospital coreano; no representan a una población
  colombiana ni al caso hipotético de 300 camas.
- Los casos sintéticos **no son evidencia clínica**: sirven para probar el
  pipeline y para balancear grupos pequeños. Toda métrica de un modelo debe
  calcularse **solo con casos reales** y separando entrenamiento y prueba por
  paciente **antes** de generar sintéticos, para que no haya fuga de información.
- Un cluster es una agrupación estadística, no un diagnóstico: cada grupo se
  interpreta clínicamente en el informe, y esa lectura debe validarla un
  profesional de la salud.

---

## Resumen

| | Imagen | Tabular |
|---|---|---|
| Dataset | NIH ChestX-ray14 | VitalDB (PhysioNet) |
| Tipo | Radiografías de tórax (DICOM al PACS) | Casos quirúrgicos (tabla) |
| Licencia | Sin restricciones; citar y reconocer al NIH | CC BY 4.0 |
| Cantidad cargada | 24 imágenes | 6.199 reales + 4.958 sintéticos |
| Requisito | R06 | R07 |

---

## Corpus

Base documental del asistente y del agente clínico (R23). Son 18 documentos en `docs/rag/guias/`, escritos
en español a partir de guías de práctica clínica, normas colombianas y la documentación del propio Pulso.
Cada uno declara su fuente en el encabezado y cada respuesta cita el documento y el fragmento usados
(campo `fuentes` de `/rag/preguntar` y de `/agente/chat`). Se indexan con `python scripts/rag_indexar.py`
(BM25 + embeddings, ver [RAG.md](RAG.md)). Además se indexa la **historia aprobada** de cada paciente
(solo reportes de IA aprobados y encuentros), seudonimizada y sin identificadores, que solo ve quien tiene
acceso a ese paciente.

La columna *audiencia* filtra qué perfiles recuperan cada documento: el contable solo recupera los de
facturación y nunca una guía clínica ni una historia.

| Id | Documento | Audiencia | Fuente |
|---|---|---|---|
| G01 | Hipoglucemia en el adulto hospitalizado | clinica | American Diabetes Association. Standards of Care in Diabetes—2025, sección 6 "Glycemic Goals and Hypoglycemia". Diabetes Care 2025;48(Suppl 1). |
| G02 | Hiperpotasemia aguda en el adulto | clinica | UK Renal Association (hoy UK Kidney Association). Clinical Practice Guidelines: Treatment of Acute Hyperkalaemia in Adults. 2020. Consultada a través de un resumen secundario; verificar contra el documento original de la UK Kidney Association. |
| G03 | Anemia y umbrales de transfusión de glóbulos rojos | clinica | Carson JL, et al. Red Blood Cell Transfusion: 2023 AABB International Guidelines. JAMA. 2023;330(19):1892-1902. doi:10.1001/jama.2023.12914 |
| G04 | Clasificación del estado físico ASA | clinica | American Society of Anesthesiologists. ASA Physical Status Classification System. Definiciones aprobadas por la Cámara de Delegados de la ASA, última aprobación el 15 de octubre de 2014. |
| G05 | Admisión, alta y triaje en la unidad de cuidados intensivos | clinica | Nates JL, Nunnally M, Kleinpell R, et al. ICU Admission, Discharge, and Triage Guidelines: A Framework to Enhance Clinical Operations, Development of Institutional Policies, and Further Research. Crit Care Med. 2016;44(8):1553-1602. doi:10.1097/CCM.0000000000001856 |
| G06 | Lista de verificación de la seguridad de la cirugía de la OMS | clinica | Organización Mundial de la Salud. Lista OMS de verificación de la seguridad de la cirugía, 2009. Evidencia: Haynes AB, et al. A Surgical Safety Checklist to Reduce Morbidity and Mortality in a Global Population. N Engl J Med. 2009;360(5):491-499. |
| G07 | Protocolo interno de Pulso para alertas críticas, reportes de IA y remisiones | operativa | Equipo Pulso (Universidad Autónoma de Occidente). Documentación del proyecto: ml/UMBRAL.md, docs/EVENTOS.md y docs/NORMATIVA.md, 2026. |
| G08 | Facturación de servicios en Pulso | facturacion | Equipo Pulso (Universidad Autónoma de Occidente). Documentación del proyecto: docs/CONTABILIDAD.md y docs/CONTRATO_FACTURACION.md, 2026. |
| G09 | Triage en los servicios de urgencias en Colombia | operativa | Ministerio de Salud y Protección Social. Resolución 5596 de 2015, por la cual se definen los criterios técnicos para el Sistema de Selección y Clasificación de pacientes en los servicios de urgencias "Triage". Complementada por la Resolución 373 de 2026 (reporte de tiempos de espera en urgencias). |
| G10 | Escala NEWS2 de alerta temprana | clinica | Royal College of Physicians. National Early Warning Score (NEWS) 2: Standardising the assessment of acute-illness severity in the NHS. Informe actualizado del grupo de trabajo. Londres: RCP, 2017. |
| G11 | Manejo inicial de la sepsis y el choque séptico en adultos | clinica | Evans L, Rhodes A, Alhazzani W, et al. Surviving Sepsis Campaign: International Guidelines for Management of Sepsis and Septic Shock 2021. Crit Care Med. 2021;49(11):e1063-e1143. doi:10.1097/CCM.0000000000005337 |
| G12 | Ayuno preoperatorio | clinica | American Society of Anesthesiologists. Practice Guidelines for Preoperative Fasting and the Use of Pharmacologic Agents to Reduce the Risk of Pulmonary Aspiration. Anesthesiology. 2017;126(3):376-393. Actualización modular: Joshi GP, et al. 2023 American Society of Anesthesiologists Practice Guidelines for Preoperative Fasting: Carbohydrate-containing Clear Liquids with or without Protein, Chewing Gum, and Pediatric Fasting Duration. Anesthesiology. 2023;138(2):132-151. |
| G13 | La historia clínica en Colombia: reserva, acceso y conservación | clinica, paciente | Ministerio de Salud. Resolución 1995 de 1999, por la cual se establecen normas para el manejo de la historia clínica; Resolución 839 de 2017 (retención y conservación); Ley 2015 de 2020 (historia clínica electrónica interoperable). Superintendencia Nacional de Salud, Concepto 337711 de 2024. |
| G14 | Derechos y deberes del paciente en Colombia | paciente, clinica | Ministerio de Salud y Protección Social. Resolución 229 de 2020 (lineamientos de la carta de derechos y deberes de la persona afiliada y del paciente, que derogó la Resolución 4343 de 2012), con base en la Ley Estatutaria 1751 de 2015. Resolución 1642 de 2026 (nuevos lineamientos de la carta de derechos y deberes). |
| G15 | Indicadores de calidad en salud (Resolución 256 de 2016) | coordinacion | Ministerio de Salud y Protección Social. Resolución 256 de 2016, por la cual se dictan disposiciones en relación con el Sistema de Información para la Calidad y se establecen los indicadores para el monitoreo de la calidad en salud. Secretaría Distrital de Salud de Bogotá, ABC de la Resolución 256 de 2016. |
| G16 | Habilitación de servicios de salud (Resolución 3100 de 2019) | coordinacion | Ministerio de Salud y Protección Social. Resolución 3100 de 2019, por la cual se definen los procedimientos y condiciones de inscripción de los prestadores de servicios de salud y de habilitación de los servicios de salud, y se adopta el Manual de Inscripción de Prestadores y Habilitación de Servicios de Salud. Modificada por las Resoluciones 544 de 2023 y 465 de 2025. |
| G17 | Cuotas moderadoras y copagos en 2026 | facturacion | Ministerio de Salud y Protección Social. Circular Externa 048 de 2025 (montos de cuotas moderadoras y copagos para 2026). Decreto 1652 de 2022 (que derogó el Acuerdo 260 de 2004 salvo sus artículos 8 a 11). Ley 2294 de 2023, art. 313 (Unidad de Valor Básico, UVB). Resolución 3488 de 2025 del Ministerio de Hacienda (UVB 2026). |
| G18 | Seguridad, protección de datos y normativa de Pulso | admin | Equipo Pulso (Universidad Autónoma de Occidente). Documentación del proyecto: docs/SEGURIDAD.md y docs/NORMATIVA.md, 2026. Ley 1581 de 2012, Decreto 1377 de 2013, Ley 2015 de 2020, Resolución 866 de 2021. |

**Pertinencia.** El problema de Pulso es coordinar camas, quirófanos y urgencias y decidir a tiempo quién
necesita UCI, cirugía o un especialista. Por eso el corpus cubre: valores críticos de laboratorio que
disparan alertas (G01, G02, G03), decisiones preoperatorias (G04, G06, G12), deterioro y UCI (G05, G10,
G11), urgencias y oportunidad (G09, G15), la norma de historia clínica, derechos y habilitación (G13, G14,
G16) y los procesos internos de Pulso (G07, G08, G17, G18).

**Evaluación.** `docs/rag/eval/preguntas.json` (preguntas con respuesta esperada y documento fuente) y
`docs/rag/eval/RESULTADOS.md` y `docs/rag/eval/RESULTADOS_ABSTENCION.md`: acierto de recuperación y abstención cuando la
pregunta no tiene soporte en el corpus.
