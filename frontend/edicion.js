/* Edición de lo ya colocado: seleccionar, mover, girar, pintar, duplicar.
 *
 * `designer.js` se ocupa de la escena y de colocar piezas nuevas; aquí vive
 * todo lo que modifica el modelo existente. La separación no es caprichosa:
 * son dos formas distintas de trabajar (construir y rectificar) y mezclarlas
 * en un solo archivo hacía imposible seguir ninguna de las dos.
 *
 * Este módulo no importa al diseñador: recibe de él un contexto con el estado
 * compartido y las funciones de dibujo. Así no hay dependencias circulares y
 * queda claro qué necesita de la escena, que es poco: dibujar el fantasma del
 * grupo que se arrastra y pedir un fotograma.
 */

let ctx = null;
const $ = (sel) => document.querySelector(sel);

// Colores en los que existe cada molde: se pide una vez por pieza y se guarda,
// porque el catálogo no cambia mientras se diseña.
const cacheColores = new Map();

export function iniciar(contexto) {
  ctx = contexto;
  conectarPanel();
}

// --------------------------------------------------------------------------
// La selección
// --------------------------------------------------------------------------
export function seleccionadas() {
  const ids = new Set(ctx.estado.seleccion);
  return (ctx.estado.modelo?.piezas || []).filter((p) => ids.has(p.id));
}

export function seleccionar(ids, { anadir = false } = {}) {
  const estado = ctx.estado;
  const lista = Array.isArray(ids) ? ids : [ids];
  if (!anadir) {
    estado.seleccion = lista.filter((id) => id !== null && id !== undefined);
  } else {
    const actual = new Set(estado.seleccion);
    for (const id of lista) {
      if (id === null || id === undefined) continue;
      // Ctrl+clic sobre algo ya seleccionado lo saca de la selección.
      if (actual.has(id)) actual.delete(id);
      else actual.add(id);
    }
    estado.seleccion = [...actual];
  }
  ctx.resaltarSeleccion();
  pintarPanel();
}

export function limpiar() {
  ctx.estado.seleccion = [];
  ctx.resaltarSeleccion();
  pintarPanel();
}

export function seleccionarTodo() {
  seleccionar(ctx.piezasVisibles().map((p) => p.id));
  ctx.avisar(`${ctx.estado.seleccion.length} piezas seleccionadas.`);
}

/** Todas las piezas iguales (mismo molde y color) que las seleccionadas. */
export function seleccionarIguales() {
  const elementos = new Set(seleccionadas().map((p) => p.element_id));
  if (!elementos.size) return ctx.avisar("Selecciona antes una pieza.", "error");
  seleccionar(ctx.piezasVisibles().filter((p) => elementos.has(p.element_id)).map((p) => p.id));
  ctx.avisar(`${ctx.estado.seleccion.length} piezas iguales seleccionadas.`);
}

export function seleccionarPaso(pasoId) {
  seleccionar(ctx.piezasVisibles().filter((p) => p.paso_id === pasoId).map((p) => p.id));
}

// --------------------------------------------------------------------------
// Validación local: la misma regla que aplicará el servidor
// --------------------------------------------------------------------------
/** ¿El grupo desplazado choca con el resto del modelo o se sale de la placa?
 *
 * El servidor lo vuelve a comprobar —es él quien manda—, pero hacerlo también
 * aquí es lo que permite que el fantasma se ponga rojo mientras se arrastra,
 * en vez de descubrir el choque al soltar.
 */
export function estorbo(dx, dy, dz, ids = ctx.estado.seleccion) {
  const seleccion = new Set(ids);
  const piezas = ctx.estado.modelo?.piezas || [];
  const placa = ctx.estado.modelo?.placa;
  const movidas = piezas.filter((p) => seleccion.has(p.id));
  if (!movidas.length) return "nada que mover";

  for (const pieza of movidas) {
    const [ancho, fondo] = ctx.huella(pieza.forma, pieza.rotacion);
    if (
      pieza.x + dx < 0 ||
      pieza.z + dz < 0 ||
      pieza.x + dx + ancho > placa.ancho ||
      pieza.z + dz + fondo > placa.fondo
    ) {
      return "se sale de la placa";
    }
    if (pieza.y + dy < 0) return "por debajo de la placa";
  }

  // Las piezas que se mueven no se estorban entre sí: sólo cuenta lo demás.
  const ocupadas = new Map();
  for (const pieza of piezas) {
    if (seleccion.has(pieza.id)) continue;
    for (const [x, z, desde, hasta] of pieza.columnas || []) {
      const clave = `${x},${z}`;
      if (!ocupadas.has(clave)) ocupadas.set(clave, []);
      ocupadas.get(clave).push([desde, hasta]);
    }
  }
  for (const pieza of movidas) {
    for (const [x, z, desde, hasta] of pieza.columnas || []) {
      const tramos = ocupadas.get(`${x + dx},${z + dz}`);
      if (!tramos) continue;
      for (const [d, h] of tramos) {
        if (!(hasta + dy <= d || h <= desde + dy)) return "choca con otra pieza";
      }
    }
  }
  return null;
}

// --------------------------------------------------------------------------
// Operaciones sobre el servidor
// --------------------------------------------------------------------------
async function operar(ruta, cuerpo, mensaje) {
  const estado = ctx.estado;
  if (!estado.seleccion.length) return ctx.avisar("No hay ninguna pieza seleccionada.", "error");
  try {
    const r = await ctx.api(`/api/montajes/${estado.montajeId}/edicion/${ruta}`, {
      method: "POST",
      body: JSON.stringify({ ids: estado.seleccion, ...cuerpo }),
    });
    // Las copias son piezas nuevas: lo natural es seguir trabajando con ellas.
    if (r.ids?.length) estado.seleccion = r.ids;
    // Cualquier edición mueve las existencias: los colores se vuelven a pedir.
    cacheColores.clear();
    await ctx.recargar({ conservarVista: true });
    if (r.avisos?.length) ctx.avisar(r.avisos[0], "error");
    else if (mensaje) ctx.avisar(typeof mensaje === "function" ? mensaje(r) : mensaje);
    return r;
  } catch (e) {
    ctx.avisar(e.message, "error");
    return null;
  }
}

export const mover = (dx, dy, dz, apoyar = false) =>
  operar("mover", { dx, dy, dz, apoyar }, null);

export const girar = () => operar("girar", { grados: 90 }, "Selección girada 90°.");

export const reflejar = (eje) =>
  operar("reflejar", { eje }, (r) => `Selección reflejada en ${eje.toUpperCase()}.`);

export const duplicar = (dx = 1, dy = 0, dz = 0) =>
  operar(
    "duplicar",
    { dx, dy, dz, paso_id: ctx.estado.pasoActivo },
    (r) => `${r.afectadas} pieza${r.afectadas === 1 ? "" : "s"} duplicada${r.afectadas === 1 ? "" : "s"}.`
  );

export const apoyar = () => operar("mover", { apoyar: true }, "Selección apoyada sobre el modelo.");

export const quitar = () =>
  operar("quitar", {}, (r) => `${r.eliminadas} pieza${r.eliminadas === 1 ? "" : "s"} retirada${r.eliminadas === 1 ? "" : "s"}: vuelven al inventario.`);

export const llevarAPaso = (pasoId) =>
  operar("paso", { paso_id: pasoId }, (r) => `Piezas movidas al paso «${r.paso}».`);

export const sustituir = (cuerpo, mensaje) => operar("sustituir", cuerpo, mensaje);

/** Pinta la selección de un color: mantiene el molde de cada pieza. */
export async function pintarDe(elementIdOColor) {
  const cuerpo =
    typeof elementIdOColor === "object" ? elementIdOColor : { element_id: elementIdOColor };
  return sustituir(cuerpo, "Piezas repintadas.");
}

/** Cambia las piezas seleccionadas por la que está elegida en la paleta. */
export async function sustituirPorLaMano() {
  const ficha = ctx.estado.piezaEnMano;
  if (!ficha) return ctx.avisar("Elige antes una pieza en la paleta.", "error");
  return sustituir({ element_id: ficha.element_id }, `Cambiadas por ${ficha.pieza}.`);
}

// --------------------------------------------------------------------------
// Una sola pieza: coordenadas exactas
// --------------------------------------------------------------------------
export async function fijarCoordenadas(cambios) {
  const id = ctx.estado.seleccion[0];
  if (!id) return;
  try {
    await ctx.api(`/api/colocaciones/${id}`, {
      method: "PATCH",
      body: JSON.stringify(cambios),
    });
    await ctx.recargar({ conservarVista: true });
  } catch (e) {
    ctx.avisar(e.message, "error");
  }
}

// --------------------------------------------------------------------------
// Portapapeles: copiar un trozo del modelo y pegarlo al lado
// --------------------------------------------------------------------------
export function copiar() {
  const piezas = seleccionadas();
  if (!piezas.length) return ctx.avisar("No hay nada que copiar.", "error");
  ctx.estado.portapapeles = piezas.map((p) => p.id);
  ctx.avisar(`${piezas.length} pieza${piezas.length === 1 ? "" : "s"} copiada${piezas.length === 1 ? "" : "s"}. Pega con Ctrl+V.`);
}

/** Pegar es duplicar lo copiado, desplazado para que no se pise con el original. */
export async function pegar() {
  const estado = ctx.estado;
  const vivas = new Set((estado.modelo?.piezas || []).map((p) => p.id));
  const ids = (estado.portapapeles || []).filter((id) => vivas.has(id));
  if (!ids.length) return ctx.avisar("El portapapeles está vacío.", "error");

  // Se busca el primer hueco libre a la derecha: pegar encima del original
  // sería un error seguro.
  const piezas = (estado.modelo?.piezas || []).filter((p) => ids.includes(p.id));
  const x0 = Math.min(...piezas.map((p) => p.x));
  const ancho = Math.max(...piezas.map((p) => p.x + ctx.huella(p.forma, p.rotacion)[0])) - x0;
  let desplazamiento = null;
  for (let d = ancho; d <= ancho + 6; d++) {
    if (!estorbo(d, 0, 0, ids)) {
      desplazamiento = d;
      break;
    }
  }
  if (desplazamiento === null) {
    return ctx.avisar("No hay sitio libre al lado para pegar la copia.", "error");
  }

  const previo = estado.seleccion;
  estado.seleccion = ids;
  const r = await duplicar(desplazamiento, 0, 0);
  // Si el pegado no ha entrado, la selección vuelve a ser la que había.
  if (!r) seleccionar(previo);
}

// --------------------------------------------------------------------------
// Deshacer y rehacer
// --------------------------------------------------------------------------
async function historial(accion) {
  const estado = ctx.estado;
  if (!estado.montajeId) return;
  try {
    const r = await ctx.api(`/api/montajes/${estado.montajeId}/${accion}`, { method: "POST" });
    if (r.deshecho === false || r.rehecho === false) return ctx.avisar(r.motivo, "error");
    await ctx.recargar({ conservarVista: true });
    ctx.avisar(`${accion === "deshacer" ? "Deshecho" : "Rehecho"}: ${r.operacion}.`);
  } catch (e) {
    ctx.avisar(e.message, "error");
  }
}

export const deshacer = () => historial("deshacer");
export const rehacer = () => historial("rehacer");

/** Enciende o apaga los botones según lo que haya en cada pila. */
export function pintarBotonesHistorial() {
  const estado = ctx.estado.modelo?.historial;
  const atras = $("#dis-deshacer");
  const adelante = $("#dis-rehacer");
  if (!atras || !adelante) return;
  atras.disabled = !estado?.puede_deshacer;
  adelante.disabled = !estado?.puede_rehacer;
  atras.title = estado?.puede_deshacer
    ? `Deshacer: ${estado.ultima_operacion} (Ctrl+Z)`
    : "No hay nada que deshacer";
  adelante.title = estado?.puede_rehacer
    ? `Rehacer: ${estado.siguiente_operacion} (Ctrl+Y)`
    : "No hay nada que rehacer";
}

// --------------------------------------------------------------------------
// El panel: lo que se ve a la derecha
// --------------------------------------------------------------------------
const esc = (v) => (ctx ? ctx.esc(v) : String(v ?? ""));

/** Altura a la que caería la pieza en mano, con el ajuste manual aplicado. */
function alturaEnMano() {
  const previa = ctx.estado.previsualizacion;
  if (!previa) return "";
  const ajuste = ctx.estado.desnivel
    ? ` (apoyo ${previa.apoyo}, ajustada ${ctx.estado.desnivel > 0 ? "+" : ""}${ctx.estado.desnivel})`
    : "";
  const aviso = previa.cabe ? "" : ` <b style="color:#ff8080">ahí choca con otra pieza</b>`;
  return `<div class="pista" style="margin:2px 0 0">Altura: ${previa.y} placa${
    previa.y === 1 ? "" : "s"
  }${ajuste}${aviso}</div>`;
}

function resumenDeSeleccion(piezas) {
  const grupos = new Map();
  for (const pieza of piezas) {
    const clave = pieza.element_id;
    if (!grupos.has(clave)) grupos.set(clave, { ...pieza, cantidad: 0 });
    grupos.get(clave).cantidad++;
  }
  return [...grupos.values()].sort((a, b) => b.cantidad - a.cantidad);
}

function camposDeUna(pieza) {
  return `
    <div class="campos-xyz">
      <label>x<input type="number" data-campo="x" value="${pieza.x}" min="0" /></label>
      <label>y<input type="number" data-campo="y" value="${pieza.y}" min="0" /></label>
      <label>z<input type="number" data-campo="z" value="${pieza.z}" min="0" /></label>
      <label>giro<select data-campo="rotacion">
        ${[0, 90, 180, 270]
          .map((g) => `<option value="${g}" ${g === pieza.rotacion ? "selected" : ""}>${g}°</option>`)
          .join("")}
      </select></label>
    </div>`;
}

function muestrasDeColor(colores) {
  return `<div class="muestras">${colores
    .map(
      (c) => `<span class="muestra ${c.actual ? "actual" : ""}"
        data-accion="pintar" data-element="${esc(c.element_id)}"
        style="background:${esc(c.color_hex || "#666")}"
        title="${esc(c.color)} · ${c.disponible} libres${c.actual ? " (color actual)" : ""}">
        ${c.actual ? "" : `<span class="n">${c.disponible}</span>`}
      </span>`
    )
    .join("")}</div>`;
}

/** Los colores del molde seleccionado, si todas las piezas comparten molde. */
async function coloresDeLaSeleccion(piezas) {
  const moldes = new Set(piezas.map((p) => p.design_id));
  if (moldes.size !== 1) return null;
  const clave = piezas[0].element_id;
  if (!cacheColores.has(clave)) {
    try {
      const r = await ctx.api(`/api/elementos/${encodeURIComponent(clave)}/colores`);
      cacheColores.set(clave, r.colores || []);
    } catch {
      cacheColores.set(clave, []);
    }
  }
  return cacheColores.get(clave);
}

export function pintarPanel() {
  const caja = $("#dis-seleccion");
  if (!caja || !ctx) return;
  const estado = ctx.estado;

  // Con una pieza en mano, el panel habla de ella: es lo que va a pasar al
  // hacer clic.
  if (estado.piezaEnMano) {
    const p = estado.piezaEnMano;
    caja.className = "seleccion";
    caja.innerHTML = `
      ${p.imagen ? `<img src="${esc(p.imagen)}" alt="" />` : ""}
      <div class="cabeza"><span class="titulo">${esc(p.pieza)}</span>
        <button class="pequeno" data-accion="soltar">Soltar</button></div>
      <div class="pista" style="margin:2px 0 0">${esc(p.color)} · ${p.disponible} libres · girada ${estado.rotacion}°</div>
      ${alturaEnMano()}
      ${
        estado.seleccion.length
          ? `<div class="acciones"><button data-accion="sustituir-mano">Cambiar la selección por esta pieza</button></div>`
          : ""
      }
      <div class="atajos">Clic en la placa para colocar · <kbd>R</kbd> girar ·
        <kbd>+</kbd>/<kbd>−</kbd> subir o bajar · <kbd>Esc</kbd> soltar</div>`;
    return;
  }

  const piezas = seleccionadas();
  if (!piezas.length) {
    caja.className = "seleccion vacio";
    caja.innerHTML = `Ninguna pieza seleccionada.
      <div class="atajos">Con la herramienta <b>Seleccionar</b>: clic en una pieza,
        <kbd>Ctrl</kbd>+clic para sumar, arrastrar sobre el vacío para encuadrar
        varias y arrastrar una pieza elegida para moverlas todas.</div>`;
    return;
  }

  const resumen = resumenDeSeleccion(piezas);
  const pasos = estado.modelo?.pasos || [];
  const unica = piezas.length === 1 ? piezas[0] : null;

  caja.className = "seleccion";
  caja.innerHTML = `
    <div class="cabeza">
      <span class="titulo">${piezas.length} pieza${piezas.length === 1 ? "" : "s"} seleccionada${piezas.length === 1 ? "" : "s"}</span>
      <button class="pequeno" data-accion="limpiar" title="Quitar la selección (Esc)">✕</button>
    </div>
    <div class="lista-piezas">
      ${resumen
        .map(
          (r) => `<div><span class="punto" style="background:${esc(r.color_hex || "#666")}"></span>
            <b>${r.cantidad}×</b> ${esc(r.pieza)} · ${esc(r.color)}</div>`
        )
        .join("")}
    </div>
    ${unica ? camposDeUna(unica) : ""}
    <div class="acciones">
      <button data-accion="girar" title="Girar el grupo 90° (R)">⟳ Girar</button>
      <button data-accion="espejo-x" title="Reflejar en el eje X">⇔ Espejo X</button>
      <button data-accion="espejo-z" title="Reflejar en el eje Z">⇕ Espejo Z</button>
      <button data-accion="duplicar" title="Duplicar al lado (Ctrl+D)">⧉ Duplicar</button>
      <button data-accion="apoyar" title="Bajar hasta apoyarse en lo que haya debajo">↓ Apoyar</button>
      <button data-accion="quitar" class="peligro" title="Quitar del modelo (Supr)">Quitar</button>
    </div>
    <div class="acciones">
      <button data-accion="iguales" title="Seleccionar todas las piezas iguales">Todas las iguales</button>
      <button data-accion="todo" title="Seleccionar todo el modelo (Ctrl+A)">Todo el modelo</button>
    </div>
    <div id="dis-colores"></div>
    ${
      pasos.length > 1
        ? `<h3>Llevar a otro paso</h3>
           <select data-accion="paso">
             <option value="">Elige el paso…</option>
             ${pasos
               .map((p) => `<option value="${p.id}">${p.posicion}. ${esc(p.titulo)}</option>`)
               .join("")}
           </select>`
        : ""
    }
    <div class="atajos">
      <kbd>←→↑↓</kbd> mover · <kbd>+</kbd>/<kbd>−</kbd> subir o bajar ·
      <kbd>R</kbd> girar · <kbd>Ctrl</kbd>+<kbd>C</kbd>/<kbd>V</kbd> copiar y pegar ·
      <kbd>Supr</kbd> quitar
    </div>`;

  // Los colores llegan del servidor: se pintan cuando estén, sin bloquear.
  coloresDeLaSeleccion(piezas).then((colores) => {
    const hueco = $("#dis-colores");
    if (!hueco || !colores) return;
    hueco.innerHTML = colores.length
      ? `<h3>Pintar de otro color</h3>${muestrasDeColor(colores)}`
      : "";
  });
}

/** Un solo enganche para todo el panel: se redibuja entero en cada cambio. */
function conectarPanel() {
  const caja = $("#dis-seleccion");
  if (!caja) return;

  caja.addEventListener("click", (evento) => {
    const objetivo = evento.target.closest("[data-accion]");
    if (!objetivo || objetivo.tagName === "SELECT") return;
    const accion = objetivo.dataset.accion;
    const acciones = {
      limpiar,
      soltar: () => ctx.soltarPieza(),
      girar,
      "espejo-x": () => reflejar("x"),
      "espejo-z": () => reflejar("z"),
      duplicar: () => duplicar(1, 0, 0),
      apoyar,
      quitar,
      iguales: seleccionarIguales,
      todo: seleccionarTodo,
      pintar: () => pintarDe(objetivo.dataset.element),
      "sustituir-mano": sustituirPorLaMano,
    };
    acciones[accion]?.();
  });

  caja.addEventListener("change", (evento) => {
    const campo = evento.target.dataset.campo;
    if (campo) {
      const valor = Number(evento.target.value);
      return fijarCoordenadas({ [campo]: Number.isFinite(valor) ? valor : 0 });
    }
    if (evento.target.dataset.accion === "paso" && evento.target.value) {
      llevarAPaso(Number(evento.target.value));
    }
  });
}
