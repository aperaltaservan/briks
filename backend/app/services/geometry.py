"""De un nombre LEGO a una forma con volumen.

El catálogo sólo guarda texto ("BRICK 2X4", "FLAT TILE 1X2", "ROOF TILE 1X2
45°"). Para dibujar un modelo en 3D hace falta saber cuánto ocupa cada pieza,
así que aquí se deduce del propio nombre: dimensiones, altura y si lleva
tetones arriba.

Unidades, las mismas que usa LEGO:
  - 1 stud  = 8 mm    -> es la unidad de X y Z.
  - 1 plate = 3,2 mm  -> es la unidad de Y. Un ladrillo mide 3 placas.

Lo que no se puede deducir se marca `exacta=False` y se dibuja como una caja
aproximada: es preferible una caja honesta a una forma inventada.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

# Un ladrillo son tres placas de alto.
PLATES_POR_BRICK = 3

# Ninguna pieza normal pasa de seis ladrillos. Un número mayor en la tercera
# posición no es una altura: es un ángulo ("CORNER PLATE 6X6X45°").
ALTURA_MAXIMA_BRICKS = 6

# Familias que sabemos representar.
FAMILIA_BRICK = "brick"
FAMILIA_PLATE = "plate"
FAMILIA_TILE = "tile"
FAMILIA_SLOPE = "slope"
FAMILIA_ROUND = "round"
FAMILIA_OTRA = "otra"


@dataclass(frozen=True)
class Forma:
    """Volumen que ocupa una pieza sin rotar."""

    ancho: int  # studs en X
    fondo: int  # studs en Z
    alto: int  # placas en Y
    familia: str
    studs: bool  # ¿lleva tetones en la cara superior?
    exacta: bool  # ¿se dedujo del nombre con confianza?
    redonda: bool = False  # se dibuja cilíndrica, aunque ocupe la misma rejilla

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def rotada(self, rotacion: int) -> tuple[int, int]:
        """Ancho y fondo tras girar la pieza. 90° y 270° intercambian los ejes."""
        return (self.fondo, self.ancho) if rotacion % 180 == 90 else (self.ancho, self.fondo)


FORMA_DESCONOCIDA = Forma(1, 1, 3, FAMILIA_OTRA, True, False)


# --------------------------------------------------------------------------
# Lectura del nombre
# --------------------------------------------------------------------------
# Los nombres de LEGO usan fracciones tipográficas ("PLATE W. BOWS 2X1½"). Hay
# que separarlas antes de normalizar: NFKD las convierte en "1/2" pegado al
# número anterior, y "2X1½" acabaría leyéndose como una placa de 2x11.
_FRACCIONES = {
    "½": " 1/2", "⅓": " 1/3", "⅔": " 2/3", "¼": " 1/4", "¾": " 3/4",
    "⅕": " 1/5", "⅖": " 2/5", "⅗": " 3/5", "⅘": " 4/5", "⅙": " 1/6",
    "⅚": " 5/6", "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8",
}


def _limpiar(nombre: str | None) -> str:
    """Minúsculas, sin acentos y con la puntuación convertida en separadores."""
    if not nombre:
        return ""
    for simbolo, texto in _FRACCIONES.items():
        nombre = nombre.replace(simbolo, texto)
    nfkd = unicodedata.normalize("NFKD", nombre)
    texto = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    # La coma decimal europea ("2,5 x 4") se conserva como punto.
    texto = re.sub(r"(?<=\d),(?=\d)", ".", texto)
    texto = re.sub(r"[^a-z0-9x/.\s]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


# 2x4, 1x2x2/3, 1x1x3 1/3, 2.5x4
_NUM = r"\d+(?:\.\d+)?"
_ALTURA = rf"(?:{_NUM}\s+\d+/\d+|\d+/\d+|{_NUM})"
_DIMENSIONES = re.compile(rf"\b({_NUM})\s*x\s*({_NUM})(?:\s*x\s*({_ALTURA}))?\b")


def _a_numero(texto: str) -> float:
    """Convierte '2', '2/3' o '1 1/3' en un número."""
    total = 0.0
    for parte in texto.strip().split():
        if "/" in parte:
            numerador, _, denominador = parte.partition("/")
            total += float(numerador) / float(denominador or 1)
        else:
            total += float(parte)
    return total


def _redondear_studs(valor: float) -> tuple[int, bool]:
    """Los studs son enteros; si no lo era, la forma deja de ser exacta."""
    entero = max(1, int(valor + 0.999) if valor % 1 else int(valor))
    return entero, float(entero) == valor


# Palabras del nombre que fijan la familia. El orden importa: "flat tile" y
# "roof tile" son cosas distintas y ambas contienen "tile".
_FAMILIAS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("roof tile", "slope"), FAMILIA_SLOPE),
    (("flat tile", "tile"), FAMILIA_TILE),
    (("plate", "plade", "pl."), FAMILIA_PLATE),
    (("brick", "palisade"), FAMILIA_BRICK),
    (("cone", "cylinder", "round", "ball", "dish"), FAMILIA_ROUND),
)

# Altura por defecto de cada familia, en placas.
_ALTO_POR_FAMILIA = {
    FAMILIA_BRICK: PLATES_POR_BRICK,
    FAMILIA_PLATE: 1,
    FAMILIA_TILE: 1,
    FAMILIA_SLOPE: PLATES_POR_BRICK,
    FAMILIA_ROUND: 1,
    FAMILIA_OTRA: PLATES_POR_BRICK,
}


def _familia(texto: str) -> tuple[str, bool]:
    for palabras, familia in _FAMILIAS:
        for palabra in palabras:
            if palabra in texto:
                return familia, True
    return FAMILIA_OTRA, False


def forma_de_nombre(nombre: str | None) -> Forma:
    """Deduce el volumen de una pieza a partir de su nombre de catálogo."""
    texto = _limpiar(nombre)
    if not texto:
        return FORMA_DESCONOCIDA

    familia, familia_clara = _familia(texto)
    exacta = familia_clara

    ancho = fondo = 1
    alto = _ALTO_POR_FAMILIA[familia]

    match = _DIMENSIONES.search(texto)
    if match:
        ancho, ok_a = _redondear_studs(_a_numero(match.group(1)))
        fondo, ok_f = _redondear_studs(_a_numero(match.group(2)))
        exacta = exacta and ok_a and ok_f
        if match.group(3):
            # La tercera dimensión viene en ladrillos: 2/3 de ladrillo = 2 placas.
            bricks = _a_numero(match.group(3))
            if bricks > ALTURA_MAXIMA_BRICKS:
                # Son grados, no altura: se conserva la de la familia y se
                # avisa de que la forma no es fiable del todo.
                exacta = False
            else:
                placas = bricks * PLATES_POR_BRICK
                alto = max(1, round(placas))
                exacta = exacta and abs(placas - alto) < 0.01
    else:
        exacta = False

    # Un nombre con adornos ("W/ ARCH", "W. STICK") describe una pieza que no
    # es una caja limpia: la dibujamos igual, pero avisando.
    if any(marca in texto for marca in (" w/ ", " w. ", " with ", "/ ")):
        exacta = False

    # Las losetas son lisas por definición; el resto lleva tetones salvo que
    # el nombre diga lo contrario.
    studs = familia != FAMILIA_TILE and "smooth" not in texto

    # "PLATE 2X2 ROUND" sigue siendo una placa de una placa de alto, pero se
    # dibuja como cilindro.
    redonda = familia == FAMILIA_ROUND or any(
        p in texto for p in ("round", "circle", "cylinder", "dish")
    )

    return Forma(
        ancho=ancho,
        fondo=fondo,
        alto=alto,
        familia=familia,
        studs=studs,
        exacta=exacta,
        redonda=redonda,
    )


# --------------------------------------------------------------------------
# Ocupación en la rejilla
# --------------------------------------------------------------------------
def celdas(x: int, z: int, forma: Forma, rotacion: int = 0) -> set[tuple[int, int]]:
    """Casillas del tablero que ocupa una pieza colocada en (x, z)."""
    ancho, fondo = forma.rotada(rotacion)
    return {(x + dx, z + dz) for dx in range(ancho) for dz in range(fondo)}


def solapan(
    a_celdas: set[tuple[int, int]],
    a_base: int,
    a_alto: int,
    b_celdas: set[tuple[int, int]],
    b_base: int,
    b_alto: int,
) -> bool:
    """¿Dos piezas ocupan el mismo espacio?

    Comparten volumen si comparten alguna casilla y sus alturas se cruzan. Que
    una se apoye exactamente encima de otra no es un choque.
    """
    if a_base + a_alto <= b_base or b_base + b_alto <= a_base:
        return False
    return not a_celdas.isdisjoint(b_celdas)


def normalizar_rotacion(rotacion: int | None) -> int:
    """Sólo se admiten giros de 90°, que es lo que permite la rejilla."""
    valor = int(rotacion or 0) % 360
    return min((0, 90, 180, 270), key=lambda r: abs(r - valor))
