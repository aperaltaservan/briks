/* Interfaz del inventario LEGO. Habla con la misma API que usa el MCP. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// --------------------------------------------------------------------------
// Utilidades
// --------------------------------------------------------------------------
async function api(ruta, opciones = {}) {
  const respuesta = await fetch(ruta, {
    headers: { "Content-Type": "application/json" },
    ...opciones,
  });
  let datos = null;
  try {
    datos = await respuesta.json();
  } catch {
    datos = null;
  }
  if (respuesta.status === 401 && ruta !== "/api/auth/login") {
    // La sesión ha caducado o no existe: no tiene sentido seguir mostrando
    // la app, así que volvemos directamente a la pantalla de acceso.
    window.location.href = "/login";
    return new Promise(() => {});
  }
  if (!respuesta.ok) {
    const detalle = datos?.detail;
    const mensaje =
      typeof detalle === "string"
        ? detalle
        : detalle?.error || `Error ${respuesta.status}`;
    const error = new Error(mensaje);
    error.estado = respuesta.status;
    error.detalle = typeof detalle === "object" ? detalle : null;
    throw error;
  }
  return datos;
}

async function cerrarSesion() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    window.location.href = "/login";
  }
}

/** Diálogo de sí/no. Devuelve una promesa: true si se acepta. */
function confirmar({ titulo, texto, detalle = "", aceptar = "Sí, continuar" }) {
  return new Promise((resolver) => {
    const caja = $("#dialogo");
    $("#dialogo-titulo").textContent = titulo;
    $("#dialogo-texto").textContent = texto;
    $("#dialogo-detalle").innerHTML = detalle;
    $("#dialogo-aceptar").textContent = aceptar;
    caja.classList.remove("oculto");

    const cerrar = (valor) => {
      caja.classList.add("oculto");
      $("#dialogo-aceptar").removeEventListener("click", alAceptar);
      $("#dialogo-cancelar").removeEventListener("click", alCancelar);
      resolver(valor);
    };
    const alAceptar = () => cerrar(true);
    const alCancelar = () => cerrar(false);
    $("#dialogo-aceptar").addEventListener("click", alAceptar);
    $("#dialogo-cancelar").addEventListener("click", alCancelar);
  });
}

let temporizadorAviso;
function avisar(mensaje, tipo = "ok") {
  const caja = $("#aviso");
  caja.textContent = mensaje;
  caja.className = `aviso ${tipo}`;
  clearTimeout(temporizadorAviso);
  temporizadorAviso = setTimeout(() => caja.classList.add("oculto"), 4200);
}

/** Escapa texto antes de insertarlo como HTML. */
function esc(valor) {
  if (valor === null || valor === undefined) return "";
  return String(valor).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function puntoColor(hex) {
  return `<span class="punto" style="background:${esc(hex || "#666")}"></span>`;
}

// --------------------------------------------------------------------------
// Navegación
// --------------------------------------------------------------------------
const cargadores = {};

/** Activa una vista y la carga. El hash permite recargar sin perder la pestaña. */
function irA(vista) {
  if (!$(`#vista-${vista}`)) vista = "resumen";
  $$("#nav button").forEach((b) => b.classList.toggle("activa", b.dataset.vista === vista));
  $$(".vista").forEach((v) => v.classList.toggle("activa", v.id === `vista-${vista}`));
  cargadores[vista]?.();
  // El diseñador 3D es un módulo aparte: se entera así de que le toca.
  window.dispatchEvent(new CustomEvent("vista-cambiada", { detail: vista }));
}

$("#nav").addEventListener("click", (evento) => {
  const boton = evento.target.closest("button[data-vista]");
  if (!boton) return;
  location.hash = boton.dataset.vista;
});

$("#salir")?.addEventListener("click", cerrarSesion);

window.addEventListener("hashchange", () => irA(location.hash.slice(1)));

// --------------------------------------------------------------------------
// Resumen
// --------------------------------------------------------------------------
cargadores.resumen = async () => {
  try {
    const d = await api("/api/resumen");
    $("#cifras").innerHTML = [
      ["Piezas totales", d.piezas_totales, ""],
      ["Disponibles", d.disponibles, "acento-verde"],
      ["Reservadas en montajes", d.reservadas_en_montajes, "acento-amarillo"],
      ["Referencias distintas", d.referencias_distintas, ""],
      ["Sets importados", d.sets_importados, ""],
      ["Catálogo (elementos)", d.catalogo.elementos, ""],
    ]
      .map(
        ([etiqueta, valor, clase]) =>
          `<div class="cifra ${clase}"><div class="valor">${valor}</div><div class="etiqueta">${etiqueta}</div></div>`
      )
      .join("");

    const barras = (datos, campoNombre, conColor) => {
      const max = Math.max(...datos.map((x) => x.piezas), 1);
      return datos
        .slice(0, 10)
        .map(
          (x) => `<div class="barra">
            <span class="nombre">${
              conColor ? `<span class="pista-color" style="background:${esc(x.hex || "#666")}"></span>` : ""
            }${esc(x[campoNombre])}</span>
            <span class="via"><span class="relleno" style="width:${(100 * x.piezas) / max}%"></span></span>
            <span class="num">${x.piezas}</span>
          </div>`
        )
        .join("");
    };
    $("#por-color").innerHTML = barras(d.por_color, "color", true);
    $("#por-categoria").innerHTML = barras(d.por_categoria, "categoria", false);

    const m = await api("/api/que-puedo-montar");
    const total = m.completos.length + m.parciales.length;
    if (!total) {
      $("#montables").innerHTML = `<p class="pista">Todavía no hay sets importados.</p>`;
    } else {
      $("#montables").innerHTML = [...m.completos, ...m.parciales]
        .map(
          (s) => `<div class="tarjeta">
            <div class="titulo">
              <span>${esc(s.set)} ${s.nombre ? `· ${esc(s.nombre)}` : ""}</span>
              <span class="insignia ${s.completo ? "ok" : "aviso"}">${
                s.completo ? "Completo" : `${s.cobertura_pct}%`
              }</span>
            </div>
            <div class="detalle">${s.piezas_totales} piezas · ${
              s.completo
                ? "tienes todo lo necesario"
                : `faltan ${s.num_faltantes} referencias`
            }</div>
          </div>`
        )
        .join("");
    }
  } catch (e) {
    avisar(e.message, "error");
  }
};

// --------------------------------------------------------------------------
// Inventario
// --------------------------------------------------------------------------
function tarjetaPieza(p) {
  const img = p.imagen
    ? `<img src="${esc(p.imagen)}" alt="${esc(p.pieza)}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'sin-imagen',textContent:'sin imagen'}))" />`
    : `<div class="sin-imagen">sin imagen</div>`;
  return `<div class="pieza" data-element="${esc(p.element_id)}">
    ${img}
    <div class="nombre">${esc(p.pieza)}</div>
    <div class="meta">${puntoColor(p.color_hex)}${esc(p.color)}</div>
    <div class="id">${esc(p.element_id)} · molde ${esc(p.design_id)}</div>
    <div class="conteos">
      <span><b>${p.existencias ?? p.en_inventario ?? 0}</b>en total</span>
      <span><b>${p.disponible ?? "–"}</b>disponibles</span>
    </div>
    ${p.ubicaciones?.length ? `<div class="id">📦 ${esc(p.ubicaciones.join(", "))}</div>` : ""}
    <div class="ajuste">
      <button class="pequeno" data-delta="-1">−</button>
      <input type="number" value="1" min="1" aria-label="Cantidad a ajustar" />
      <button class="pequeno" data-delta="1">+</button>
    </div>
  </div>`;
}

async function buscarInventario() {
  const caja = $("#inv-resultado");
  caja.innerHTML = `<p class="cargando">Buscando…</p>`;
  const params = new URLSearchParams({
    descripcion: $("#inv-q").value.trim(),
    color: $("#inv-color").value.trim(),
    ubicacion: $("#inv-ubi").value.trim(),
    solo_disponibles: $("#inv-disp").checked,
    limite: 60,
  });
  try {
    const d = await api(`/api/inventario?${params}`);
    caja.innerHTML = d.items.length
      ? d.items.map(tarjetaPieza).join("")
      : `<p class="cargando">Sin resultados. Prueba con otra descripción o quita el filtro de color.</p>`;
  } catch (e) {
    caja.innerHTML = `<p class="cargando">${esc(e.message)}</p>`;
  }
}

cargadores.inventario = () => {
  if (!$("#inv-resultado").dataset.cargado) {
    $("#inv-resultado").dataset.cargado = "1";
    buscarInventario();
  }
};

$("#inv-buscar").addEventListener("click", buscarInventario);
["#inv-q", "#inv-color", "#inv-ubi"].forEach((sel) =>
  $(sel).addEventListener("keydown", (e) => e.key === "Enter" && buscarInventario())
);
$("#inv-disp").addEventListener("change", buscarInventario);

// Ajuste rápido de existencias desde la propia tarjeta.
$("#inv-resultado").addEventListener("click", async (evento) => {
  const boton = evento.target.closest("button[data-delta]");
  if (!boton) return;
  const tarjeta = boton.closest(".pieza");
  const cantidad = Number(tarjeta.querySelector("input").value) || 1;
  const delta = Number(boton.dataset.delta) * cantidad;
  try {
    await api("/api/inventario/ajustar", {
      method: "POST",
      body: JSON.stringify({
        element_id: tarjeta.dataset.element,
        cantidad: delta,
        modo: "sumar",
        motivo: "manual",
      }),
    });
    avisar(`${delta > 0 ? "Añadidas" : "Retiradas"} ${Math.abs(delta)} unidades.`);
    buscarInventario();
  } catch (e) {
    avisar(e.message, "error");
  }
});

// --------------------------------------------------------------------------
// Sets
// --------------------------------------------------------------------------
cargadores.sets = async () => {
  const caja = $("#sets-lista");
  caja.innerHTML = `<p class="cargando">Cargando…</p>`;
  try {
    const d = await api("/api/sets");
    caja.innerHTML = d.sets.length
      ? d.sets
          .map(
            (s) => `<div class="tarjeta">
              <div class="titulo">
                <span>${esc(s.set)} ${s.nombre ? `· ${esc(s.nombre)}` : ""}</span>
                <span class="insignia ${s.inventariado ? "ok" : ""}">${
                  s.inventariado ? "Inventariado" : "Sin inventariar"
                }</span>
              </div>
              <div class="detalle">${s.piezas} piezas · ${s.referencias} referencias</div>
              <div class="fila">
                <input class="ubi-set" placeholder="Ubicación (opcional)" />
                <input class="veces-set" type="number" value="1" min="1" style="max-width:80px" title="Cuántas copias" />
                <button class="${s.inventariado ? "" : "primario "}pequeno inventariar" data-set="${esc(
                  s.set
                )}">${s.inventariado ? "Inventariar otra copia" : "Inventariar"}</button>
              </div>
              ${
                s.inventariado
                  ? `<p class="pista">Ya inventariado: volver a hacerlo sumaría las piezas otra vez, y se pedirá confirmación.</p>`
                  : ""
              }
            </div>`
          )
          .join("")
      : `<p class="cargando">No hay sets importados todavía.</p>`;
  } catch (e) {
    caja.innerHTML = `<p class="cargando">${esc(e.message)}</p>`;
  }
};

/** Inventaría un set. Si ya lo estaba, la API responde 409 y hay que confirmar. */
async function inventariarSet(numero, veces, ubicacion, confirmado = false) {
  try {
    const r = await api(`/api/sets/${encodeURIComponent(numero)}/inventariar`, {
      method: "POST",
      body: JSON.stringify({ veces, ubicacion, confirmar: confirmado }),
    });
    avisar(
      r.repetido
        ? `${r.piezas_anadidas} piezas añadidas OTRA VEZ (copia adicional del set).`
        : `${r.piezas_anadidas} piezas añadidas al inventario.`
    );
    cargadores.sets();
  } catch (e) {
    if (e.estado === 409 && e.detalle?.requiere_confirmacion) {
      const d = e.detalle.detalle || {};
      const cuando = d.ultima_vez
        ? ` La última vez fue el ${new Date(d.ultima_vez).toLocaleDateString("es-ES")}.`
        : "";
      const aceptado = await confirmar({
        titulo: "Este set ya está inventariado",
        texto: e.message,
        detalle: `Ya se sumaron <b>${d.piezas_anadidas ?? "?"}</b> piezas de ${esc(
          d.set || numero
        )}.${cuando} Continúa sólo si tienes otra copia física del set.`,
        aceptar: "Sí, tengo otra copia",
      });
      if (aceptado) await inventariarSet(numero, veces, ubicacion, true);
      else avisar("Inventariado cancelado: no se ha tocado nada.");
      return;
    }
    avisar(e.message, "error");
  }
}

$("#sets-lista").addEventListener("click", async (evento) => {
  const boton = evento.target.closest("button.inventariar");
  if (!boton) return;
  const tarjeta = boton.closest(".tarjeta");
  await inventariarSet(
    boton.dataset.set,
    Number(tarjeta.querySelector(".veces-set").value) || 1,
    tarjeta.querySelector(".ubi-set").value.trim()
  );
});

async function importarCSV(contenido) {
  if (!contenido.trim()) return avisar("No hay contenido CSV que importar.", "error");
  try {
    const r = await api("/api/sets/importar", {
      method: "POST",
      body: JSON.stringify({
        contenido_csv: contenido,
        numero_set: $("#csv-numero").value.trim() || null,
        nombre_set: $("#csv-nombre").value.trim() || null,
      }),
    });
    avisar(`Set ${r.set} importado: ${r.piezas_totales} piezas en ${r.lineas_procesadas} líneas.`);
    $("#csv-texto").value = "";
    cargadores.sets();
  } catch (e) {
    avisar(e.message, "error");
  }
}

$("#csv-importar").addEventListener("click", () => importarCSV($("#csv-texto").value));

const zona = $("#zona-csv");
["dragenter", "dragover"].forEach((ev) =>
  zona.addEventListener(ev, (e) => {
    e.preventDefault();
    zona.classList.add("encima");
  })
);
["dragleave", "drop"].forEach((ev) =>
  zona.addEventListener(ev, (e) => {
    e.preventDefault();
    zona.classList.remove("encima");
  })
);
zona.addEventListener("drop", async (evento) => {
  const fichero = evento.dataTransfer.files[0];
  if (fichero) importarCSV(await fichero.text());
});

// --------------------------------------------------------------------------
// Montajes
// --------------------------------------------------------------------------
let montajeActual = null;

cargadores.montajes = async () => {
  const caja = $("#montajes-lista");
  caja.innerHTML = `<p class="cargando">Cargando…</p>`;
  try {
    const d = await api("/api/montajes");
    caja.innerHTML = d.montajes.length
      ? d.montajes
          .map(
            (m) => `<div class="tarjeta pulsable ${
              m.id === montajeActual ? "seleccionada" : ""
            }" data-montaje="${m.id}">
              <div class="titulo">
                <span>${esc(m.nombre)}</span>
                <span class="insignia">${esc(m.estado)}</span>
              </div>
              <div class="detalle">${m.num_pasos} pasos · ${m.piezas_totales} piezas</div>
            </div>`
          )
          .join("")
      : `<p class="cargando">Aún no hay montajes. Créalos aquí o desde el chat.</p>`;

    // Sin selección previa, abrimos el primero: es más útil que un panel vacío.
    const seleccion =
      montajeActual && d.montajes.some((m) => m.id === montajeActual)
        ? montajeActual
        : d.montajes[0]?.id;
    if (seleccion) {
      $$("#montajes-lista .tarjeta").forEach((t) =>
        t.classList.toggle("seleccionada", Number(t.dataset.montaje) === seleccion)
      );
      mostrarMontaje(seleccion);
    }
  } catch (e) {
    caja.innerHTML = `<p class="cargando">${esc(e.message)}</p>`;
  }
};

async function mostrarMontaje(id) {
  montajeActual = id;
  const caja = $("#montaje-detalle");
  caja.classList.remove("vacio");
  try {
    const [m, v] = await Promise.all([
      api(`/api/montajes/${id}`),
      api(`/api/montajes/${id}/comprobar`),
    ]);
    const pasos = m.pasos.length
      ? m.pasos
          .map(
            (p) => `<div class="paso ${p.estado === "hecho" ? "hecho" : ""}">
              <div class="cabeza">
                <div>
                  <span class="num">Paso ${p.posicion}</span>
                  <div><strong>${esc(p.titulo)}</strong></div>
                </div>
                <div style="display:flex;gap:6px">
                  <button class="pequeno alternar" data-paso="${p.id}" data-estado="${
                    p.estado === "hecho" ? "pendiente" : "hecho"
                  }">${p.estado === "hecho" ? "Deshacer" : "Hecho"}</button>
                  <button class="pequeno peligro borrar-paso" data-paso="${p.id}">✕</button>
                </div>
              </div>
              ${p.instruccion ? `<div class="detalle">${esc(p.instruccion)}</div>` : ""}
              ${
                p.piezas.length
                  ? `<div class="recuadro-piezas">${p.piezas
                      .map(
                        (pz) => `<div class="pieza-manual">
                          ${
                            pz.imagen
                              ? `<img src="${esc(pz.imagen)}" alt="${esc(pz.pieza)}" loading="lazy" />`
                              : `<div class="sin-imagen"></div>`
                          }
                          <div class="cantidad">${pz.cantidad}×</div>
                          <div>${esc(pz.pieza)}</div>
                          <div class="color">${esc(pz.color || "")}</div>
                        </div>`
                      )
                      .join("")}</div>`
                  : `<div class="detalle">Sin piezas declaradas.</div>`
              }
              <div class="detalle">${
                p.colocaciones
                  ? `${p.colocaciones} colocadas en el modelo 3D`
                  : "Todavía sin colocar en el modelo 3D"
              }</div>
            </div>`
          )
          .join("")
      : `<p class="pista">Este montaje no tiene pasos todavía. Añádelos desde el chat con <code>anadir_paso</code>.</p>`;

    caja.innerHTML = `
      <div class="titulo" style="margin-bottom:6px">
        <span style="font-size:17px">${esc(m.nombre)}</span>
        <span class="insignia ${v.viable ? "ok" : "error"}">${
          v.viable ? "Viable" : `Faltan ${v.faltantes.length}`
        }</span>
      </div>
      ${m.descripcion ? `<div class="detalle">${esc(m.descripcion)}</div>` : ""}
      <div class="detalle">${m.pasos_hechos}/${m.num_pasos} pasos hechos · ${
        m.piezas_totales
      } piezas · estado: ${esc(m.estado)}</div>
      ${
        v.faltantes.length
          ? `<div class="pista">Faltan: ${v.faltantes
              .map((f) => `${esc(f.pieza)} (${esc(f.color)}) ×${f.faltan}`)
              .join(", ")}</div>`
          : ""
      }
      <div class="fila" style="margin-bottom:12px">
        <select id="estado-montaje">
          ${["planificado", "en_progreso", "completado", "desmontado"]
            .map(
              (e) => `<option value="${e}" ${e === m.estado ? "selected" : ""}>${e}</option>`
            )
            .join("")}
        </select>
        <button class="primario pequeno" id="abrir-disenador">Diseñar en 3D</button>
        <button class="pequeno" id="abrir-instrucciones">Ver instrucciones</button>
        <button class="pequeno peligro" id="borrar-montaje">Eliminar montaje</button>
      </div>
      ${pasos}`;
  } catch (e) {
    caja.innerHTML = `<p class="cargando">${esc(e.message)}</p>`;
  }
}

$("#montajes-lista").addEventListener("click", (evento) => {
  const tarjeta = evento.target.closest("[data-montaje]");
  if (!tarjeta) return;
  $$("#montajes-lista .tarjeta").forEach((t) => t.classList.toggle("seleccionada", t === tarjeta));
  mostrarMontaje(Number(tarjeta.dataset.montaje));
});

$("#montaje-detalle").addEventListener("click", async (evento) => {
  const alternar = evento.target.closest("button.alternar");
  const borrarPaso = evento.target.closest("button.borrar-paso");
  const borrarMontaje = evento.target.closest("#borrar-montaje");
  const disenar = evento.target.closest("#abrir-disenador");
  const instrucciones = evento.target.closest("#abrir-instrucciones");
  if (disenar || instrucciones) {
    window.abrirDisenador?.(montajeActual, instrucciones ? "instrucciones" : "disenar");
    return;
  }
  try {
    if (alternar) {
      await api(`/api/pasos/${alternar.dataset.paso}/estado`, {
        method: "POST",
        body: JSON.stringify({ estado: alternar.dataset.estado }),
      });
      mostrarMontaje(montajeActual);
    } else if (borrarPaso) {
      await api(`/api/pasos/${borrarPaso.dataset.paso}`, { method: "DELETE" });
      avisar("Paso eliminado.");
      mostrarMontaje(montajeActual);
    } else if (borrarMontaje) {
      if (!confirm("¿Eliminar este montaje y liberar sus piezas?")) return;
      await api(`/api/montajes/${montajeActual}`, { method: "DELETE" });
      montajeActual = null;
      $("#montaje-detalle").className = "panel vacio";
      $("#montaje-detalle").textContent = "Selecciona un montaje para ver sus pasos.";
      avisar("Montaje eliminado.");
      cargadores.montajes();
    }
  } catch (e) {
    avisar(e.message, "error");
  }
});

$("#montaje-detalle").addEventListener("change", async (evento) => {
  if (evento.target.id !== "estado-montaje") return;
  try {
    await api(`/api/montajes/${montajeActual}/estado`, {
      method: "POST",
      body: JSON.stringify({ estado: evento.target.value }),
    });
    avisar(`Montaje marcado como ${evento.target.value}.`);
    cargadores.montajes();
  } catch (e) {
    avisar(e.message, "error");
  }
});

$("#mont-crear").addEventListener("click", async () => {
  const nombre = $("#mont-nombre").value.trim();
  if (!nombre) return avisar("Ponle un nombre al montaje.", "error");
  try {
    const m = await api("/api/montajes", {
      method: "POST",
      body: JSON.stringify({ nombre, descripcion: $("#mont-desc").value.trim() || null }),
    });
    $("#mont-nombre").value = "";
    $("#mont-desc").value = "";
    montajeActual = m.id;
    avisar("Montaje creado. Añade los pasos desde el chat.");
    cargadores.montajes();
  } catch (e) {
    avisar(e.message, "error");
  }
});

// --------------------------------------------------------------------------
// Reconocimientos por foto
// --------------------------------------------------------------------------
cargadores.fotos = async () => {
  const caja = $("#fotos-lista");
  caja.innerHTML = `<p class="cargando">Cargando…</p>`;
  try {
    const d = await api("/api/reconocimientos");
    if (!d.sesiones.length) {
      caja.innerHTML = `<p class="cargando">Todavía no hay lotes. Enséñale una foto al chat y pídele que la inventaríe.</p>`;
      return;
    }
    caja.innerHTML = d.sesiones
      .map(
        (s) => `<div class="tarjeta">
          <div class="titulo">
            <span>Lote #${s.sesion_id} · ${esc(s.origen)}</span>
            <span class="insignia ${
              s.estado === "aplicada" ? "ok" : s.estado === "descartada" ? "error" : "aviso"
            }">${esc(s.estado)}</span>
          </div>
          <div class="detalle">${s.lineas} líneas · ${s.pendientes} pendientes · ${new Date(
            s.creada
          ).toLocaleString("es-ES")}</div>
          ${
            s.imagen
              ? `<img src="${esc(s.imagen)}" alt="Foto del lote" style="max-height:150px;margin-top:8px;border-radius:6px" />`
              : ""
          }
          ${
            s.estado === "pendiente"
              ? `<div class="fila">
                   <input class="ubi-rec" placeholder="Ubicación (opcional)" />
                   <button class="primario pequeno confirmar-rec" data-sesion="${s.sesion_id}">Confirmar e inventariar</button>
                   <button class="pequeno peligro descartar-rec" data-sesion="${s.sesion_id}">Descartar</button>
                 </div>`
              : ""
          }
        </div>`
      )
      .join("");
  } catch (e) {
    caja.innerHTML = `<p class="cargando">${esc(e.message)}</p>`;
  }
};

$("#fotos-lista").addEventListener("click", async (evento) => {
  const botonConfirmar = evento.target.closest("button.confirmar-rec");
  const descartar = evento.target.closest("button.descartar-rec");
  if (!botonConfirmar && !descartar) return;
  const boton = botonConfirmar || descartar;
  const tarjeta = boton.closest(".tarjeta");
  try {
    if (botonConfirmar) {
      const r = await api(`/api/reconocimientos/${boton.dataset.sesion}/confirmar`, {
        method: "POST",
        body: JSON.stringify({
          ubicacion: tarjeta.querySelector(".ubi-rec").value.trim(),
          ignorar_pendientes: true,
        }),
      });
      avisar(`${r.piezas_anadidas} piezas añadidas al inventario.`);
    } else {
      await api(`/api/reconocimientos/${boton.dataset.sesion}/descartar`, { method: "POST" });
      avisar("Lote descartado.");
    }
    cargadores.fotos();
  } catch (e) {
    avisar(e.message, "error");
  }
});

// --------------------------------------------------------------------------
// MCP
// --------------------------------------------------------------------------
cargadores.mcp = () => {
  const base = location.origin;
  $("#cmd-http").textContent = `claude mcp add --transport http lego ${base}/mcp/`;
  $("#cmd-stdio").textContent = JSON.stringify(
    {
      mcpServers: {
        lego: {
          command: "P:\\aperalta\\lego\\.venv\\Scripts\\python.exe",
          args: ["P:\\aperalta\\lego\\backend\\lego_mcp.py"],
        },
      },
    },
    null,
    2
  );

  const herramientas = [
    "resumen_inventario", "buscar_piezas", "consultar_inventario", "listar_colores",
    "importar_set_csv", "inventariar_set", "listar_sets", "piezas_de_set",
    "ajustar_inventario", "alta_pieza_manual", "historial_movimientos",
    "registrar_piezas_detectadas", "ver_reconocimiento", "resolver_reconocimiento",
    "confirmar_reconocimiento", "listar_reconocimientos", "descartar_reconocimiento",
    "crear_montaje", "anadir_paso", "ver_montaje", "listar_montajes", "marcar_paso",
    "eliminar_paso", "cambiar_estado_montaje", "eliminar_montaje", "comprobar_montaje",
    "que_puedo_montar", "editar_paso",
    "colocar_piezas", "mover_pieza", "quitar_pieza", "vaciar_modelo", "ver_modelo",
    "instrucciones_montaje", "paleta_de_piezas", "configurar_placa",
  ];
  $("#mcp-tools").innerHTML = herramientas
    .map((t) => `<span class="etiqueta-tool">${t}</span>`)
    .join("");
};

// Arranque: respeta el hash de la URL (#inventario, #montajes...).
irA(location.hash.slice(1) || "resumen");
