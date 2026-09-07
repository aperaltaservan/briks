"""Puente entre el lenguaje natural (español) y la nomenclatura oficial LEGO.

LEGO no llama a las cosas como nosotros: una "placa" es un PLATE, un "beige" es
"Brick Yellow" y un "gris claro" es "Medium Stone Grey". Este módulo traduce lo
que se escribe en el chat a los términos que realmente están en el catálogo.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# --------------------------------------------------------------------------
# Normalización
# --------------------------------------------------------------------------
def normalize(text: str | None) -> str:
    """Minúsculas, sin acentos y con espacios colapsados."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    sin_acentos = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpio = re.sub(r"[^a-z0-9x/°\s\.-]", " ", sin_acentos.lower())
    return re.sub(r"\s+", " ", limpio).strip()


def tokens(text: str | None) -> list[str]:
    return [t for t in normalize(text).split(" ") if t]


# --------------------------------------------------------------------------
# Morfología: "roja", "rojas" y "rojos" deben encontrar el alias "rojo"
# --------------------------------------------------------------------------
def _variantes_palabra(palabra: str) -> set[str]:
    """Variantes de género y número de una palabra española."""
    v = {palabra}
    if palabra.endswith("o"):
        raiz = palabra[:-1]
        v |= {raiz + "a", raiz + "os", raiz + "as"}
    elif palabra.endswith("a"):
        raiz = palabra[:-1]
        v |= {raiz + "o", raiz + "as", raiz + "os"}
    elif palabra.endswith("e"):
        v.add(palabra + "s")
    elif palabra.endswith("s"):
        # Puede ser un plural ("placas") o un singular acabado en s ("gris").
        v |= {palabra[:-1], palabra + "es"}
    else:
        v |= {palabra + "s", palabra + "es"}
    return v


@lru_cache(maxsize=512)
def _alias_regex(alias: str) -> re.Pattern[str]:
    """Regex que reconoce un alias y sus variantes morfológicas."""
    partes = []
    for palabra in normalize(alias).split(" "):
        opciones = sorted(_variantes_palabra(palabra), key=len, reverse=True)
        partes.append("(?:" + "|".join(re.escape(o) for o in opciones) + ")")
    return re.compile(r"\b" + r"\s+".join(partes) + r"\b")


def _menciona(alias: str, texto_normalizado: str) -> bool:
    return bool(_alias_regex(alias).search(texto_normalizado))


# --------------------------------------------------------------------------
# Colores: alias en español -> nombres oficiales LEGO, en orden de preferencia
# --------------------------------------------------------------------------
COLOR_ALIASES: dict[str, list[str]] = {
    "blanco": ["White"],
    "negro": ["Black"],
    "rojo": ["Bright Red", "Dark Red", "New Dark Red"],
    "rojo oscuro": ["Dark Red", "New Dark Red"],
    "azul": ["Bright Blue", "Earth Blue", "Medium Azur"],
    "azul oscuro": ["Earth Blue"],
    "azul claro": ["Light Royal Blue", "Medium Azur", "Transparent Light Blue"],
    "azul celeste": ["Medium Azur", "Light Royal Blue"],
    "turquesa": ["Aqua", "Medium Azur", "Bright Bluish Green"],
    "amarillo": ["Bright Yellow", "Cool Yellow"],
    "amarillo claro": ["Cool Yellow"],
    "verde": ["Bright Green", "Dark Green", "Earth Green"],
    "verde oscuro": ["Earth Green", "Dark Green"],
    "verde claro": ["Bright Yellowish Green", "Spring Yellowish Green"],
    "verde lima": ["Bright Yellowish Green"],
    "gris": ["Medium Stone Grey", "Dark Stone Grey"],
    "gris claro": ["Medium Stone Grey"],
    "gris oscuro": ["Dark Stone Grey"],
    "beige": ["Brick Yellow"],
    "arena": ["Brick Yellow", "Sand Yellow"],
    "crema": ["Brick Yellow"],
    "marron": ["Reddish Brown", "Dark Brown"],
    "marron claro": ["Medium Nougat", "Nougat"],
    "naranja": ["Bright Orange", "Flame Yellowish Orange"],
    "rosa": ["Light Purple", "Bright Pink"],
    "morado": ["Medium Lilac", "Bright Reddish Violet"],
    "lila": ["Medium Lilac", "Light Purple"],
    "purpura": ["Medium Lilac"],
    "violeta": ["Medium Lilac", "Bright Reddish Violet"],
    "dorado": ["Warm Gold", "Metallic Gold"],
    "oro": ["Warm Gold", "Metallic Gold"],
    "plateado": ["Silver Metallic", "Metallic Silver"],
    "plata": ["Silver Metallic", "Metallic Silver"],
    "transparente": ["Transparent"],
    "cristal": ["Transparent"],
    "piel": ["Nougat", "Light Nougat"],
    "carne": ["Nougat", "Light Nougat"],
}

# Alias en inglés corriente -> nombre oficial LEGO. Mucha gente (y Rebrickable)
# usa "Tan" o "Light Bluish Gray" en vez de la nomenclatura interna de LEGO.
COLOR_ALIASES_EN: dict[str, list[str]] = {
    "red": ["Bright Red"],
    "blue": ["Bright Blue"],
    "yellow": ["Bright Yellow"],
    "green": ["Bright Green", "Dark Green"],
    "tan": ["Brick Yellow"],
    "light bluish gray": ["Medium Stone Grey"],
    "light bluish grey": ["Medium Stone Grey"],
    "dark bluish gray": ["Dark Stone Grey"],
    "dark bluish grey": ["Dark Stone Grey"],
    "light gray": ["Medium Stone Grey"],
    "dark gray": ["Dark Stone Grey"],
    "grey": ["Medium Stone Grey"],
    "gray": ["Medium Stone Grey"],
    "brown": ["Reddish Brown"],
    "orange": ["Bright Orange"],
    "pink": ["Light Purple", "Bright Pink"],
    "purple": ["Medium Lilac"],
    "gold": ["Warm Gold"],
    "silver": ["Silver Metallic"],
    "trans clear": ["Transparent"],
    "clear": ["Transparent"],
}

ALL_COLOR_ALIASES: dict[str, list[str]] = {**COLOR_ALIASES, **COLOR_ALIASES_EN}


# --------------------------------------------------------------------------
# Formas: término coloquial -> palabras que aparecen en los nombres LEGO
# --------------------------------------------------------------------------
SHAPE_ALIASES: dict[str, list[str]] = {
    "placa": ["plate"],
    "placas": ["plate"],
    "plancha": ["plate"],
    "ladrillo": ["brick"],
    "ladrillos": ["brick"],
    "bloque": ["brick"],
    "loseta": ["tile"],
    "losetas": ["tile"],
    "azulejo": ["tile"],
    "baldosa": ["tile"],
    "teja": ["roof", "tile"],
    "rampa": ["roof", "tile"],
    "inclinada": ["roof", "tile"],
    "cuna": ["wedge"],
    "cuña": ["wedge"],
    "eje": ["shaft", "axle"],
    "viga": ["beam", "brick"],
    "conector": ["connector"],
    "bisagra": ["hinge"],
    "soporte": ["bracket"],
    "escuadra": ["bracket"],
    "rueda": ["wheel"],
    "ruedas": ["wheel"],
    "neumatico": ["tyre", "tire"],
    "llanta": ["rim", "wheel"],
    "ventana": ["window"],
    "puerta": ["door"],
    "valla": ["fence"],
    "escalera": ["ladder", "stairs"],
    "pinza": ["clip"],
    "clip": ["clip"],
    "barra": ["bar"],
    "palo": ["bar"],
    "antena": ["antenna", "bar"],
    "cono": ["cone"],
    "cilindro": ["cylinder", "round"],
    "redondo": ["round"],
    "redonda": ["round"],
    "circulo": ["circle", "round"],
    "arco": ["arch", "bow"],
    "curva": ["bow", "curved"],
    "pin": ["pin", "peg"],
    "tornillo": ["screw"],
    "engranaje": ["gear"],
    "pinon": ["gear"],
    "cadena": ["chain"],
    "cuerda": ["string"],
    "planta": ["plant"],
    "arbol": ["tree", "plant"],
    "flor": ["flower", "plant"],
    "hoja": ["leaf", "plant"],
    "minifigura": ["mini", "figure"],
    "figura": ["figure"],
    "cabeza": ["head"],
    "pelo": ["hair"],
    "casco": ["helmet"],
    "torso": ["torso", "upper part"],
    "pierna": ["leg", "hip"],
    "brazo": ["arm"],
    "mano": ["hand"],
    "sombrero": ["hat"],
    "espada": ["sword"],
    "esquina": ["corner"],
    "angulo": ["angle", "corner"],
    "tenton": ["knob"],
    "teton": ["knob"],
    "liso": ["smooth"],
    "modificado": ["w.", "with"],
    "con": ["w."],
}

# Categorías del catálogo LEGO tal y como llegan en los CSV.
CATEGORY_ALIASES: dict[str, str] = {
    "placas": "Plates",
    "plates": "Plates",
    "ladrillos": "Bricks",
    "bricks": "Bricks",
    "conectores": "Connectors",
    "connectors": "Connectors",
    "animales": "Animals & Nature",
    "naturaleza": "Animals & Nature",
    "plantas": "Animals & Nature",
    "edificios": "Buildings & Furniture",
    "muebles": "Buildings & Furniture",
    "vehiculos": "Vehicles & Transportation",
    "transporte": "Vehicles & Transportation",
    "accesorios": "Minifigure Accessories",
    "minifiguras": "Minifigure Accessories",
    "varios": "Miscellaneous",
}


# --------------------------------------------------------------------------
# Extracción de pistas de una descripción libre
# --------------------------------------------------------------------------
DIMENSION_RE = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)(?:\s*[x×]\s*([\d/]+))?\b")


def extract_dimensions(text: str) -> list[str]:
    """Devuelve las dimensiones detectadas normalizadas al estilo LEGO ('2X4')."""
    encontradas: list[str] = []
    for match in DIMENSION_RE.finditer(normalize(text)):
        partes = [p for p in match.groups() if p]
        encontradas.append("x".join(partes))
    return encontradas


def expand_shape_terms(text: str) -> list[str]:
    """Traduce los términos coloquiales de la consulta a palabras del catálogo."""
    resultado: list[str] = []
    normalizado = normalize(text)
    for alias, destinos in SHAPE_ALIASES.items():
        if _menciona(alias, normalizado):
            for destino in destinos:
                if destino not in resultado:
                    resultado.append(destino)
    return resultado


def resolve_color_names(text: str | None) -> list[str]:
    """Devuelve los nombres LEGO candidatos para un color escrito en lenguaje natural.

    Prueba primero los alias compuestos ('gris oscuro') antes que los simples
    ('gris'), para no perder el matiz.
    """
    if not text:
        return []
    normalizado = normalize(text)
    candidatos: list[str] = []

    for alias in sorted(ALL_COLOR_ALIASES, key=len, reverse=True):
        alias_norm = normalize(alias)
        if _menciona(alias, normalizado):
            for nombre in ALL_COLOR_ALIASES[alias]:
                if nombre not in candidatos:
                    candidatos.append(nombre)
            # Un alias compuesto ya es suficientemente específico.
            if " " in alias_norm:
                break
    return candidatos


def resolve_category(text: str | None) -> str | None:
    if not text:
        return None
    normalizado = normalize(text)
    for alias, categoria in CATEGORY_ALIASES.items():
        if _menciona(alias, normalizado):
            return categoria
    return None


# --------------------------------------------------------------------------
# Códigos de color aproximados, para pintar la interfaz
# --------------------------------------------------------------------------
COLOR_HEX: dict[str, str] = {
    "White": "#F2F3F2",
    "Black": "#1B2A34",
    "Bright Red": "#C91A09",
    "Dark Red": "#720E0F",
    "New Dark Red": "#720E0F",
    "Bright Blue": "#0055BF",
    "Earth Blue": "#0A3463",
    "Light Royal Blue": "#9FC3E9",
    "Medium Azur": "#36AEBF",
    "Aqua": "#B3D7D1",
    "Bright Bluish Green": "#008F9B",
    "Bright Yellow": "#F2CD37",
    "Cool Yellow": "#FBE696",
    "Bright Yellowish Green": "#BBE90B",
    "Spring Yellowish Green": "#C7D23C",
    "Bright Green": "#4B9F4A",
    "Dark Green": "#237841",
    "Earth Green": "#184632",
    "Brick Yellow": "#D9BB7B",
    "Sand Yellow": "#DCC48E",
    "Nougat": "#D09168",
    "Light Nougat": "#F6D7B3",
    "Medium Nougat": "#CC8E69",
    "Reddish Brown": "#582A12",
    "Dark Brown": "#352100",
    "Bright Orange": "#FE8A18",
    "Flame Yellowish Orange": "#F8BB3D",
    "Light Purple": "#E4ADC8",
    "Bright Pink": "#E4ADC8",
    "Medium Lilac": "#6C0FA9",
    "Bright Reddish Violet": "#923978",
    "Warm Gold": "#DBAC34",
    "Metallic Gold": "#DBAC34",
    "Silver Metallic": "#A5A9B4",
    "Metallic Silver": "#A5A9B4",
    "Medium Stone Grey": "#A0A5A9",
    "Dark Stone Grey": "#6C6E68",
    "Transparent": "#FCFCFC",
    "Transparent Bright Orange": "#F08F1C",
    "Transparent Brown": "#635F52",
    "Transparent Light Blue": "#AEEFEC",
}


def hex_for_color(nombre: str | None) -> str | None:
    """Código hex de un color LEGO, si lo conocemos."""
    if not nombre:
        return None
    if nombre in COLOR_HEX:
        return COLOR_HEX[nombre]
    objetivo = normalize(nombre)
    for clave, valor in COLOR_HEX.items():
        if normalize(clave) == objetivo:
            return valor
    return None
