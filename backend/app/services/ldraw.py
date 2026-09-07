"""Geometría real de las piezas, a partir de la biblioteca LDraw.

Deducir la forma del nombre sirve para saber cuánto ocupa una pieza, pero no
para dibujarla: un arco, una cuña o una bisagra acaban siendo la misma caja.
LDraw es el catálogo abierto que describe cada molde con su geometría exacta y
—esto es lo que lo hace encajar— **usa los mismos números de molde que LEGO**:
`3021.dat` es la PLATE 2X3, igual que el DesignID 3021 del inventario.

Aquí se descarga cada `.dat` una sola vez, se resuelve la jerarquía de
subpiezas y primitivas, y se deja una malla de triángulos lista para el
navegador. Todo queda cacheado en disco: la segunda vez no hay red.

Unidades: LDraw mide en LDU (1 stud = 20 LDU, 1 placa de alto = 8 LDU) y su eje
Y crece hacia abajo. Aquí se convierte a las unidades del diseñador (1 stud = 1)
con la Y hacia arriba, así que basta dividir entre 20 e invertir el signo.

Licencia de la biblioteca: CCAL 2.0 (redistribuible citando la fuente).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from ..config import settings

# LDU por stud y por placa de altura.
LDU_POR_STUD = 20.0
LDU_POR_PLACA = 8.0

# Hasta dónde se sigue la cadena de subpiezas antes de dar por hecho que hay un
# ciclo. Las piezas reales rara vez pasan de seis niveles.
PROFUNDIDAD_MAXIMA = 12

# Color 16 = "el que herede de quien me use". Es el que se pinta con el color
# real del elemento; el resto son colores fijos del molde (ejes negros, etc.).
COLOR_HEREDADO = 16
COLOR_BORDE = 24

# Carpetas donde LDraw busca un fichero referenciado, en este orden.
_PREFIJOS = ("parts/", "p/", "models/")

_LINEA_NUMEROS = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


class LDrawNoDisponible(Exception):
    """La pieza no está en la biblioteca (o no hay forma de traerla)."""


# --------------------------------------------------------------------------
# Equivalencias de numeración
# --------------------------------------------------------------------------
_ALIAS: dict[str, str] | None = None


def alias_ldraw() -> dict[str, str]:
    """DesignID de LEGO -> número de LDraw, cuando no son el mismo.

    La mayoría coinciden (3021 es PLATE 2X3 en los dos), pero algunos moldes
    modernos llevan numeración distinta. La tabla vive en `seed/ldraw_alias.json`
    para poder ampliarla sin tocar código.
    """
    global _ALIAS
    if _ALIAS is not None:
        return _ALIAS
    _ALIAS = {}
    fichero = settings.seed_dir / "ldraw_alias.json"
    if fichero.is_file():
        try:
            datos = json.loads(fichero.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return _ALIAS
        for clave, valor in datos.items():
            if clave.startswith("_"):
                continue
            destino = valor.get("ldraw") if isinstance(valor, dict) else valor
            if destino:
                _ALIAS[str(clave).strip()] = str(destino).strip()
    return _ALIAS


# --------------------------------------------------------------------------
# Acceso a los ficheros .dat
# --------------------------------------------------------------------------
def _dir_biblioteca():
    ruta = settings.db_path.parent / "ldraw"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def _dir_mallas():
    ruta = settings.db_path.parent / "mallas"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def _normalizar(nombre: str) -> str:
    return nombre.strip().replace("\\", "/").lower()


def _leer_local(ruta_relativa: str) -> str | None:
    fichero = _dir_biblioteca() / ruta_relativa
    if fichero.is_file():
        return fichero.read_text(encoding="utf-8", errors="replace")
    return None


def _guardar_local(ruta_relativa: str, contenido: str) -> None:
    fichero = _dir_biblioteca() / ruta_relativa
    fichero.parent.mkdir(parents=True, exist_ok=True)
    fichero.write_text(contenido, encoding="utf-8")


# Ficheros que ya se sabe que no existen: evita repetir la petición fallida.
_FALLIDOS: set[str] = set()


def obtener_dat(nombre: str, permitir_descarga: bool = True) -> str | None:
    """Contenido de un fichero LDraw, de la caché local o de la biblioteca."""
    nombre = _normalizar(nombre)
    if not nombre.endswith(".dat") and not nombre.endswith(".ldr"):
        nombre += ".dat"

    candidatos = [nombre] if "/" in nombre and nombre.split("/")[0] in ("parts", "p", "models") else []
    candidatos += [prefijo + nombre for prefijo in _PREFIJOS]

    for ruta in candidatos:
        contenido = _leer_local(ruta)
        if contenido is not None:
            return contenido

    if not permitir_descarga or not settings.ldraw_base_url:
        return None

    for ruta in candidatos:
        if ruta in _FALLIDOS:
            continue
        url = f"{settings.ldraw_base_url.rstrip('/')}/{ruta}"
        try:
            respuesta = httpx.get(url, timeout=20.0, follow_redirects=True)
        except httpx.HTTPError:
            # Sin red no hay nada que hacer, pero tampoco es un error fatal:
            # quien llama se queda con la forma aproximada.
            return None
        if respuesta.status_code == 200:
            _guardar_local(ruta, respuesta.text)
            return respuesta.text
        _FALLIDOS.add(ruta)
    return None


# --------------------------------------------------------------------------
# Colores de LDraw
# --------------------------------------------------------------------------
_COLORES: dict[int, str] | None = None


def colores_ldraw() -> dict[int, str]:
    """Código de color LDraw -> hex. Se usa para las partes de color fijo."""
    global _COLORES
    if _COLORES is not None:
        return _COLORES
    _COLORES = {}
    contenido = _leer_local("LDConfig.ldr")
    if contenido is None and settings.ldraw_base_url:
        try:
            respuesta = httpx.get(
                f"{settings.ldraw_base_url.rstrip('/')}/LDConfig.ldr",
                timeout=20.0,
                follow_redirects=True,
            )
            if respuesta.status_code == 200:
                contenido = respuesta.text
                _guardar_local("LDConfig.ldr", contenido)
        except httpx.HTTPError:
            contenido = None
    if not contenido:
        return _COLORES

    for linea in contenido.splitlines():
        partes = linea.split()
        if len(partes) < 8 or partes[:2] != ["0", "!COLOUR"]:
            continue
        try:
            codigo = int(partes[partes.index("CODE") + 1])
            valor = partes[partes.index("VALUE") + 1]
        except (ValueError, IndexError):
            continue
        if valor.startswith("#"):
            _COLORES[codigo] = valor.upper()
    return _COLORES


# --------------------------------------------------------------------------
# Parseo y triangulación
# --------------------------------------------------------------------------
@dataclass
class _Acumulador:
    """Triángulos que se van juntando, separados por color."""

    heredado: list[float] = field(default_factory=list)
    fijos: dict[int, list[float]] = field(default_factory=dict)
    faltantes: set[str] = field(default_factory=set)
    triangulos: int = 0

    def anadir(self, color: int, puntos: Iterable[tuple[float, float, float]]) -> None:
        destino = (
            self.heredado
            if color in (COLOR_HEREDADO, COLOR_BORDE)
            else self.fijos.setdefault(color, [])
        )
        for x, y, z in puntos:
            # LDU -> studs, con la Y de LDraw (que apunta hacia abajo) invertida.
            destino.extend((x / LDU_POR_STUD, -y / LDU_POR_STUD, z / LDU_POR_STUD))
        self.triangulos += 1


def _transformar(matriz: tuple[float, ...], punto: tuple[float, float, float]):
    """Aplica la matriz 3x4 de una línea tipo 1 (rotación/escala + traslación)."""
    x, y, z = punto
    tx, ty, tz, a, b, c, d, e, f, g, h, i = matriz
    return (
        a * x + b * y + c * z + tx,
        d * x + e * y + f * z + ty,
        g * x + h * y + i * z + tz,
    )


def _componer(m1: tuple[float, ...], m2: tuple[float, ...]) -> tuple[float, ...]:
    """Encadena dos transformaciones: primero m2 (hija), luego m1 (padre)."""
    origen = _transformar(m1, (m2[0], m2[1], m2[2]))
    ejes = []
    for columna in range(3):
        base = (m2[3 + columna], m2[6 + columna], m2[9 + columna])
        x = m1[3] * base[0] + m1[4] * base[1] + m1[5] * base[2]
        y = m1[6] * base[0] + m1[7] * base[1] + m1[8] * base[2]
        z = m1[9] * base[0] + m1[10] * base[1] + m1[11] * base[2]
        ejes.append((x, y, z))
    return (
        origen[0], origen[1], origen[2],
        ejes[0][0], ejes[1][0], ejes[2][0],
        ejes[0][1], ejes[1][1], ejes[2][1],
        ejes[0][2], ejes[1][2], ejes[2][2],
    )


IDENTIDAD = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def _recorrer(
    nombre: str,
    matriz: tuple[float, ...],
    color_actual: int,
    acumulador: _Acumulador,
    profundidad: int,
) -> None:
    if profundidad > PROFUNDIDAD_MAXIMA:
        return
    contenido = obtener_dat(nombre)
    if contenido is None:
        acumulador.faltantes.add(nombre)
        return

    for linea in contenido.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        partes = linea.split()
        tipo = partes[0]

        if tipo == "1" and len(partes) >= 15:
            try:
                color = int(partes[1])
                numeros = tuple(float(v) for v in partes[2:14])
            except ValueError:
                continue
            hijo = " ".join(partes[14:])
            _recorrer(
                hijo,
                _componer(matriz, numeros),
                color_actual if color == COLOR_HEREDADO else color,
                acumulador,
                profundidad + 1,
            )

        elif tipo in ("3", "4"):
            esperados = 11 if tipo == "3" else 14
            if len(partes) < esperados:
                continue
            try:
                color = int(partes[1])
                valores = [float(v) for v in partes[2:esperados]]
            except ValueError:
                continue
            puntos = [
                _transformar(matriz, tuple(valores[i : i + 3]))  # type: ignore[arg-type]
                for i in range(0, len(valores), 3)
            ]
            efectivo = color_actual if color == COLOR_HEREDADO else color
            acumulador.anadir(efectivo, puntos[:3])
            if tipo == "4":
                # Un cuadrilátero son dos triángulos que comparten la diagonal.
                acumulador.anadir(efectivo, [puntos[0], puntos[2], puntos[3]])

        # Los tipos 2 y 5 son líneas de contorno: no aportan superficie.


def _perfil(posiciones_por_grupo: list[list[float]], caja: dict[str, list[float]]) -> dict[str, Any]:
    """Relieve de la pieza: qué altura ocupa en cada casilla de su planta.

    Sin esto una pieza se comporta como el ladrillo macizo que encierra su
    caja, y cualquier cosa apoyada junto a un saliente subiría hasta lo alto
    del saliente. Se guarda, por casilla de un stud, desde qué altura y hasta
    cuál hay material.
    """
    x0, y0, z0 = caja["min"]
    ancho = max(1, int(caja["max"][0] - x0 + 0.999))
    fondo = max(1, int(caja["max"][2] - z0 + 0.999))

    arriba: list[list[float | None]] = [[None] * ancho for _ in range(fondo)]
    abajo: list[list[float | None]] = [[None] * ancho for _ in range(fondo)]

    for posiciones in posiciones_por_grupo:
        for i in range(0, len(posiciones), 9):
            # Cada triángulo marca todas las casillas que toca su propia caja.
            xs = (posiciones[i], posiciones[i + 3], posiciones[i + 6])
            ys = (posiciones[i + 1], posiciones[i + 4], posiciones[i + 7])
            zs = (posiciones[i + 2], posiciones[i + 5], posiciones[i + 8])
            ci0 = max(0, min(ancho - 1, int((min(xs) - x0) // 1)))
            ci1 = max(0, min(ancho - 1, int((max(xs) - x0 - 0.001) // 1)))
            cj0 = max(0, min(fondo - 1, int((min(zs) - z0) // 1)))
            cj1 = max(0, min(fondo - 1, int((max(zs) - z0 - 0.001) // 1)))
            alto_min, alto_max = min(ys) - y0, max(ys) - y0
            for j in range(cj0, cj1 + 1):
                fila_arriba, fila_abajo = arriba[j], abajo[j]
                for k in range(ci0, ci1 + 1):
                    actual = fila_arriba[k]
                    fila_arriba[k] = alto_max if actual is None else max(actual, alto_max)
                    actual = fila_abajo[k]
                    fila_abajo[k] = alto_min if actual is None else min(actual, alto_min)

    return {
        "origen": [round(x0, 3), round(z0, 3)],
        "ancho": ancho,
        "fondo": fondo,
        "arriba": [[None if v is None else round(v, 3) for v in fila] for fila in arriba],
        "abajo": [[None if v is None else round(v, 3) for v in fila] for fila in abajo],
    }


def _caja(posiciones: list[float]) -> dict[str, list[float]]:
    minimos = [float("inf")] * 3
    maximos = [float("-inf")] * 3
    for i in range(0, len(posiciones), 3):
        for eje in range(3):
            valor = posiciones[i + eje]
            minimos[eje] = min(minimos[eje], valor)
            maximos[eje] = max(maximos[eje], valor)
    return {"min": minimos, "max": maximos}


def construir_malla(design_id: str, permitir_descarga: bool = True) -> dict[str, Any]:
    """Malla de una pieza, lista para dibujar. Lanza LDrawNoDisponible si no está.

    La pieza se devuelve apoyada en y=0 (su base) para que el diseñador pueda
    situarla directamente en la altura que le toque.
    """
    design_id = str(design_id).strip()
    if not design_id:
        raise LDrawNoDisponible("Falta el DesignID.")

    # El molde puede estar en LDraw con otro número.
    nombre_ldraw = alias_ldraw().get(design_id, design_id)

    if obtener_dat(f"{nombre_ldraw}.dat", permitir_descarga=permitir_descarga) is None:
        raise LDrawNoDisponible(f"La pieza {design_id} no está en la biblioteca LDraw.")

    acumulador = _Acumulador()
    _recorrer(f"{nombre_ldraw}.dat", IDENTIDAD, COLOR_HEREDADO, acumulador, 0)
    if not acumulador.heredado and not acumulador.fijos:
        raise LDrawNoDisponible(f"La pieza {design_id} no ha dado ninguna superficie.")

    todas = list(acumulador.heredado)
    for posiciones in acumulador.fijos.values():
        todas.extend(posiciones)
    caja = _caja(todas)

    # Se baja la pieza hasta apoyarla en y=0; en X y Z se respeta el origen de
    # LDraw, que es el centro del molde.
    desplazamiento_y = -caja["min"][1]

    def preparar(posiciones: list[float]) -> list[float]:
        salida = []
        for i in range(0, len(posiciones), 3):
            salida.append(round(posiciones[i], 3))
            salida.append(round(posiciones[i + 1] + desplazamiento_y, 3))
            salida.append(round(posiciones[i + 2], 3))
        return salida

    paleta = colores_ldraw()
    grupos = [{"color": None, "posiciones": preparar(acumulador.heredado)}]
    for codigo, posiciones in sorted(acumulador.fijos.items()):
        grupos.append(
            {
                "color": paleta.get(codigo),
                "codigo_ldraw": codigo,
                "posiciones": preparar(posiciones),
            }
        )
    grupos = [g for g in grupos if g["posiciones"]]

    caja_apoyada = {
        "min": [caja["min"][0], 0.0, caja["min"][2]],
        "max": [caja["max"][0], caja["max"][1] - caja["min"][1], caja["max"][2]],
    }
    perfil = _perfil([g["posiciones"] for g in grupos], caja_apoyada)

    return {
        "design_id": design_id,
        "ldraw_id": nombre_ldraw,
        "disponible": True,
        "triangulos": acumulador.triangulos,
        "medidas": {
            "ancho": round(caja["max"][0] - caja["min"][0], 3),
            "alto": round(caja["max"][1] - caja["min"][1], 3),
            "fondo": round(caja["max"][2] - caja["min"][2], 3),
        },
        "caja": {
            "min": [round(caja["min"][0], 3), 0.0, round(caja["min"][2], 3)],
            "max": [
                round(caja["max"][0], 3),
                round(caja["max"][1] - caja["min"][1], 3),
                round(caja["max"][2], 3),
            ],
        },
        "subpiezas_no_encontradas": sorted(acumulador.faltantes),
        "perfil": perfil,
        "grupos": grupos,
    }


# --------------------------------------------------------------------------
# Índice de medidas
# --------------------------------------------------------------------------
# Altura de un tetón, en studs. Se descuenta al medir una pieza porque el tetón
# no ocupa altura propia: se mete dentro del tubo de la pieza que va encima.
ALTO_TETON = 0.2
ALTO_PLACA = 0.4

_MEDIDAS: dict[str, dict[str, float]] | None = None


def _fichero_indice():
    return _dir_mallas() / "_medidas.json"


def indice_medidas() -> dict[str, dict[str, Any]]:
    """Medidas y relieve de cada molde ya convertido.

    Es un índice aparte para que el motor no tenga que cargar mallas de cientos
    de kilobytes sólo para saber cuánto ocupa una pieza.
    """
    global _MEDIDAS
    if _MEDIDAS is not None:
        return _MEDIDAS

    fichero = _fichero_indice()
    if fichero.is_file():
        try:
            _MEDIDAS = json.loads(fichero.read_text(encoding="utf-8"))
            return _MEDIDAS
        except json.JSONDecodeError:
            pass

    # Primera vez: se reconstruye leyendo las mallas y se deja guardado.
    _MEDIDAS = {}
    for ruta in _dir_mallas().glob("*.json"):
        if ruta.name.startswith("_"):
            continue
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if datos.get("medidas"):
            _MEDIDAS[datos["design_id"]] = {
                "medidas": datos["medidas"],
                "perfil": datos.get("perfil"),
            }
    _guardar_indice()
    return _MEDIDAS


def _guardar_indice() -> None:
    if _MEDIDAS is None:
        return
    _fichero_indice().write_text(json.dumps(_MEDIDAS, separators=(",", ":")), encoding="utf-8")


def medidas(design_id: str) -> dict[str, float] | None:
    """Ancho, alto y fondo reales de un molde, en studs. None si no se conoce."""
    entrada = indice_medidas().get(str(design_id).strip())
    return entrada.get("medidas") if entrada else None


def perfil(design_id: str) -> dict[str, Any] | None:
    """Relieve del molde por casillas, tal y como lo describe su malla."""
    entrada = indice_medidas().get(str(design_id).strip())
    return entrada.get("perfil") if entrada else None


def placas_de_alto(alto_bruto: float) -> int:
    """Altura de una pieza en placas, a partir de su altura real en studs.

    La medida bruta incluye los tetones de la cara superior, que no cuentan:
    encajan dentro de la pieza de encima. Se reconocen porque dejan la altura
    en un múltiplo de placa más media placa (0,6 una placa; 1,4 un ladrillo).
    """
    resto = alto_bruto % ALTO_PLACA
    cuerpo = alto_bruto - ALTO_TETON if abs(resto - ALTO_TETON) < 0.08 else alto_bruto
    return max(1, round(cuerpo / ALTO_PLACA))


# --------------------------------------------------------------------------
# Caché en disco
# --------------------------------------------------------------------------
def malla(design_id: str, permitir_descarga: bool = True) -> dict[str, Any]:
    """Malla de la pieza, del disco si ya se generó antes."""
    design_id = str(design_id).strip()
    fichero = _dir_mallas() / f"{design_id}.json"
    if fichero.is_file():
        try:
            return json.loads(fichero.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            fichero.unlink(missing_ok=True)

    datos = construir_malla(design_id, permitir_descarga=permitir_descarga)
    fichero.write_text(json.dumps(datos, separators=(",", ":")), encoding="utf-8")
    indice_medidas()[design_id] = {"medidas": datos["medidas"], "perfil": datos.get("perfil")}
    _guardar_indice()
    return datos


def malla_o_nada(design_id: str, permitir_descarga: bool = True) -> dict[str, Any] | None:
    """Como `malla`, pero devuelve None en vez de fallar: la pieza es opcional."""
    try:
        return malla(design_id, permitir_descarga=permitir_descarga)
    except (LDrawNoDisponible, OSError):
        return None


def hay_malla(design_id: str) -> bool:
    """¿Está ya generada la malla de esta pieza? (sin red ni parseo)"""
    return (_dir_mallas() / f"{str(design_id).strip()}.json").is_file()
