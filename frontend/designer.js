/* Diseñador 3D: construir el modelo pieza a pieza y verlo como un manual.
 *
 * Habla con la misma API que el chat: colocar una pieza aquí y colocarla desde
 * el MCP acaban en la misma tabla. Lo que se dibuja no es una decoración, es el
 * estado real del montaje.
 *
 * Unidades del render (las de LEGO):
 *   1 stud = 1 unidad en X y Z · 1 placa = 0,4 unidades en Y (un ladrillo son 3).
 */
import * as THREE from "/static/vendor/three.module.min.js";
import * as edicion from "/static/edicion.js";

const STUD = 1;
const PLACA = 0.4;
// Holgura entre piezas contiguas: sin ella el z-fighting hace parpadear las caras.
const HOLGURA = 0.03;
const ALTO_STUD = 0.17;
const RADIO_STUD = 0.24;

const $ = (sel) => document.querySelector(sel);

// --------------------------------------------------------------------------
// Estado
// --------------------------------------------------------------------------
const estado = {
  montajeId: null,
  modelo: null,
  instrucciones: null,
  modo: "disenar",
  // Qué hace el clic sobre el lienzo. Es lo que convierte el visor en un
  // editor: la misma escena sirve para construir, corregir, pintar o borrar.
  herramienta: "colocar",
  pasoActivo: null,
  indicePaso: 0,
  piezaEnMano: null, // ficha de la paleta seleccionada
  rotacion: 0,
  // Placas que la persona suma o resta a la altura de apoyo calculada. Con él
  // se puede intentar meter una pieza más abajo de donde el motor la dejaría:
  // si no cabe, el servidor dice contra qué choca en vez de subirla en silencio.
  desnivel: 0,
  seleccion: [], // ids de las colocaciones seleccionadas
  portapapeles: [],
  // Hasta qué altura se dibuja el modelo (null = entero) y si se enseña sólo
  // esa capa: sin esto no hay forma de trabajar dentro de algo ya cerrado.
  capaMaxima: null,
  aislarCapa: false,
  paleta: [],
  filtroPaleta: "",
  iniciado: false,
};

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
  if (!respuesta.ok) {
    const detalle = datos?.detail;
    const error = new Error(
      typeof detalle === "string" ? detalle : detalle?.error || `Error ${respuesta.status}`
    );
    error.detalle = detalle;
    throw error;
  }
  return datos;
}

const esc = (v) =>
  v === null || v === undefined
    ? ""
    : String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function avisar(mensaje, tipo = "ok") {
  // app.js ya tiene el sitio donde se muestran los avisos; lo reutilizamos.
  const caja = $("#aviso");
  if (!caja) return;
  caja.textContent = mensaje;
  caja.className = `aviso ${tipo}`;
  clearTimeout(avisar._t);
  avisar._t = setTimeout(() => caja.classList.add("oculto"), 4200);
}

/** Ancho y fondo de una pieza ya girada. */
function huella(forma, rotacion) {
  return rotacion % 180 === 90 ? [forma.fondo, forma.ancho] : [forma.ancho, forma.fondo];
}

// --------------------------------------------------------------------------
// Escena
// --------------------------------------------------------------------------
let renderer, escena, camara, raycaster;
let grupoPlaca, grupoModelo, grupoFantasma, grupoMarcos;
let pendienteRender = false;

const COLOR_SELECCION = new THREE.Color("#f5c518");

const orbita = { theta: Math.PI * 0.25, phi: Math.PI * 0.32, radio: 42, objetivo: new THREE.Vector3() };
const raton = new THREE.Vector2();

const cacheGeometria = new Map();
const cacheMaterial = new Map();

// Geometría real de cada molde, tal y como la describe la biblioteca LDraw.
// design_id -> datos de la malla, o null si esa pieza no está en LDraw.
const mallas = new Map();
let reintentoMallas = null;

/** Pide al servidor las mallas que falten. Las que aún no estén se reintentan. */
async function asegurarMallas(designIds) {
  const pendientes = [...new Set(designIds.filter((d) => d && !mallas.has(d)))];
  if (!pendientes.length) return;
  try {
    const d = await api(`/api/piezas/mallas?design_ids=${encodeURIComponent(pendientes.join(","))}`);
    for (const design of pendientes) {
      mallas.set(design, d.mallas[design] || null);
    }
    // Lo que el servidor esté convirtiendo se vuelve a pedir en unos segundos:
    // mientras tanto la pieza se dibuja como la caja que ocupa.
    if (d.generando?.length && !reintentoMallas) {
      reintentoMallas = setTimeout(async () => {
        reintentoMallas = null;
        d.generando.forEach((design) => mallas.delete(design));
        await asegurarMallas(d.generando);
        dibujarModelo();
      }, 7000);
    }
  } catch {
    // Sin mallas se sigue dibujando: son una mejora, no un requisito.
    pendientes.forEach((design) => mallas.set(design, null));
  }
}

/** Geometría de un grupo de color de una pieza LDraw, cacheada. */
function geometriaLDraw(designId, indice, posiciones) {
  const clave = `ldraw:${designId}:${indice}`;
  if (cacheGeometria.has(clave)) return cacheGeometria.get(clave);
  const geometria = new THREE.BufferGeometry();
  geometria.setAttribute("position", new THREE.Float32BufferAttribute(posiciones, 3));
  geometria.computeVertexNormals();
  cacheGeometria.set(clave, geometria);
  return geometria;
}

function solicitarRender() {
  if (pendienteRender) return;
  pendienteRender = true;
  requestAnimationFrame(() => {
    pendienteRender = false;
    renderer.render(escena, camara);
  });
}

function iniciarEscena() {
  const lienzo = $("#dis-lienzo");
  renderer = new THREE.WebGLRenderer({ canvas: lienzo, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  escena = new THREE.Scene();
  escena.background = new THREE.Color("#0d1319");

  camara = new THREE.PerspectiveCamera(42, 1, 0.1, 500);

  escena.add(new THREE.HemisphereLight("#dceaff", "#1a2129", 1.05));
  const sol = new THREE.DirectionalLight("#ffffff", 1.5);
  sol.position.set(26, 40, 18);
  sol.castShadow = true;
  sol.shadow.mapSize.set(1024, 1024);
  const c = sol.shadow.camera;
  c.left = -40;
  c.right = 40;
  c.top = 40;
  c.bottom = -40;
  c.near = 1;
  c.far = 130;
  escena.add(sol);
  // Un relleno frío por detrás evita que las caras en sombra se vean planas.
  const relleno = new THREE.DirectionalLight("#9fc0ff", 0.35);
  relleno.position.set(-20, 14, -22);
  escena.add(relleno);

  grupoPlaca = new THREE.Group();
  grupoModelo = new THREE.Group();
  grupoFantasma = new THREE.Group();
  grupoMarcos = new THREE.Group();
  escena.add(grupoPlaca, grupoModelo, grupoFantasma, grupoMarcos);

  raycaster = new THREE.Raycaster();

  new ResizeObserver(redimensionar).observe(lienzo.parentElement);
  redimensionar();
  conectarRaton(lienzo);
  actualizarCamara();
}

function redimensionar() {
  const envoltorio = $("#dis-lienzo").parentElement;
  const ancho = Math.max(320, envoltorio.clientWidth);
  const alto = Math.max(320, envoltorio.clientHeight);
  renderer.setSize(ancho, alto, false);
  camara.aspect = ancho / alto;
  camara.updateProjectionMatrix();
  solicitarRender();
}

function actualizarCamara() {
  const { theta, phi, radio, objetivo } = orbita;
  camara.position.set(
    objetivo.x + radio * Math.sin(phi) * Math.cos(theta),
    objetivo.y + radio * Math.cos(phi),
    objetivo.z + radio * Math.sin(phi) * Math.sin(theta)
  );
  camara.lookAt(objetivo);
  solicitarRender();
}

function recentrar() {
  const placa = estado.modelo?.placa || { ancho: 32, fondo: 32 };
  const piezas = piezasVisibles();
  orbita.theta = Math.PI * 0.25;
  orbita.phi = Math.PI * 0.32;

  if (!piezas.length) {
    // Placa vacía: se encuadra la placa entera para saber dónde construir.
    orbita.objetivo.set(0, 0, 0);
    orbita.radio = Math.max(placa.ancho, placa.fondo) * 1.35;
    return actualizarCamara();
  }

  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity, alto = 0;
  for (const pieza of piezas) {
    const [ancho, fondo] = huella(pieza.forma, pieza.rotacion);
    x0 = Math.min(x0, pieza.x);
    x1 = Math.max(x1, pieza.x + ancho);
    z0 = Math.min(z0, pieza.z);
    z1 = Math.max(z1, pieza.z + fondo);
    alto = Math.max(alto, (pieza.y + pieza.forma.alto) * PLACA);
  }
  orbita.objetivo.set((x0 + x1) / 2 - placa.ancho / 2, alto / 2, (z0 + z1) / 2 - placa.fondo / 2);
  // 1,6 veces la mayor dimensión deja el modelo grande con un margen holgado
  // para el campo de visión de 42 grados de la cámara.
  orbita.radio = Math.max(9, Math.max(x1 - x0, z1 - z0, alto * 2) * 1.6);
  actualizarCamara();
}

/** Piezas que se están mostrando: todas, las de hasta el paso en curso, o las
 *  que deja pasar el filtro de capas. */
function piezasVisibles() {
  if (estado.modo === "instrucciones" && estado.instrucciones) {
    const hasta = Math.min(estado.indicePaso, estado.instrucciones.pasos.length - 1);
    return estado.instrucciones.pasos
      .slice(0, hasta + 1)
      .flatMap((p) => p.nuevas)
      .filter(visibleEnLaCapa);
  }
  return (estado.modelo?.piezas || []).filter(visibleEnLaCapa);
}

/** El filtro de capas. Un modelo cerrado tapa su propio interior: bajando el
 *  techo se llega a lo de dentro, que es donde suele estar el error. */
function visibleEnLaCapa(pieza) {
  if (estado.capaMaxima === null) return true;
  if (estado.aislarCapa) {
    return pieza.y <= estado.capaMaxima && estado.capaMaxima < pieza.y + pieza.forma.alto;
  }
  return pieza.y <= estado.capaMaxima;
}

// --------------------------------------------------------------------------
// Geometría de las piezas
// --------------------------------------------------------------------------
function geometriaCuerpo(forma) {
  const clave = `${forma.ancho}x${forma.fondo}x${forma.alto}:${forma.familia}:${forma.redonda}`;
  if (cacheGeometria.has(clave)) return cacheGeometria.get(clave);

  const ancho = forma.ancho * STUD - HOLGURA;
  const fondo = forma.fondo * STUD - HOLGURA;
  const alto = forma.alto * PLACA;
  let geometria;

  if (forma.redonda && forma.ancho === forma.fondo) {
    geometria = new THREE.CylinderGeometry(ancho / 2, ancho / 2, alto, 24);
  } else if (forma.familia === "slope") {
    // Rampa: perfil triangular extruido. Baja a lo largo de X.
    const perfil = new THREE.Shape();
    perfil.moveTo(-ancho / 2, -alto / 2);
    perfil.lineTo(ancho / 2, -alto / 2);
    perfil.lineTo(ancho / 2, alto / 2);
    perfil.lineTo(-ancho / 2, -alto / 2 + PLACA * 0.5);
    perfil.closePath();
    geometria = new THREE.ExtrudeGeometry(perfil, { depth: fondo, bevelEnabled: false });
    geometria.translate(0, 0, -fondo / 2);
  } else {
    geometria = new THREE.BoxGeometry(ancho, alto, fondo);
  }
  cacheGeometria.set(clave, geometria);
  return geometria;
}

function geometriaStud() {
  if (!cacheGeometria.has("stud")) {
    cacheGeometria.set("stud", new THREE.CylinderGeometry(RADIO_STUD, RADIO_STUD, ALTO_STUD, 14));
  }
  return cacheGeometria.get("stud");
}

function material(hex, variante = "normal", transparente = false, dobleCara = false) {
  const color = hex || "#9aa5b1";
  const clave = `${color}:${variante}:${transparente}:${dobleCara}`;
  if (cacheMaterial.has(clave)) return cacheMaterial.get(clave);

  let mat;
  if (variante === "atenuada") {
    // Lo ya montado se ve, pero no compite con la pieza del paso.
    mat = new THREE.MeshLambertMaterial({
      color: new THREE.Color(color).lerp(new THREE.Color("#5b656f"), 0.75),
      transparent: true,
      opacity: 0.62,
    });
  } else if (variante === "fantasma-ok" || variante === "fantasma-mal") {
    mat = new THREE.MeshLambertMaterial({
      color: variante === "fantasma-ok" ? new THREE.Color(color) : new THREE.Color("#ff5555"),
      transparent: true,
      opacity: 0.55,
    });
  } else {
    mat = new THREE.MeshLambertMaterial({
      color: new THREE.Color(color),
      transparent: transparente,
      opacity: transparente ? 0.65 : 1,
    });
  }
  // Las piezas de LDraw no siempre traen las caras orientadas igual; pintar
  // por las dos evita agujeros sin tener que interpretar su BFC.
  if (dobleCara) mat.side = THREE.DoubleSide;
  cacheMaterial.set(clave, mat);
  return mat;
}

/** Construye la representación de una pieza colocada.
 *
 * Si el molde está en LDraw se dibuja su geometría real; si no, la caja que
 * ocupa, que al menos respeta el sitio y el color.
 */
function mallaPieza(pieza, variante = "normal") {
  const datos = mallas.get(pieza.design_id);
  return datos ? piezaReal(pieza, datos, variante) : piezaAproximada(pieza, variante);
}

/** Pieza dibujada con su geometría de verdad. */
function piezaReal(pieza, datos, variante) {
  const forma = pieza.forma;
  const grupo = new THREE.Group();
  const interior = new THREE.Group();

  // LDraw y el nombre del catálogo no siempre coinciden en qué lado es el
  // ancho: si encaja mejor girada un cuarto de vuelta, se gira.
  // El giro y el corrimiento que casan la malla con su casilla los calcula el
  // servidor, que es quien los usa también para los choques: así lo que se ve
  // y lo que el motor considera ocupado son lo mismo.
  const ajuste = pieza.ajuste || { giro: 0, dx: 0, dz: 0 };
  interior.rotation.y = ajuste.giro ? Math.PI / 2 : 0;
  interior.position.set(ajuste.dx, 0, ajuste.dz);

  datos.grupos.forEach((sub, indice) => {
    // Un color propio del molde (un eje negro, una junta gris) se respeta;
    // el resto de la pieza va del color del elemento.
    const mat =
      sub.color && variante === "normal"
        ? material(sub.color, "normal", false, true)
        : material(pieza.color_hex, variante, pieza.transparente, true);
    const malla = new THREE.Mesh(geometriaLDraw(pieza.design_id, indice, sub.posiciones), mat);
    malla.castShadow = variante === "normal";
    malla.receiveShadow = variante === "normal";
    interior.add(malla);
  });
  grupo.add(interior);

  const [ancho, fondo] = huella(forma, pieza.rotacion);
  grupo.rotation.y = (-pieza.rotacion * Math.PI) / 180;
  // La malla viene apoyada en su base, así que la altura es directa.
  grupo.position.set(pieza.x + ancho / 2, pieza.y * PLACA, pieza.z + fondo / 2);
  grupo.userData = { colocacionId: pieza.id, pieza };
  return grupo;
}

/** Respaldo: la caja que ocupa la pieza, con sus tetones. */
function piezaAproximada(pieza, variante = "normal") {
  const forma = pieza.forma;
  const grupo = new THREE.Group();
  const mat = material(pieza.color_hex, variante, pieza.transparente);

  const cuerpo = new THREE.Mesh(geometriaCuerpo(forma), mat);
  cuerpo.castShadow = variante === "normal";
  cuerpo.receiveShadow = variante === "normal";
  grupo.add(cuerpo);

  // Los tetones: una sola malla instanciada por pieza.
  if (forma.studs && forma.familia !== "slope") {
    const total = forma.ancho * forma.fondo;
    const studs = new THREE.InstancedMesh(geometriaStud(), mat, total);
    studs.castShadow = variante === "normal";
    const matriz = new THREE.Matrix4();
    let i = 0;
    for (let dx = 0; dx < forma.ancho; dx++) {
      for (let dz = 0; dz < forma.fondo; dz++) {
        matriz.makeTranslation(
          dx + 0.5 - forma.ancho / 2,
          (forma.alto * PLACA) / 2 + ALTO_STUD / 2,
          dz + 0.5 - forma.fondo / 2
        );
        studs.setMatrixAt(i++, matriz);
      }
    }
    studs.instanceMatrix.needsUpdate = true;
    grupo.add(studs);
  }

  const [ancho, fondo] = huella(forma, pieza.rotacion);
  grupo.rotation.y = (-pieza.rotacion * Math.PI) / 180;
  grupo.position.set(
    pieza.x + ancho / 2,
    pieza.y * PLACA + (forma.alto * PLACA) / 2,
    pieza.z + fondo / 2
  );
  grupo.userData = { colocacionId: pieza.id, pieza };
  return grupo;
}

/** Placa base: la superficie y su retícula de tetones. */
function dibujarPlaca(ancho, fondo) {
  grupoPlaca.clear();
  const grosor = PLACA;
  const base = new THREE.Mesh(
    new THREE.BoxGeometry(ancho, grosor, fondo),
    new THREE.MeshLambertMaterial({ color: "#3f4b55" })
  );
  base.position.set(ancho / 2, -grosor / 2, fondo / 2);
  base.receiveShadow = true;
  grupoPlaca.add(base);

  const studs = new THREE.InstancedMesh(
    geometriaStud(),
    new THREE.MeshLambertMaterial({ color: "#4a5762" }),
    ancho * fondo
  );
  studs.receiveShadow = true;
  const matriz = new THREE.Matrix4();
  let i = 0;
  for (let x = 0; x < ancho; x++) {
    for (let z = 0; z < fondo; z++) {
      matriz.makeTranslation(x + 0.5, ALTO_STUD / 2 - 0.01, z + 0.5);
      studs.setMatrixAt(i++, matriz);
    }
  }
  studs.instanceMatrix.needsUpdate = true;
  grupoPlaca.add(studs);

  // Todo el mundo se centra en el origen para que la órbita sea cómoda.
  grupoPlaca.position.set(-ancho / 2, 0, -fondo / 2);
  grupoModelo.position.copy(grupoPlaca.position);
  grupoFantasma.position.copy(grupoPlaca.position);
}

// --------------------------------------------------------------------------
// Dibujo del modelo
// --------------------------------------------------------------------------
function dibujarModelo() {
  grupoModelo.clear();
  if (!estado.modelo) return solicitarRender();

  if (estado.modo === "instrucciones" && estado.instrucciones) {
    const pasos = estado.instrucciones.pasos;
    const hasta = Math.min(estado.indicePaso, pasos.length - 1);
    pasos.forEach((paso, indice) => {
      if (indice > hasta) return;
      paso.nuevas.filter(visibleEnLaCapa).forEach((pieza) => {
        grupoModelo.add(mallaPieza(pieza, indice === hasta ? "normal" : "atenuada"));
      });
    });
  } else {
    piezasVisibles().forEach((pieza) => grupoModelo.add(mallaPieza(pieza, "normal")));
  }
  resaltarSeleccion();
}

/** Recuadra en amarillo lo seleccionado, un marco por pieza.
 *
 * Se dibujan por encima de todo a propósito (`depthTest` desactivado): al
 * editar por dentro de un modelo, media selección queda detrás de otras piezas
 * y hay que seguir viéndola. */
function resaltarSeleccion() {
  grupoMarcos.clear();
  const elegidas = new Set(estado.seleccion);
  if (elegidas.size) {
    for (const objeto of grupoModelo.children) {
      if (!elegidas.has(objeto.userData.colocacionId)) continue;
      const marco = new THREE.Box3Helper(
        new THREE.Box3().setFromObject(objeto),
        COLOR_SELECCION
      );
      marco.material.depthTest = false;
      marco.renderOrder = 5;
      grupoMarcos.add(marco);
    }
  }
  solicitarRender();
}

/** Vista previa del grupo que se está arrastrando, ya en su destino. */
function fantasmaDeGrupo(piezas, dx, dy, dz, cabe) {
  grupoFantasma.clear();
  for (const pieza of piezas) {
    grupoFantasma.add(
      mallaPieza(
        { ...pieza, x: pieza.x + dx, y: pieza.y + dy, z: pieza.z + dz },
        cabe ? "fantasma-ok" : "fantasma-mal"
      )
    );
  }
  solicitarRender();
}

// --------------------------------------------------------------------------
// Colocación: fantasma y clic
// --------------------------------------------------------------------------
/** Altura a la que caería una pieza en (x,z), con las mismas reglas del backend. */
function girarCelda(i, j, ancho, fondo, rotacion) {
  if (rotacion === 90) return [fondo - 1 - j, i];
  if (rotacion === 180) return [ancho - 1 - i, fondo - 1 - j];
  if (rotacion === 270) return [j, ancho - 1 - i];
  return [i, j];
}

/** Casillas que ocuparía la pieza en mano puesta ahí, con su tramo de altura. */
function columnasDe(ficha, x, z, rotacion) {
  const { ancho, fondo } = ficha.forma;
  const salida = new Map();
  for (const [i, j, desde, hasta] of ficha.perfil || []) {
    const [gi, gj] = girarCelda(i, j, ancho, fondo, rotacion);
    salida.set(`${x + gi},${z + gj}`, [desde, hasta]);
  }
  return salida;
}

/** Altura a la que caería la pieza, con la misma regla que aplica el servidor:
 *  casilla a casilla, mirando el relieve real de lo que hay debajo. */
function alturaDeApoyo(columnas) {
  // La pieza baja hasta que su parte más baja toca algo: una casilla con la
  // base una placa más alta (la mitad trasera de una placa curva) pasa por
  // encima de una placa sin levantar la pieza.
  let altura = 0;
  for (const [clave, [desde]] of columnas) {
    const tope = estado.alturas?.get(clave) || 0;
    altura = Math.max(altura, tope - desde);
  }
  return altura;
}

/** Lanza el rayo del ratón hacia la escena. Lo usan todas las herramientas. */
function apuntar(evento) {
  const caja = $("#dis-lienzo").getBoundingClientRect();
  raton.x = ((evento.clientX - caja.left) / caja.width) * 2 - 1;
  raton.y = -((evento.clientY - caja.top) / caja.height) * 2 + 1;
  raycaster.setFromCamera(raton, camara);
}

/** Colocación que hay bajo el cursor, o null si se apunta al vacío. */
function piezaBajoCursor(evento) {
  apuntar(evento);
  const tocadas = raycaster.intersectObjects(grupoModelo.children, true);
  if (!tocadas.length) return null;
  let objeto = tocadas[0].object;
  while (objeto && objeto.userData.colocacionId === undefined) objeto = objeto.parent;
  return objeto?.userData.colocacionId ?? null;
}

/** Dónde corta el cursor un plano horizontal a `y` placas de altura.
 *
 * Es lo que convierte el ratón en un desplazamiento en studs al arrastrar un
 * grupo: se mide sobre el plano de la pieza que se ha agarrado, no sobre el
 * suelo, para que la pieza no se escape al mover la vista. */
function puntoEnPlano(evento, y) {
  apuntar(evento);
  const plano = new THREE.Plane(new THREE.Vector3(0, 1, 0), -y * PLACA);
  const punto = new THREE.Vector3();
  if (!raycaster.ray.intersectPlane(plano, punto)) return null;
  return punto.sub(grupoPlaca.position);
}

/** Casilla de la placa bajo el cursor, centrando la pieza en el puntero. */
function casillaBajoCursor(evento) {
  apuntar(evento);

  const ficha = estado.piezaEnMano;
  const forma = ficha?.forma;
  const [ancho, fondo] = forma ? huella(forma, estado.rotacion) : [1, 1];

  // Se prueba primero contra las piezas ya puestas: así se apila encima de
  // ellas en vez de atravesarlas.
  const tocadas = raycaster.intersectObjects(grupoModelo.children, true);
  let punto = null;
  if (tocadas.length) {
    punto = tocadas[0].point.clone();
    // Un pelo hacia dentro para que la casilla sea la de debajo del cursor.
    punto.addScaledVector(raycaster.ray.direction, 0.001);
  } else {
    const plano = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    punto = new THREE.Vector3();
    if (!raycaster.ray.intersectPlane(plano, punto)) return null;
  }

  const placa = estado.modelo.placa;
  const local = punto.clone().sub(grupoPlaca.position);
  const encajar = (valor, tam, limite) =>
    Math.max(0, Math.min(limite - tam, Math.round(valor - tam / 2)));
  const x = encajar(local.x, ancho, placa.ancho);
  const z = encajar(local.z, fondo, placa.fondo);

  // Casilla exacta a la que apunta el ratón: es la superficie que la persona
  // está señalando, y sobre la que espera que la pieza se apoye entera.
  const señalada = {
    x: Math.max(0, Math.min(placa.ancho - 1, Math.floor(local.x))),
    z: Math.max(0, Math.min(placa.fondo - 1, Math.floor(local.z))),
  };
  return alinearConLaSuperficie({ x, z }, señalada, ancho, fondo, placa);
}

/** Altura de apoyo de una casilla concreta del tablero. */
function nivelDeCasilla(x, z) {
  return estado.alturas?.get(`${x},${z}`) || 0;
}

/** Corre la pieza como mucho una casilla para que no quede a caballo.
 *
 * Centrada en el cursor, una pieza que se señala junto al borde de otra acaba
 * medio encima y medio en el aire, y el motor la sube al nivel más alto. Aquí
 * se prefiere la posición en la que toda la pieza descansa sobre la misma
 * superficie que se está señalando.
 */
function alinearConLaSuperficie(centrada, señalada, ancho, fondo, placa) {
  const nivelObjetivo = nivelDeCasilla(señalada.x, señalada.z);
  let mejor = centrada;
  let mejorPuntos = -1;

  for (const dx of [0, -1, 1]) {
    for (const dz of [0, -1, 1]) {
      const x = centrada.x + dx;
      const z = centrada.z + dz;
      if (x < 0 || z < 0 || x + ancho > placa.ancho || z + fondo > placa.fondo) continue;
      // La pieza tiene que seguir estando bajo el cursor.
      if (señalada.x < x || señalada.x >= x + ancho) continue;
      if (señalada.z < z || señalada.z >= z + fondo) continue;

      let iguales = 0;
      for (let i = 0; i < ancho; i++) {
        for (let j = 0; j < fondo; j++) {
          if (nivelDeCasilla(x + i, z + j) === nivelObjetivo) iguales++;
        }
      }
      // A igualdad de casillas al mismo nivel, se respeta el centrado.
      const puntos = iguales * 10 - Math.abs(dx) - Math.abs(dz);
      if (puntos > mejorPuntos) {
        mejorPuntos = puntos;
        mejor = { x, z };
      }
    }
  }
  return mejor;
}

function actualizarFantasma(evento) {
  grupoFantasma.clear();
  const ficha = estado.piezaEnMano;
  if (!ficha || estado.modo !== "disenar" || !estado.modelo) return solicitarRender();

  const casilla = casillaBajoCursor(evento);
  if (!casilla) return solicitarRender();

  const columnas = columnasDe(ficha, casilla.x, casilla.z, estado.rotacion);
  const apoyo = alturaDeApoyo(columnas);
  const y = Math.max(0, apoyo + estado.desnivel);
  // Si la persona la ha bajado a mano y ahí no cabe, el fantasma se pone rojo:
  // es la misma comprobación que hará el servidor, casilla a casilla.
  const cabe = !chocaConElModelo(columnas, y);
  estado.previsualizacion = { apoyo, y, cabe };
  const previa = {
    id: -1,
    x: casilla.x,
    z: casilla.z,
    y,
    rotacion: estado.rotacion,
    forma: ficha.forma,
    design_id: ficha.design_id,
    ajuste: ficha.ajuste,
    color_hex: ficha.color_hex,
    transparente: false,
  };
  grupoFantasma.add(mallaPieza(previa, cabe ? "fantasma-ok" : "fantasma-mal"));
  edicion.pintarPanel();
  solicitarRender();
}

/** ¿La pieza en mano, puesta a esa altura, se cruza con algo del modelo? */
function chocaConElModelo(columnas, y) {
  for (const otra of estado.modelo?.piezas || []) {
    for (const [cx, cz, desde, hasta] of otra.columnas || []) {
      const tramo = columnas.get(`${cx},${cz}`);
      if (!tramo) continue;
      const [d, h] = [y + tramo[0], y + tramo[1]];
      if (!(h <= desde || hasta <= d)) return true;
    }
  }
  return false;
}

async function colocarEnCursor(evento) {
  const ficha = estado.piezaEnMano;
  const casilla = casillaBajoCursor(evento);
  if (!ficha || !casilla) return;
  try {
    const r = await api(`/api/montajes/${estado.montajeId}/piezas`, {
      method: "POST",
      body: JSON.stringify({
        piezas: [
          {
            element_id: ficha.element_id,
            x: casilla.x,
            z: casilla.z,
            rotacion: estado.rotacion,
            // Sólo se fija la altura si la persona la ha ajustado a mano; si
            // no, el servidor la apoya sobre lo que haya debajo.
            ...(estado.desnivel ? { y: estado.previsualizacion?.y ?? 0 } : {}),
          },
        ],
        paso_id: estado.pasoActivo,
      }),
    });
    const puesta = r.detalle?.[0];
    if (r.avisos?.length) {
      avisar(r.avisos[0], "error");
    } else if (puesta && puesta.y > 0) {
      // Que se sepa por qué ha subido: descansa sobre otra pieza.
      const sobre = puesta.apoyada_sobre.map((n) => n.replace(/ \(colocación \d+\)/, "")).join(", ");
      avisar(`Colocada a ${puesta.y} placa${puesta.y === 1 ? "" : "s"}, apoyada sobre ${sobre || "otra pieza"}.`);
    }
    await recargar({ conservarVista: true });
  } catch (e) {
    avisar(e.message, "error");
  }
}

/** Qué hace un clic en el lienzo. Cada herramienta responde a lo suyo. */
function clicEnLienzo(evento) {
  const sumar = evento.ctrlKey || evento.metaKey || evento.shiftKey;
  const id = piezaBajoCursor(evento);

  switch (estado.herramienta) {
    case "colocar":
      // Sin pieza en mano, el pincel no tiene qué poner: se comporta como el
      // puntero de siempre y selecciona.
      if (estado.piezaEnMano) return colocarEnCursor(evento);
      return edicion.seleccionar(id, { anadir: sumar });
    case "borrar":
      if (id === null) return;
      edicion.seleccionar(id);
      return edicion.quitar();
    case "pintar":
      return pintarPiezaDelCursor(id);
    case "cuentagotas":
      return cogerPiezaDelModelo(id);
    default:
      return edicion.seleccionar(id, { anadir: sumar });
  }
}

/** El bote de pintura usa el color de la pieza elegida en la paleta. */
function pintarPiezaDelCursor(id) {
  if (id === null) return;
  if (!estado.piezaEnMano) {
    return avisar("Elige en la paleta una pieza del color con el que quieres pintar.", "error");
  }
  edicion.seleccionar(id);
  return edicion.pintarDe({ color: estado.piezaEnMano.color });
}

/** Cuentagotas: coge en mano la pieza que ya está puesta, para repetirla. */
function cogerPiezaDelModelo(id) {
  const pieza = (estado.modelo?.piezas || []).find((p) => p.id === id);
  if (!pieza) return;
  const ficha = estado.paleta.find((f) => f.element_id === pieza.element_id);
  if (!ficha) {
    return avisar(`No te quedan ${pieza.pieza} (${pieza.color}) libres para poner más.`, "error");
  }
  estado.piezaEnMano = ficha;
  estado.rotacion = pieza.rotacion;
  estado.desnivel = 0;
  $("#dis-rotacion").textContent = `${estado.rotacion}°`;
  cambiarHerramienta("colocar");
  pintarPaleta();
  edicion.pintarPanel();
  avisar(`${ficha.pieza} (${ficha.color}) en mano: ${ficha.disponible} libres.`);
}

function quitarSeleccion() {
  if (estado.modo !== "disenar") return avisar("Cambia a «Diseñar» para modificar el modelo.", "error");
  if (!estado.seleccion.length) return avisar("No hay ninguna pieza seleccionada.", "error");
  return edicion.quitar();
}

/** Cambia la herramienta activa y deja el lienzo coherente con ella. */
function cambiarHerramienta(nombre) {
  estado.herramienta = nombre;
  if (nombre !== "colocar") {
    grupoFantasma.clear();
    solicitarRender();
  }
  const lienzo = $("#dis-lienzo");
  lienzo.className = `modo-${nombre}`;
  document
    .querySelectorAll("#dis-herramientas button")
    .forEach((b) => b.classList.toggle("activa", b.dataset.herramienta === nombre));
  pintarAyuda();
}

/** El pie del lienzo explica lo que se puede hacer ahora mismo. */
function pintarAyuda() {
  const comun =
    "Botón derecho: girar la vista · Rueda: zoom · <kbd>Mayús</kbd>+arrastrar: desplazar";
  const segun = {
    seleccionar:
      "Clic: elegir · <kbd>Ctrl</kbd>+clic: sumar · arrastrar en el vacío: encuadrar varias · " +
      "arrastrar una pieza: mover el grupo · <kbd>←→↑↓</kbd> mover · <kbd>R</kbd> girar · <kbd>Supr</kbd> quitar",
    colocar:
      "Clic: colocar la pieza en mano · <kbd>R</kbd> girar · <kbd>+</kbd>/<kbd>−</kbd> subir o bajar · <kbd>Esc</kbd> soltar",
    pintar: "Clic: pintar esa pieza del color de la que tengas elegida en la paleta",
    borrar: "Clic: quitar esa pieza del modelo (vuelve al inventario)",
    cuentagotas: "Clic: coger en mano la pieza señalada para repetirla",
  };
  const caja = $("#dis-ayuda");
  if (caja) caja.innerHTML = `${segun[estado.herramienta] || ""} · ${comun}`;
}

// --------------------------------------------------------------------------
// Ratón y teclado
// --------------------------------------------------------------------------
function conectarRaton(lienzo) {
  let arrastrando = false;
  let movido = false;
  // "orbitar" | "desplazar" | "mover-piezas" | "encuadrar"
  let modoArrastre = null;
  let ultimo = { x: 0, y: 0 };
  let inicio = { x: 0, y: 0 };
  let agarre = null; // qué piezas viajan con el ratón y desde dónde
  let recorrido = null; // desplazamiento en studs del arrastre en curso

  lienzo.addEventListener("contextmenu", (e) => e.preventDefault());

  lienzo.addEventListener("pointerdown", (evento) => {
    arrastrando = true;
    movido = false;
    inicio = ultimo = { x: evento.clientX, y: evento.clientY };
    lienzo.setPointerCapture(evento.pointerId);
    modoArrastre = null;
    agarre = null;
    recorrido = null;

    // La vista se maneja con el botón derecho (o el central, o con Mayús): el
    // izquierdo queda entero para trabajar sobre el modelo.
    if (evento.button !== 0) {
      modoArrastre = evento.button === 1 ? "desplazar" : "orbitar";
      return;
    }
    if (evento.shiftKey) return void (modoArrastre = "desplazar");
    if (estado.modo !== "disenar") return void (modoArrastre = "orbitar");
    if (estado.herramienta !== "seleccionar") return;

    const id = piezaBajoCursor(evento);
    if (id === null) {
      modoArrastre = "encuadrar";
      marcoEncuadre(inicio, inicio);
      return;
    }
    // Arrastrar una pieza mueve con ella todo lo seleccionado; si no estaba
    // elegida, pasa a estarlo antes de moverse.
    if (!estado.seleccion.includes(id)) {
      edicion.seleccionar(id, { anadir: evento.ctrlKey || evento.metaKey });
    }
    const piezas = (estado.modelo?.piezas || []).filter((p) => estado.seleccion.includes(p.id));
    const referencia = piezas.find((p) => p.id === id) || piezas[0];
    const punto = referencia ? puntoEnPlano(evento, referencia.y) : null;
    if (punto) {
      agarre = { piezas, y: referencia.y, punto };
      modoArrastre = "mover-piezas";
    }
  });

  lienzo.addEventListener("pointermove", (evento) => {
    if (!arrastrando) {
      estado.ultimoRaton = evento;
      if (estado.herramienta === "colocar") actualizarFantasma(evento);
      return;
    }
    // Un clic con un temblor de tres píxeles sigue siendo un clic.
    if (!movido && Math.hypot(evento.clientX - inicio.x, evento.clientY - inicio.y) < 4) return;
    movido = true;

    if (modoArrastre === "mover-piezas") {
      const punto = puntoEnPlano(evento, agarre.y);
      if (!punto) return;
      lienzo.classList.add("arrastrando-piezas");
      recorrido = {
        dx: Math.round(punto.x - agarre.punto.x),
        dz: Math.round(punto.z - agarre.punto.z),
      };
      const estorbo =
        recorrido.dx || recorrido.dz ? edicion.estorbo(recorrido.dx, 0, recorrido.dz) : null;
      fantasmaDeGrupo(agarre.piezas, recorrido.dx, 0, recorrido.dz, !estorbo);
      return;
    }
    if (modoArrastre === "encuadrar") {
      marcoEncuadre(inicio, { x: evento.clientX, y: evento.clientY });
      return;
    }
    // Con una pieza en mano, arrastrar sigue siendo colocar: girar la vista es
    // cosa del botón derecho.
    if (!modoArrastre && estado.piezaEnMano && estado.herramienta === "colocar") {
      actualizarFantasma(evento);
      return;
    }
    if (!modoArrastre) modoArrastre = "orbitar";

    lienzo.classList.add("girando");
    const dx = evento.clientX - ultimo.x;
    const dy = evento.clientY - ultimo.y;
    if (modoArrastre === "desplazar") {
      const escala = orbita.radio * 0.0016;
      const derecha = new THREE.Vector3().setFromMatrixColumn(camara.matrix, 0);
      const arriba = new THREE.Vector3().setFromMatrixColumn(camara.matrix, 1);
      orbita.objetivo.addScaledVector(derecha, -dx * escala);
      orbita.objetivo.addScaledVector(arriba, dy * escala);
    } else {
      orbita.theta -= dx * 0.007;
      orbita.phi = Math.max(0.12, Math.min(Math.PI / 2.05, orbita.phi - dy * 0.006));
    }
    ultimo = { x: evento.clientX, y: evento.clientY };
    actualizarCamara();
  });

  lienzo.addEventListener("pointerup", (evento) => {
    if (lienzo.hasPointerCapture(evento.pointerId)) lienzo.releasePointerCapture(evento.pointerId);
    lienzo.classList.remove("girando", "arrastrando-piezas");
    const eraClic = arrastrando && !movido;
    const modo = modoArrastre;
    const movimiento = recorrido;
    arrastrando = false;
    modoArrastre = null;
    agarre = null;
    recorrido = null;

    if (modo === "mover-piezas") {
      grupoFantasma.clear();
      solicitarRender();
      if (eraClic || !movimiento || (!movimiento.dx && !movimiento.dz)) return;
      const estorbo = edicion.estorbo(movimiento.dx, 0, movimiento.dz);
      if (estorbo) return avisar(`Ahí no cabe: ${estorbo}.`, "error");
      edicion.mover(movimiento.dx, 0, movimiento.dz);
      return;
    }
    if (modo === "encuadrar") {
      cerrarEncuadre(
        inicio,
        { x: evento.clientX, y: evento.clientY },
        evento.ctrlKey || evento.metaKey || evento.shiftKey
      );
      return;
    }
    if (!eraClic || evento.button !== 0) return;
    if (estado.modo !== "disenar") return;
    clicEnLienzo(evento);
  });

  lienzo.addEventListener(
    "wheel",
    (evento) => {
      evento.preventDefault();
      orbita.radio = Math.max(6, Math.min(160, orbita.radio * (evento.deltaY > 0 ? 1.1 : 0.91)));
      actualizarCamara();
    },
    { passive: false }
  );

  conectarTeclado();
}

/** El rectángulo de encuadre se dibuja encima del lienzo, no dentro de la
 *  escena: es una herramienta de la interfaz, no parte del modelo. */
function marcoEncuadre(a, b) {
  const marco = $("#dis-marco-seleccion");
  const caja = $("#dis-lienzo").getBoundingClientRect();
  marco.style.left = `${Math.min(a.x, b.x) - caja.left}px`;
  marco.style.top = `${Math.min(a.y, b.y) - caja.top}px`;
  marco.style.width = `${Math.abs(a.x - b.x)}px`;
  marco.style.height = `${Math.abs(a.y - b.y)}px`;
  marco.classList.remove("oculto");
}

/** Al soltar queda seleccionado todo lo que haya caído dentro del recuadro. */
function cerrarEncuadre(a, b, sumar) {
  $("#dis-marco-seleccion").classList.add("oculto");
  const caja = $("#dis-lienzo").getBoundingClientRect();
  const x0 = Math.min(a.x, b.x);
  const x1 = Math.max(a.x, b.x);
  const y0 = Math.min(a.y, b.y);
  const y1 = Math.max(a.y, b.y);
  // Un clic en el vacío no encuadra nada: lo que hace es soltar la selección.
  if (x1 - x0 < 5 && y1 - y0 < 5) return edicion.limpiar();

  const dentro = [];
  const centro = new THREE.Vector3();
  for (const objeto of grupoModelo.children) {
    new THREE.Box3().setFromObject(objeto).getCenter(centro);
    const proyectado = centro.clone().project(camara);
    const px = caja.left + ((proyectado.x + 1) / 2) * caja.width;
    const py = caja.top + ((1 - proyectado.y) / 2) * caja.height;
    if (px >= x0 && px <= x1 && py >= y0 && py <= y1) dentro.push(objeto.userData.colocacionId);
  }
  edicion.seleccionar(dentro, { anadir: sumar });
  avisar(`${dentro.length} pieza${dentro.length === 1 ? "" : "s"} en el encuadre.`);
}

/** Atajos del diseñador, pensados para no tener que soltar el ratón. */
function conectarTeclado() {
  window.addEventListener("keydown", (evento) => {
    if (!$("#vista-disenador").classList.contains("activa")) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(evento.target.tagName)) return;
    if (evento.ctrlKey || evento.metaKey) return atajosDeControl(evento);

    if (estado.modo === "instrucciones") {
      if (evento.key === "ArrowRight") irAPaso(estado.indicePaso + 1);
      else if (evento.key === "ArrowLeft") irAPaso(estado.indicePaso - 1);
      return;
    }

    const herramientas = {
      1: "seleccionar",
      2: "colocar",
      3: "pintar",
      4: "borrar",
      5: "cuentagotas",
    };
    if (herramientas[evento.key]) return cambiarHerramienta(herramientas[evento.key]);

    switch (evento.key) {
      case "r":
      case "R":
        return girarPieza();
      case "+":
      case "PageUp":
        return ajustarAltura(1);
      case "-":
      case "PageDown":
        return ajustarAltura(-1);
      case "Escape":
        return estado.piezaEnMano ? soltarPieza() : edicion.limpiar();
      case "Delete":
      case "Backspace":
        evento.preventDefault();
        return quitarSeleccion();
      case "ArrowUp":
      case "ArrowDown":
      case "ArrowLeft":
      case "ArrowRight": {
        if (!estado.seleccion.length) return;
        evento.preventDefault();
        const [dx, dz] = direccionEnPantalla(evento.key);
        return edicion.mover(dx, 0, dz);
      }
      default:
        return;
    }
  });
}

function atajosDeControl(evento) {
  const acciones = {
    z: () => (evento.shiftKey ? edicion.rehacer() : edicion.deshacer()),
    y: () => edicion.rehacer(),
    c: () => edicion.copiar(),
    v: () => edicion.pegar(),
    d: () => edicion.duplicar(1, 0, 0),
    a: () => edicion.seleccionarTodo(),
  };
  const accion = acciones[evento.key.toLowerCase()];
  if (!accion) return;
  evento.preventDefault();
  accion();
}

/** Traduce una flecha a un movimiento en la rejilla según hacia dónde mire la
 *  cámara: «derecha» es siempre la derecha de la pantalla, se haya girado la
 *  vista como se haya girado. */
function direccionEnPantalla(tecla) {
  const derecha = new THREE.Vector3().setFromMatrixColumn(camara.matrix, 0);
  derecha.y = 0;
  derecha.normalize();
  const adentro = new THREE.Vector3();
  camara.getWorldDirection(adentro);
  adentro.y = 0;
  adentro.normalize();

  const vector = {
    ArrowRight: derecha,
    ArrowLeft: derecha.clone().negate(),
    ArrowUp: adentro,
    ArrowDown: adentro.clone().negate(),
  }[tecla];
  // Un eje cada vez: en diagonal manda el dominante.
  return Math.abs(vector.x) >= Math.abs(vector.z)
    ? [Math.sign(vector.x), 0]
    : [0, Math.sign(vector.z)];
}

/** R: gira la pieza en mano; sin pieza en mano, gira el grupo seleccionado. */
function girarPieza() {
  if (!estado.piezaEnMano) {
    if (estado.seleccion.length) edicion.girar();
    return;
  }
  estado.rotacion = (estado.rotacion + 90) % 360;
  $("#dis-rotacion").textContent = `${estado.rotacion}°`;
  edicion.pintarPanel();
  if (estado.ultimoRaton) actualizarFantasma(estado.ultimoRaton);
}

/** +/−: sube o baja la pieza en mano, o la selección, una placa. */
function ajustarAltura(delta) {
  if (!estado.piezaEnMano) {
    if (estado.seleccion.length) edicion.mover(0, delta, 0);
    return;
  }
  estado.desnivel += delta;
  // Por debajo del suelo no hay nada que hacer; por encima se deja libertad.
  const apoyo = estado.previsualizacion?.apoyo ?? 0;
  if (apoyo + estado.desnivel < 0) estado.desnivel = -apoyo;
  edicion.pintarPanel();
  if (estado.ultimoRaton) actualizarFantasma(estado.ultimoRaton);
}

function soltarPieza() {
  estado.piezaEnMano = null;
  estado.desnivel = 0;
  grupoFantasma.clear();
  document.querySelectorAll("#dis-paleta .ficha").forEach((f) => f.classList.remove("activa"));
  edicion.pintarPanel();
  solicitarRender();
}

// --------------------------------------------------------------------------
// Paneles
// --------------------------------------------------------------------------
function pintarPaleta() {
  const caja = $("#dis-paleta");
  const filtro = estado.filtroPaleta.toLowerCase();
  const visibles = estado.paleta.filter(
    (p) =>
      !filtro ||
      `${p.pieza} ${p.color} ${p.element_id}`.toLowerCase().includes(filtro)
  );
  if (!visibles.length) {
    caja.innerHTML = `<p class="pista">Sin piezas disponibles. Inventaría un set o revisa el filtro.</p>`;
    return;
  }
  caja.innerHTML = visibles
    .slice(0, 120)
    .map((p) => {
      const forma = p.forma;
      const img = p.imagen
        ? `<img src="${esc(p.imagen)}" alt="${esc(p.pieza)}" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'sin-imagen',textContent:'sin foto'}))" />`
        : `<div class="sin-imagen">sin imagen</div>`;
      return `<div class="ficha ${
        estado.piezaEnMano?.element_id === p.element_id ? "activa" : ""
      }" data-element="${esc(p.element_id)}" title="${esc(p.pieza)} · ${esc(p.color)}">
        ${img}
        <div class="nom">${esc(p.pieza)}</div>
        <div class="dat">
          <span class="punto" style="background:${esc(p.color_hex || "#666")}"></span>
          ${forma.ancho}×${forma.fondo}${forma.alto !== 1 ? `×${forma.alto}p` : ""}
          ${p.malla_3d ? "" : `<span class="aprox" title="Sin geometría en LDraw: se dibuja como la caja que ocupa">≈</span>`}
        </div>
        <div class="dat"><b>${p.disponible}</b> libres</div>
      </div>`;
    })
    .join("");
}

function pintarPasos() {
  const caja = $("#dis-pasos");
  const pasos = estado.modelo?.pasos || [];
  if (!pasos.length) {
    caja.innerHTML = `<p class="pista">Todavía no hay pasos. Crea el primero para empezar a colocar piezas.</p>`;
    return;
  }
  caja.innerHTML = pasos
    .map(
      (p) => `<div class="paso-item ${p.id === estado.pasoActivo ? "activo" : ""}" data-paso="${p.id}">
        <span>${p.posicion}. ${esc(p.titulo)}</span>
        <span class="cuenta">${p.colocaciones} pzs
          <button class="pequeno" data-piezas-de="${p.id}"
            title="Seleccionar las piezas de este paso">◎</button></span>
      </div>`
    )
    .join("");
}

function pintarInstruccion() {
  const datos = estado.instrucciones;
  const caja = $("#ins-detalle");
  if (!datos || !datos.pasos.length) {
    $("#ins-indicador").textContent = "Sin pasos";
    caja.innerHTML = `<p class="pista">Este montaje aún no tiene pasos con piezas colocadas. Constrúyelo en el modo Diseñar o pídeselo al chat.</p>`;
    return;
  }
  const indice = Math.max(0, Math.min(estado.indicePaso, datos.pasos.length - 1));
  const paso = datos.pasos[indice];
  $("#ins-indicador").textContent = `Paso ${indice + 1} de ${datos.pasos.length}`;

  // El recuadro de piezas junta las colocadas en 3D y las que el paso declara
  // pero todavía no tienen sitio: un montaje escrito desde el chat también
  // merece unas instrucciones con fotos.
  const delPaso = [
    ...paso.piezas_del_paso.map((p) => ({ ...p, colocada: true })),
    ...paso.sin_colocar.map((p) => ({ ...p, colocada: false })),
  ];
  const recuadro = delPaso.length
    ? `<div class="recuadro-piezas">${delPaso
        .map(
          (p) => `<div class="pieza-manual ${p.colocada ? "" : "sin-sitio"}">
            ${p.imagen ? `<img src="${esc(p.imagen)}" alt="${esc(p.pieza)}" loading="lazy"
                 onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'sin-imagen'}))" />`
                       : `<div class="sin-imagen"></div>`}
            <div class="cantidad">${p.cantidad}×</div>
            <div>${esc(p.pieza)}</div>
            <div class="color">${esc(p.color)}</div>
            ${p.colocada ? "" : `<div class="color aviso-sitio">sin colocar</div>`}
          </div>`
        )
        .join("")}</div>`
    : `<p class="pista">Este paso no declara piezas.</p>`;

  const pendientes = paso.sin_colocar.length
    ? `<p class="pista">Hay piezas sin sitio en el modelo. Colócalas en el modo
       «Diseñar» (o pídeselo al chat) para que se vea dónde van.</p>`
    : "";

  const donde = paso.nuevas.length
    ? `<h3 style="margin-top:12px">Dónde van</h3>
       <div class="coordenadas">${paso.nuevas
         .map((p) => `${esc(p.pieza)} → x ${p.x}, y ${p.y}, z ${p.z}${p.rotacion ? `, ${p.rotacion}°` : ""}`)
         .join("<br>")}</div>`
    : "";

  caja.innerHTML = `
    <div class="paso-actual">
      <h3>${esc(paso.titulo)}</h3>
      ${paso.instruccion ? `<p class="pista" style="margin-top:2px">${esc(paso.instruccion)}</p>` : ""}
      <p class="pista" style="margin-top:2px">${paso.piezas_previas} piezas ya montadas (en gris) · ${paso.nuevas.length} nuevas</p>
      ${recuadro}
      ${pendientes}
      <div class="fila">
        <button class="pequeno ${paso.estado === "hecho" ? "" : "primario"}" id="ins-marcar"
          data-paso="${paso.id}" data-estado="${paso.estado === "hecho" ? "pendiente" : "hecho"}">
          ${paso.estado === "hecho" ? "Marcar pendiente" : "Marcar hecho"}
        </button>
      </div>
      ${donde}
    </div>`;

  $("#ins-marcar")?.addEventListener("click", async (evento) => {
    const boton = evento.currentTarget;
    try {
      await api(`/api/pasos/${boton.dataset.paso}/estado`, {
        method: "POST",
        body: JSON.stringify({ estado: boton.dataset.estado }),
      });
      await recargar({ conservarVista: true });
    } catch (e) {
      avisar(e.message, "error");
    }
  });
}

function irAPaso(indice) {
  const total = estado.instrucciones?.pasos.length || 0;
  if (!total) return;
  estado.indicePaso = Math.max(0, Math.min(indice, total - 1));
  pintarInstruccion();
  dibujarModelo();
  recentrar();
}

// --------------------------------------------------------------------------
// Carga
// --------------------------------------------------------------------------
async function cargarMontajes() {
  const select = $("#dis-montaje");
  try {
    const d = await api("/api/montajes");
    if (!d.montajes.length) {
      select.innerHTML = `<option value="">No hay montajes</option>`;
      $("#dis-estado").textContent = "Crea un montaje en la pestaña Montajes para empezar.";
      return [];
    }
    select.innerHTML = d.montajes
      .map((m) => `<option value="${m.id}">${esc(m.nombre)} · ${m.colocaciones} pzs</option>`)
      .join("");
    if (!estado.montajeId || !d.montajes.some((m) => m.id === estado.montajeId)) {
      estado.montajeId = d.montajes[0].id;
    }
    select.value = String(estado.montajeId);
    return d.montajes;
  } catch (e) {
    avisar(e.message, "error");
    return [];
  }
}

async function recargar({ conservarVista = false } = {}) {
  if (!estado.montajeId) return;
  $("#dis-cargando").classList.remove("oculto");
  try {
    const [modelo, paleta, instrucciones] = await Promise.all([
      api(`/api/montajes/${estado.montajeId}/modelo`),
      api(`/api/montajes/${estado.montajeId}/paleta?limite=200`),
      api(`/api/montajes/${estado.montajeId}/instrucciones`),
    ]);
    estado.modelo = modelo;
    estado.paleta = paleta.piezas;
    estado.instrucciones = instrucciones;

    // El paso activo se conserva mientras exista; si no, el último.
    const pasos = modelo.pasos;
    if (!pasos.some((p) => p.id === estado.pasoActivo)) {
      estado.pasoActivo = pasos.length ? pasos[pasos.length - 1].id : null;
    }
    if (estado.indicePaso >= (instrucciones.pasos.length || 1)) estado.indicePaso = 0;

    // La pieza en mano puede haberse agotado al colocarla.
    if (estado.piezaEnMano) {
      const viva = estado.paleta.find((p) => p.element_id === estado.piezaEnMano.element_id);
      estado.piezaEnMano = viva || null;
    }

    // La selección sobrevive a la recarga mientras esas piezas sigan puestas:
    // así mover, pintar o deshacer no obligan a volver a elegirlas.
    const vivas = new Set(modelo.piezas.map((p) => p.id));
    estado.seleccion = estado.seleccion.filter((id) => vivas.has(id));

    // La geometría real de cada molde, antes de dibujar nada.
    await asegurarMallas(modelo.piezas.map((p) => p.design_id));

    // Altura de cada casilla del tablero, para colocar y previsualizar.
    estado.alturas = new Map();
    for (const pieza of modelo.piezas) {
      for (const [x, z, , hasta] of pieza.columnas || []) {
        const clave = `${x},${z}`;
        estado.alturas.set(clave, Math.max(estado.alturas.get(clave) || 0, hasta));
      }
    }

    $("#dis-placa-w").value = modelo.placa.ancho;
    $("#dis-placa-d").value = modelo.placa.fondo;
    $("#dis-estado").textContent =
      `${modelo.total_piezas} piezas colocadas · altura ${modelo.altura_maxima} placas · ${modelo.estado}` +
      (estado.seleccion.length ? ` · ${estado.seleccion.length} seleccionadas` : "");

    ajustarControlDeCapas(modelo.altura_maxima);
    dibujarPlaca(modelo.placa.ancho, modelo.placa.fondo);
    dibujarModelo();
    pintarPaleta();
    pintarPasos();
    edicion.pintarPanel();
    edicion.pintarBotonesHistorial();
    pintarInstruccion();
    if (!conservarVista) recentrar();
  } catch (e) {
    avisar(e.message, "error");
  } finally {
    $("#dis-cargando").classList.add("oculto");
  }
}

function aplicarModo() {
  const enDisenar = estado.modo === "disenar";
  $("#dis-panel-disenar").classList.toggle("oculto", !enDisenar);
  $("#dis-panel-instrucciones").classList.toggle("oculto", enDisenar);
  document.querySelector(".disenador").classList.toggle("leyendo", !enDisenar);
  document
    .querySelectorAll("#dis-modo button")
    .forEach((b) => b.classList.toggle("activa", b.dataset.modo === estado.modo));
  if (!enDisenar) {
    soltarPieza();
    edicion.limpiar();
  }
  pintarAyuda();
  dibujarModelo();
  recentrar();
}

// --------------------------------------------------------------------------
// Enganches de la interfaz
// --------------------------------------------------------------------------
/** El deslizador de capas se ajusta a lo alto que sea el modelo. */
function ajustarControlDeCapas(altura) {
  const control = $("#dis-capa");
  const maximo = Math.max(0, altura - 1);
  control.max = String(maximo);
  if (estado.capaMaxima !== null) estado.capaMaxima = Math.min(estado.capaMaxima, maximo);
  control.value = String(estado.capaMaxima === null ? maximo : estado.capaMaxima);
  $("#dis-capa-texto").textContent = textoDeCapa();
}

function textoDeCapa() {
  if (estado.capaMaxima === null) return "todas";
  return `${estado.aislarCapa ? "sólo" : "hasta"} ${estado.capaMaxima}`;
}

/** Puntos de vista de siempre: isométrica para construir, alzado para juzgar
 *  las alturas y planta para cuadrar la rejilla. */
function verDesde(vista) {
  if (vista === "planta") {
    orbita.phi = 0.13;
  } else if (vista === "alzado") {
    orbita.theta = Math.PI * 0.5;
    orbita.phi = Math.PI / 2.05;
  } else {
    orbita.theta = Math.PI * 0.25;
    orbita.phi = Math.PI * 0.32;
  }
  actualizarCamara();
}

function conectarInterfaz() {
  $("#dis-montaje").addEventListener("change", async (e) => {
    estado.montajeId = Number(e.target.value) || null;
    estado.pasoActivo = null;
    estado.seleccion = [];
    estado.indicePaso = 0;
    estado.capaMaxima = null;
    await recargar();
  });

  $("#dis-herramientas").addEventListener("click", (evento) => {
    const boton = evento.target.closest("button[data-herramienta]");
    if (boton) cambiarHerramienta(boton.dataset.herramienta);
  });

  $("#dis-deshacer").addEventListener("click", () => edicion.deshacer());
  $("#dis-rehacer").addEventListener("click", () => edicion.rehacer());

  $("#dis-vistas").addEventListener("click", (evento) => {
    const boton = evento.target.closest("button[data-vista3d]");
    if (!boton) return;
    verDesde(boton.dataset.vista3d);
    document
      .querySelectorAll("#dis-vistas button")
      .forEach((b) => b.classList.toggle("activa", b === boton));
  });

  $("#dis-capa").addEventListener("input", (evento) => {
    const maximo = Number(evento.target.max);
    const valor = Number(evento.target.value);
    // Con el mando arriba del todo se ve el modelo entero, que es lo normal.
    estado.capaMaxima = valor >= maximo && !estado.aislarCapa ? null : valor;
    $("#dis-capa-texto").textContent = textoDeCapa();
    dibujarModelo();
  });

  $("#dis-aislar").addEventListener("change", (evento) => {
    estado.aislarCapa = evento.target.checked;
    // Aislar sin haber elegido capa no dice nada: se toma la del mando.
    if (estado.aislarCapa && estado.capaMaxima === null) {
      estado.capaMaxima = Number($("#dis-capa").value);
    }
    $("#dis-capa-texto").textContent = textoDeCapa();
    dibujarModelo();
  });

  $("#dis-modo").addEventListener("click", (evento) => {
    const boton = evento.target.closest("button[data-modo]");
    if (!boton) return;
    estado.modo = boton.dataset.modo;
    aplicarModo();
  });

  $("#dis-paleta").addEventListener("click", (evento) => {
    const ficha = evento.target.closest(".ficha");
    if (!ficha) return;
    const pieza = estado.paleta.find((p) => p.element_id === ficha.dataset.element);
    estado.piezaEnMano = estado.piezaEnMano?.element_id === pieza.element_id ? null : pieza;
    if (estado.piezaEnMano) asegurarMallas([pieza.design_id]);
    // Cada pieza empieza sin ajuste manual de altura.
    estado.desnivel = 0;
    estado.previsualizacion = null;
    // Elegir una pieza es para ponerla; sólo el bote de pintura la usa de otra
    // manera, y ahí lo que interesa es su color.
    if (estado.piezaEnMano && estado.herramienta !== "pintar") cambiarHerramienta("colocar");
    pintarPaleta();
    edicion.pintarPanel();
    if (!estado.piezaEnMano) grupoFantasma.clear();
    solicitarRender();
  });

  $("#dis-buscar").addEventListener("input", (e) => {
    estado.filtroPaleta = e.target.value.trim();
    pintarPaleta();
  });

  $("#dis-pasos").addEventListener("click", (evento) => {
    const boton = evento.target.closest("[data-piezas-de]");
    if (boton) {
      evento.stopPropagation();
      return edicion.seleccionarPaso(Number(boton.dataset.piezasDe));
    }
    const item = evento.target.closest("[data-paso]");
    if (!item) return;
    estado.pasoActivo = Number(item.dataset.paso);
    pintarPasos();
  });

  $("#dis-nuevo-paso").addEventListener("click", async () => {
    const titulo = $("#dis-paso-titulo").value.trim();
    if (!titulo) return avisar("Ponle un título al paso.", "error");
    try {
      const d = await api(`/api/montajes/${estado.montajeId}/pasos`, {
        method: "POST",
        body: JSON.stringify({ titulo, piezas: [] }),
      });
      $("#dis-paso-titulo").value = "";
      const nuevo = d.pasos[d.pasos.length - 1];
      estado.pasoActivo = nuevo?.id ?? null;
      await recargar({ conservarVista: true });
      avisar(`Paso «${titulo}» creado. Lo que coloques ahora irá aquí.`);
    } catch (e) {
      avisar(e.message, "error");
    }
  });

  $("#dis-rotar").addEventListener("click", girarPieza);
  $("#dis-soltar").addEventListener("click", soltarPieza);
  $("#dis-borrar").addEventListener("click", quitarSeleccion);
  $("#dis-recentrar").addEventListener("click", recentrar);
  $("#ins-anterior").addEventListener("click", () => irAPaso(estado.indicePaso - 1));
  $("#ins-siguiente").addEventListener("click", () => irAPaso(estado.indicePaso + 1));

  const cambiarPlaca = async () => {
    const ancho = Number($("#dis-placa-w").value);
    const fondo = Number($("#dis-placa-d").value);
    if (!ancho || !fondo) return;
    try {
      await api(`/api/montajes/${estado.montajeId}/placa`, {
        method: "POST",
        body: JSON.stringify({ ancho, fondo }),
      });
      await recargar();
    } catch (e) {
      avisar(e.message, "error");
      $("#dis-placa-w").value = estado.modelo.placa.ancho;
      $("#dis-placa-d").value = estado.modelo.placa.fondo;
    }
  };
  $("#dis-placa-w").addEventListener("change", cambiarPlaca);
  $("#dis-placa-d").addEventListener("change", cambiarPlaca);
}

// --------------------------------------------------------------------------
// Arranque: sólo se monta la escena cuando se entra en la vista.
// --------------------------------------------------------------------------
async function abrir(montajeId = null, modo = null) {
  if (!estado.iniciado) {
    iniciarEscena();
    // El editor no importa a este módulo: recibe lo que necesita de la escena
    // y del estado, y así no hay dependencias cruzadas entre los dos.
    edicion.iniciar({
      estado,
      api,
      avisar,
      esc,
      huella,
      recargar,
      resaltarSeleccion,
      piezasVisibles,
      soltarPieza,
    });
    conectarInterfaz();
    cambiarHerramienta(estado.herramienta);
    estado.iniciado = true;
  }
  const montajes = await cargarMontajes();
  if (!montajes.length) return;
  if (montajeId && montajes.some((m) => m.id === montajeId)) {
    estado.montajeId = montajeId;
    $("#dis-montaje").value = String(montajeId);
  }
  if (modo) estado.modo = modo;
  aplicarModo();
  await recargar();
  redimensionar();
}

window.addEventListener("vista-cambiada", (evento) => {
  if (evento.detail === "disenador") abrir();
});

// Puente para el resto de la interfaz: "Abrir en el diseñador" desde montajes.
window.abrirDisenador = (montajeId, modo) => {
  if (montajeId) estado.montajeId = montajeId;
  if (modo) estado.modo = modo;
  if (location.hash.slice(1) === "disenador") abrir(montajeId, modo);
  else location.hash = "disenador"; // el evento de cambio de vista hace el resto
};

if (location.hash.slice(1) === "disenador") abrir();
