# Inventario, montajes y diseñador LEGO

Aplicación web para inventariar piezas LEGO, diseñar modelos en 3D con lo que
realmente tienes y seguir las instrucciones paso a paso. El **servidor MCP es el
motor**: todo lo que se hace desde la web se puede hacer desde un chat, porque
ambos usan los mismos servicios y la misma base de datos.

```
lego/
├── backend/
│   ├── app/
│   │   ├── main.py          FastAPI: API REST + MCP + frontend en un proceso
│   │   ├── mcp_server.py    Las 48 herramientas MCP
│   │   ├── mcp_stdio.py     Mismo servidor por stdio (chats)
│   │   ├── models.py        Esquema SQLite
│   │   ├── routers/api.py   API REST
│   │   └── services/        Lógica de negocio (compartida por REST y MCP)
│   │       ├── vocab.py        español -> nomenclatura oficial LEGO
│   │       ├── catalog.py      catálogo y búsqueda puntuada
│   │       ├── importers.py    CSV de LEGO.com
│   │       ├── inventory.py    existencias, reservas, disponibilidad
│   │       ├── builds.py       montajes paso a paso
│   │       ├── geometry.py     del nombre de la pieza a su volumen
│   │       ├── ldraw.py        geometría real de cada molde (biblioteca LDraw)
│   │       ├── designer.py     modelo 3D: colocar, mover, instrucciones
│   │       ├── editor.py       edición en grupo: mover, girar, duplicar…
│   │       ├── historial.py    deshacer y rehacer sobre el modelo
│   │       └── recognition.py  inventariado por foto
│   ├── lego_mcp.py      Lanzador stdio, ejecutable desde cualquier ruta
│   └── scripts/
│       ├── seed.py      Carga inicial desde seed/*.csv
│       ├── mallas.py    Convierte los moldes del catálogo a geometría 3D
│       └── pruebas.py   68 comprobaciones de regresión
├── frontend/            Interfaz web
│   ├── app.js               Inventario, sets, montajes, fotos
│   ├── designer.js          Escena 3D, paleta e instrucciones visuales
│   ├── edicion.js           Editor: selección, mover, pintar, deshacer
│   └── vendor/              three.js servido en local (sin CDN)
├── seed/                CSV de sets y equivalencias LDraw
└── data/
    ├── lego.db          Base de datos SQLite
    ├── ldraw/           Copia local de los moldes que se van usando
    └── mallas/          Geometría ya convertida, lista para el navegador
```

## Puesta en marcha

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

cd backend
..\.venv\Scripts\python.exe -m scripts.seed        # carga los CSV de seed/
..\.venv\Scripts\python.exe -m scripts.mallas      # geometría 3D de las piezas
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

`scripts.mallas` tarda unos minutos la primera vez (se descarga de LDraw lo que
haga falta) y no es obligatorio: sin él la aplicación funciona igual, sólo que
las piezas se dibujan como la caja que ocupan hasta que el propio diseñador las
vaya convirtiendo en segundo plano.

O directamente `arrancar.bat` desde la raíz.

| Dirección | Qué es |
|---|---|
| http://127.0.0.1:8000/ | Interfaz web |
| http://127.0.0.1:8000/docs | Documentación de la API |
| http://127.0.0.1:8000/mcp/ | Servidor MCP (HTTP) |

## Acceso

La app entera vive detrás de un login (usuario único, sin registro): al
entrar sin sesión, cualquier ruta redirige a `/login`. El servidor MCP en `/mcp` es aparte, porque lo usan clientes que no pueden
hacer un login interactivo, y admite dos formas de autenticarse:

- **Token fijo**: `Authorization: Bearer <LEGO_MCP_TOKEN>`. Es lo más simple
  y le vale a Claude Code. Sin `LEGO_MCP_TOKEN`, `/mcp` queda abierto.
- **OAuth 2.1** (el flujo del propio protocolo MCP): el cliente descubre los
  metadatos, se registra solo, manda al usuario a una pantalla de permiso
  --que reutiliza el login-- y recibe un token. Es lo que necesitan los
  conectores de ChatGPT, que no dejan fijar la cabecera a mano. Se activa
  definiendo `LEGO_PUBLIC_URL`. El protocolo lo implementa el SDK de MCP;
  `app/oauth.py` sólo aporta el proveedor y la pantalla de consentimiento.

Variables de entorno relevantes (ver `.env.example`):

| Variable | Para qué |
|---|---|
| `LEGO_ADMIN_USER` | Usuario del login (por defecto `admin`) |
| `LEGO_ADMIN_PASSWORD` | Contraseña del login |
| `LEGO_SESSION_SECRET` | Firma la cookie de sesión; un valor aleatorio largo |
| `LEGO_MCP_TOKEN` | Opcional: exige `Authorization: Bearer <token>` en `/mcp` |
| `LEGO_PUBLIC_URL` | URL pública (p. ej. `https://briks.peraltalberto.com`); activa OAuth |
| `LEGO_COOKIE_SECURE` | `true` en producción (HTTPS); `false` en local |

## Despliegue (Docker / Dokploy)

```bash
docker compose build
LEGO_ADMIN_PASSWORD=... LEGO_SESSION_SECRET=... docker compose up -d
```

En Dokploy: aplicación de tipo *Dockerfile* apuntando a este repositorio,
con las variables de entorno de la tabla anterior y un **volumen persistente**
en `/app/data` (ahí vive `lego.db`, las fotos subidas y la caché de mallas
3D: sin volumen, cada despliegue nuevo borra el inventario). El contenedor
expone el puerto 8000; Dokploy se encarga del dominio y el certificado TLS.

El primer arranque sin base de datos siembra el catálogo (colores, piezas,
los sets de `seed/`) con `scripts.seed --solo-catalogo`: registra los sets
pero deja el inventario a cero, listo para las piezas reales. `scripts.mallas`
no se ejecuta solo -- tarda minutos y descarga de LDraw -- se lanza a mano
desde la terminal de Dokploy si se quiere el acabado 3D real desde el
principio.

## Conceptos

LEGO tiene su propio vocabulario y conviene respetarlo porque es lo que aparece
en los CSV oficiales:

- **DesignID**: el molde, sin color. `3021` = `PLATE 2X3`.
- **ElementID**: molde + color, lo que identifica una pieza concreta. `302126` =
  `PLATE 2X3` en `Black`. **El inventario se lleva por ElementID.**
- **Colores**: nomenclatura oficial. `Brick Yellow` es el beige y
  `Medium Stone Grey` el gris claro. No hace falta que te la aprendas: puedes
  escribir «placa 2x4 roja» y se traduce.

Tres cantidades distintas, y la diferencia importa:

- **existencias**: lo que tienes físicamente.
- **reservado**: lo que está usado por montajes activos.
- **disponible**: existencias − reservado. Es lo que puedes usar en algo nuevo.

Un montaje en estado `planificado`, `en_progreso` o `completado` mantiene sus
piezas reservadas. Pasarlo a `desmontado` las devuelve al fondo común.

## Conectar el chat

**Claude Code (HTTP)** — con el servidor arrancado:

```bash
claude mcp add --transport http lego http://127.0.0.1:8000/mcp/
```

**Claude Code (stdio)** — no necesita la web arrancada, que es lo más cómodo
para el uso diario. Ya queda registrado así:

```bash
claude mcp add lego --scope user -- "P:\aperalta\lego\.venv\Scripts\python.exe" "P:\aperalta\lego\backend\lego_mcp.py"
```

**Claude Desktop (stdio)** — en `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lego": {
      "command": "P:\\aperalta\\lego\\.venv\\Scripts\\python.exe",
      "args": ["P:\\aperalta\\lego\\backend\\lego_mcp.py"]
    }
  }
}
```

`lego_mcp.py` se lanza por ruta absoluta y no depende del directorio de trabajo.
Los tres caminos atacan la misma base de datos, así que da igual por cuál entres.

## Inventariar

### Por número de set

Los CSV se descargan de LEGO.com (servicio al cliente → inventario del set).
Formato: `SetNumber, ElementID, Qty, Colour, Category, DesignID, ElementName,
ImageURL, ElementSetCount`.

1. `importar_set_csv` registra de qué se compone el set y amplía el catálogo.
2. `inventariar_set` es lo que **suma las piezas** a tu inventario.

Están separados a propósito: puedes conocer un set sin tenerlo, que es lo que
permite responder «¿qué me falta para montarlo?».

**Un set ya inventariado no se vuelve a inventariar sin preguntar.** Repetirlo
duplicaría las existencias en silencio, así que la operación se rechaza y se
informa de cuántas piezas se sumaron y cuándo. Para seguir hay que insistir:
`confirmar=true` en la herramienta MCP, o «Sí, tengo otra copia» en la web. Es
lo correcto cuando de verdad tienes el mismo set repetido.

### Por foto

El reconocimiento visual lo hace el modelo del chat, que es quien ve la imagen.
Este servidor aporta lo que el modelo no puede hacer de memoria: casar una
descripción con el ElementID correcto y llevar la contabilidad.

1. Le enseñas la foto al chat.
2. Él describe lo que ve y llama a `registrar_piezas_detectadas`.
3. Lo que casa con claridad queda resuelto; lo dudoso queda **pendiente con
   candidatos**, en vez de adivinar.
4. `resolver_reconocimiento` cierra las dudas.
5. `confirmar_reconocimiento` es el único paso que toca el inventario.

Ese lote intermedio existe para que una foto mal interpretada no te contamine
el inventario en silencio.

## Montajes

Un montaje es una secuencia de pasos; cada paso declara sus piezas. Al añadir un
paso se comprueba la disponibilidad real y se rechaza si no llegan las piezas
(salvo que pases `permitir_faltantes`). Si una descripción es ambigua, la
herramienta devuelve los candidatos para que el chat elija en vez de arriesgarse.

```
crear_montaje  →  anadir_paso (×N)  →  marcar_paso  →  cambiar_estado_montaje
```

`que_puedo_montar` compara tu inventario disponible con los sets importados y
dice cuáles salen enteros y qué falta para los demás.

## Diseñador 3D

Un montaje no es sólo una lista de piezas: cada pieza puede tener un sitio. La
pestaña **Diseñador 3D** es un LEGO Digital Designer sobre tu propio inventario,
y el chat construye en el mismo modelo con las mismas reglas.

### Coordenadas

Las de LEGO, y siempre enteras:

- `x`, `z`: studs desde la esquina de la placa base (32×32 por defecto).
- `y`: altura en **placas**; un ladrillo mide 3. Si no la indicas, la pieza se
  apoya sobre lo que haya debajo, que es como se construye de verdad.
- `rotacion`: 0, 90, 180 o 270 grados. A 90° y 270° se intercambian ancho y fondo.

Al hacer clic, la pieza se coloca sobre la superficie que estás señalando: si
apuntas al suelo junto al borde de otra pieza, se corre hasta una casilla para
apoyarse entera en el suelo en vez de quedar medio encima y medio en el aire.
Cuando aun así queda en voladizo —que en LEGO también se sostiene— se avisa.

Si la pieza sube, es porque descansa sobre otra: el aviso dice sobre cuál
(«colocada a 1 placa, apoyada sobre PLATE 1X2»). Y si crees que debería ir más
abajo, bájala con <kbd>−</kbd>: el fantasma se pone rojo donde no cabe y, al
hacer clic, el motor responde contra qué pieza choca en vez de subirla en
silencio. La misma comprobación, casilla a casilla y con el relieve real de
cada molde, la aplica el chat con `colocar_piezas` pasando `y`.

Al colocar se comprueban tres cosas: que la pieza cabe en la placa, que no pisa
a otra y que tienes unidades libres. Una pieza colocada **queda reservada**,
igual que si la hubieras declarado en el paso; quitarla la devuelve al fondo
común.

### Cómo sabe la aplicación qué forma tiene cada pieza

Hay dos capas, y conviene no confundirlas:

**Cuánto ocupa en planta** (`geometry.py`) sale del nombre del molde: `BRICK
2X4` se ancla sobre 2×4 studs. Ese rectángulo es el que gobierna la rejilla.

**Qué altura tiene y con qué relieve** sale de la geometría real. El nombre
miente a menudo: `CORNER PLATE 1X2X2` no es un bloque de dos ladrillos, es una
placa de una sola placa de alto, y creérselo dejaba flotando en el aire todo lo
que se apoyara encima. De los moldes del catálogo, 24 tenían la altura mal por
esta razón.

Además, cada pieza guarda su **relieve por casillas**: desde qué altura y
hasta cuál hay material en cada stud de su planta. La base tampoco es plana: la
mitad trasera de una `PLATE W. BOW 2X2X2/3` empieza una placa más arriba que la
delantera, y por eso una placa 1×2 cabe debajo y la curva se queda a nivel de
suelo con la placa metida en su hueco. Una pieza baja hasta que su parte más
baja toca algo, no hasta que lo hace su casilla más alta; y apoyar algo junto
al saliente de otra pieza ya no lo sube hasta lo alto del saliente. El navegador recibe ese mismo relieve, de modo que la
previsualización que ves al mover el ratón y lo que el motor acepta son lo
mismo.

**Cómo se dibuja** (`ldraw.py`) sale de la biblioteca **LDraw**, el catálogo
abierto que describe cada molde con su geometría exacta. Encaja con el
inventario porque LDraw usa los mismos números de molde que LEGO: `3021.dat` es
la PLATE 2X3, igual que el DesignID 3021. De ahí salen los arcos, las cuñas, los
cuartos de círculo y los tetones de verdad, en vez de una caja.

Cada molde se descarga una vez, se resuelven sus subpiezas y primitivas, y la
malla queda cacheada en `data/mallas/`. El molde que LDraw no tenga se dibuja
como su caja y se marca con `≈` en la paleta: es preferible una caja honesta a
una forma inventada.

Cuando LEGO y LDraw no coinciden en la numeración, la equivalencia se anota en
`seed/ldraw_alias.json` junto con la descripción contra la que se verificó, para
que se pueda comprobar de un vistazo si el alias es correcto.

### Instrucciones visuales

Cada pieza pertenece al paso en el que se colocó, y eso es lo que convierte el
modelo en un manual. El modo **Instrucciones** muestra, paso a paso:

- lo ya montado en gris translúcido y las piezas nuevas a todo color;
- el recuadro de piezas del paso con **la foto real de cada una** y su cantidad,
  como en los manuales de LEGO;
- las coordenadas exactas de cada pieza nueva.

Desde el chat, `instrucciones_montaje` devuelve exactamente lo mismo en datos, y
`ver_modelo` con `mapa=true` dibuja la planta de cada capa en texto: es la forma
de comprobar cómo va quedando el modelo sin ver la pantalla.

El chat edita con las mismas herramientas que la web: `buscar_en_modelo`
devuelve los ids de lo que cumpla un criterio («las placas rojas de la capa 3»)
y con esos ids trabajan `mover_piezas`, `girar_piezas`, `reflejar_piezas`,
`duplicar_piezas`, `sustituir_piezas` y `quitar_piezas`. Como todo pasa por el
mismo servicio, deshacer desde la web revierte lo que hizo el chat, y al
revés.

### Editar lo ya construido

Colocar piezas es la mitad del trabajo; la otra mitad es rectificar. El
diseñador tiene cinco herramientas, y el clic hace lo que diga la que esté
activa:

| Herramienta | Qué hace el clic | Tecla |
|---|---|---|
| **Seleccionar** | elegir piezas, arrastrarlas y encuadrar varias | <kbd>1</kbd> |
| **Colocar** | poner la pieza que tengas en mano | <kbd>2</kbd> |
| **Pintar** | repintar esa pieza del color de la que tengas elegida en la paleta | <kbd>3</kbd> |
| **Borrar** | quitar esa pieza (vuelve al inventario) | <kbd>4</kbd> |
| **Copiar** | coger en mano la pieza señalada, para repetirla | <kbd>5</kbd> |

La selección es de varias piezas: <kbd>Ctrl</kbd>+clic suma o resta,
arrastrar sobre el vacío encuadra todo lo que abarque el rectángulo, y hay
atajos para «todas las iguales», «todo el modelo» y «las piezas de este paso».
Con algo seleccionado, el panel de la derecha permite moverlo, girarlo,
reflejarlo, duplicarlo, apoyarlo sobre lo que haya debajo, repintarlo con las
muestras de los colores que tengas de ese molde, llevarlo a otro paso o
quitarlo. Con una sola pieza, además, se escriben sus coordenadas a mano.

**Un grupo se mueve rígido**: las piezas que viajan juntas no chocan entre sí,
sólo contra el resto del modelo, y lo que no cabe se rechaza diciendo con qué
choca. Arrastrando, el fantasma se pone rojo antes de soltar.

**Todo se puede deshacer** (<kbd>Ctrl</kbd>+<kbd>Z</kbd>), incluido lo que haya
hecho el chat: antes de cada operación se guarda una foto del modelo con sus
piezas, sus reservas y su placa. Las últimas 30 quedan disponibles, y `deshacer`
funciona igual desde la web que desde una conversación.

| Acción | Cómo |
|---|---|
| Colocar | elegir pieza en la paleta y hacer clic en la placa |
| Apoyar entera | la pieza se corre hasta una casilla para no quedar a caballo |
| Girar la pieza o la selección | botón «Girar» o tecla <kbd>R</kbd> |
| Subir o bajar | <kbd>+</kbd> / <kbd>−</kbd> (o Re Pág / Av Pág) |
| Mover la selección | <kbd>←</kbd> <kbd>→</kbd> <kbd>↑</kbd> <kbd>↓</kbd> (según hacia dónde mires) |
| Soltar la pieza / la selección | <kbd>Esc</kbd> |
| Quitar lo seleccionado | <kbd>Supr</kbd> |
| Deshacer / rehacer | <kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> |
| Copiar y pegar | <kbd>Ctrl</kbd>+<kbd>C</kbd> / <kbd>Ctrl</kbd>+<kbd>V</kbd> (pega al lado, si hay hueco) |
| Duplicar / seleccionar todo | <kbd>Ctrl</kbd>+<kbd>D</kbd> / <kbd>Ctrl</kbd>+<kbd>A</kbd> |
| Ver el interior | el mando «Capas» baja el techo; «aislar» deja sólo esa capa |
| Punto de vista | Iso / Alzado / Planta |
| Girar la vista | arrastrar con el botón derecho |
| Zoom / desplazar | rueda / <kbd>Mayús</kbd> + arrastrar |

three.js se sirve desde `frontend/vendor/`, así que la aplicación no depende de
ningún CDN para funcionar. Sí salen de fuera dos cosas: las fotos de las piezas
(CDN de LEGO) y los moldes que aún no se hayan convertido (biblioteca LDraw,
licencia CCAL 2.0). Una vez convertidos, el diseñador funciona sin red.

## Herramientas MCP

| Grupo | Herramientas |
|---|---|
| Panorama | `resumen_inventario`, `listar_colores` |
| Búsqueda | `buscar_piezas`, `consultar_inventario`, `historial_movimientos` |
| Sets | `importar_set_csv`, `inventariar_set`, `listar_sets`, `piezas_de_set` |
| Inventario | `ajustar_inventario`, `alta_pieza_manual` |
| Foto | `registrar_piezas_detectadas`, `ver_reconocimiento`, `resolver_reconocimiento`, `confirmar_reconocimiento`, `listar_reconocimientos`, `descartar_reconocimiento` |
| Montajes | `crear_montaje`, `anadir_paso`, `editar_paso`, `ver_montaje`, `listar_montajes`, `marcar_paso`, `eliminar_paso`, `cambiar_estado_montaje`, `eliminar_montaje`, `comprobar_montaje`, `que_puedo_montar` |
| Diseñador 3D | `colocar_piezas`, `mover_pieza`, `quitar_pieza`, `vaciar_modelo`, `ver_modelo`, `instrucciones_montaje`, `paleta_de_piezas`, `configurar_placa` |
| Editor | `buscar_en_modelo`, `mover_piezas`, `girar_piezas`, `reflejar_piezas`, `duplicar_piezas`, `sustituir_piezas`, `colores_de_pieza`, `mover_piezas_de_paso`, `quitar_piezas` |
| Deshacer | `deshacer`, `rehacer`, `historial_diseno` |

Los errores de negocio se devuelven como datos (`{"ok": false, "error": …}`), y
cuando hay ambigüedad incluyen `candidatos`, para que el chat pueda reaccionar
sin romper la conversación. Cuando lo que hace falta es una decisión de la
persona —reinventariar un set— llegan con `requiere_confirmacion`.

## Configuración

Variables de entorno con prefijo `LEGO_` (o un `.env` en la raíz):

| Variable | Por defecto | Para qué |
|---|---|---|
| `LEGO_DB_PATH` | `data/lego.db` | Ubicación de la base de datos |
| `LEGO_SEED_DIR` | `seed/` | Carpeta de CSV para la carga inicial |
| `LEGO_API_PORT` | `8000` | Puerto del servidor |
| `LEGO_REBRICKABLE_API_KEY` | — | Reservado para catálogo externo (no usado aún) |

## Estado actual

Funciona y está probado de extremo a extremo: importación de los CSV reales,
inventario con ubicaciones, búsqueda en español, reconocimiento por foto con
confirmación, montajes con reserva de piezas, diseñador 3D con instrucciones
visuales, y el MCP servido por HTTP y stdio.

Lo que **no** hace todavía:

- No hay visión artificial propia: el reconocimiento de la foto lo pone el
  modelo del chat.
- La geometría real llega hasta donde llega LDraw: de los moldes del catálogo
  actual, nueve no están con la numeración de LEGO y se dibujan como su caja.
  Se arreglan uno a uno añadiéndolos a `seed/ldraw_alias.json`.
- En planta, el hueco que reserva cada pieza sigue siendo su rectángulo, no su
  silueta: dos cuñas que en la realidad encajarían en diagonal aquí chocan. En
  altura sí se respeta el relieve real.
- Las piezas sólo giran sobre el eje vertical y de 90 en 90 grados. No hay
  piezas tumbadas ni bisagras abiertas.
- El catálogo se limita a lo que hayas importado. Una pieza que no esté en
  ningún set importado no se puede resolver hasta darla de alta con
  `alta_pieza_manual`.
- Sin autenticación: pensado para uso local.
- La integración con Rebrickable está prevista en la configuración pero no
  implementada.
