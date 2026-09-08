"""Pruebas de regresión sobre una base de datos temporal.

    python -m scripts.pruebas

No toca la base de datos real: crea una aparte y la borra al terminar.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

# La configuración se lee al importar, así que el destino se fija antes.
_TMP = Path(tempfile.mkdtemp(prefix="lego-test-"))
os.environ["LEGO_DB_PATH"] = str(_TMP / "prueba.db")

from app.db import init_db, session_scope  # noqa: E402
from app.services import (  # noqa: E402
    builds,
    catalog,
    designer,
    editor,
    geometry,
    historial,
    importers,
    inventory,
    ldraw,
    recognition,
)

fallos: list[str] = []
hechas = 0


def comprobar(condicion: bool, descripcion: str) -> None:
    global hechas
    hechas += 1
    if condicion:
        print(f"  ok   {descripcion}")
    else:
        print(f"  FALLA {descripcion}")
        fallos.append(descripcion)


def main() -> int:
    init_db()
    csv = sorted((RAIZ.parent / "seed").glob("*.csv"))

    print("\n[1] Importación de sets")
    with session_scope() as s:
        r1 = importers.import_set_csv_file(s, csv[0])
        comprobar(r1.piezas_totales == 144, f"el set {r1.set_number} trae 144 piezas")
        comprobar(not r1.errores, "importación sin errores")
        # Reimportar no debe duplicar las líneas del set.
        r2 = importers.import_set_csv_file(s, csv[0])
        comprobar(r2.piezas_totales == 144, "reimportar no duplica piezas")
        importers.import_set_csv_file(s, csv[1])

    with session_scope() as s:
        pieza = s.get(importers.Part, "6106")
        comprobar("°" in (pieza.name or ""), "el mojibake del CSV se repara (45°)")

    print("\n[2] Inventario y disponibilidad")
    with session_scope() as s:
        alta = inventory.add_set_to_inventory(s, "31134", location="caja A")
        comprobar(alta["piezas_anadidas"] == 144, "inventariar el set suma 144 piezas")
        disp = inventory.availability(s, ["302126"])["302126"]
        comprobar(disp["existencias"] == 1 and disp["disponible"] == 1, "disponible = existencias sin montajes")

    with session_scope() as s:
        try:
            inventory.add_stock(s, "302126", -99)
            comprobar(False, "retirar de más debe fallar")
        except inventory.InventoryError:
            comprobar(True, "retirar más de lo que hay se rechaza")
        try:
            inventory.add_stock(s, "no-existe", 1)
            comprobar(False, "elemento inexistente debe fallar")
        except inventory.InventoryError:
            comprobar(True, "no se puede inventariar un elemento inexistente")

    print("\n[3] Búsqueda en español")
    with session_scope() as s:
        mejor = catalog.search_elements(s, "placa 2x4", limit=1)
        comprobar(mejor and mejor[0].element.part.name == "PLATE 2X4", "'placa 2x4' -> PLATE 2X4")
        mejor = catalog.search_elements(s, "ladrillo 2x4", limit=1)
        comprobar(mejor and mejor[0].element.part.name == "BRICK 2X4", "'ladrillo 2x4' -> BRICK 2X4")
        rojos = catalog.search_elements(s, "loseta", color="rojo", limit=5)
        comprobar(all(c.element.color.name == "Bright Red" for c in rojos), "el filtro de color se respeta")

    print("\n[4] Montajes")
    with session_scope() as s:
        b = builds.create_build(s, "Prueba")
        builds.add_step(s, b["id"], "Uno", parts=[{"element_id": "302126", "cantidad": 1}])
        disp = inventory.availability(s, ["302126"])["302126"]
        comprobar(disp["reservado"] == 1 and disp["disponible"] == 0, "un montaje activo reserva sus piezas")
        try:
            builds.add_step(s, b["id"], "Dos", parts=[{"element_id": "302126", "cantidad": 1}])
            comprobar(False, "sin disponibilidad debe fallar")
        except builds.BuildError:
            comprobar(True, "no se puede reservar dos veces la misma pieza")
        comprobar(builds.check_build(s, b["id"])["viable"], "el montaje no se cuenta contra sí mismo")

        builds.set_build_status(s, b["id"], "desmontado")
        disp = inventory.availability(s, ["302126"])["302126"]
        comprobar(disp["disponible"] == 1, "desmontar libera las piezas")

    print("\n[5] Orden de los pasos")
    with session_scope() as s:
        b = builds.create_build(s, "Orden")
        for titulo in ("A", "B", "C"):
            builds.add_step(s, b["id"], titulo)
        d = builds.add_step(s, b["id"], "NUEVO", position=2)
        titulos = [p["titulo"] for p in d["pasos"]]
        comprobar(titulos == ["A", "NUEVO", "B", "C"], f"insertar en medio reordena bien ({titulos})")

        paso_b = next(p for p in d["pasos"] if p["titulo"] == "B")
        d2 = builds.remove_step(s, paso_b["id"])
        posiciones = [p["posicion"] for p in d2["pasos"]]
        comprobar(posiciones == [1, 2, 3], f"al borrar se recompactan las posiciones ({posiciones})")

    print("\n[6] Reconocimiento por foto")
    with session_scope() as s:
        rec = recognition.crear_sesion(s, [
            {"descripcion": "placa 2x3", "color": "negro", "cantidad": 2},
            {"descripcion": "chisme indescriptible", "cantidad": 1},
        ])
        comprobar(rec["resumen"]["resueltas"] == 1, "lo claro se resuelve solo")
        comprobar(rec["resumen"]["pendientes"] == 1, "lo dudoso queda pendiente")

        antes = inventory.inventory_summary(s)["piezas_totales"]
        conf = recognition.confirmar_sesion(s, rec["sesion_id"])
        despues = inventory.inventory_summary(s)["piezas_totales"]
        comprobar(despues - antes == 2, "confirmar suma sólo lo resuelto")
        try:
            recognition.confirmar_sesion(s, rec["sesion_id"])
            comprobar(False, "confirmar dos veces debe fallar")
        except recognition.RecognitionError:
            comprobar(True, "no se puede confirmar dos veces el mismo lote")

    print("\n[7] Alta manual")
    with session_scope() as s:
        color = catalog.get_or_create_color(s, "Bright Green")
        catalog.get_or_create_part(s, "99999", "PIEZA DE PRUEBA", "Bricks")
        el = catalog.get_or_create_element(s, "99999-x", "99999", color.id)
        inventory.add_stock(s, el.element_id, 5)
        encontrada = catalog.search_elements(s, "pieza de prueba", limit=1)
        comprobar(encontrada and encontrada[0].element.element_id == "99999-x", "la pieza dada de alta se encuentra")

    print("\n[8] Formas deducidas del nombre")
    for nombre, medidas, exacta in (
        ("BRICK 2X4", (2, 4, 3), True),
        ("PLATE 1X2", (1, 2, 1), True),
        ("FLAT TILE 1X8", (1, 8, 1), True),
        ("PLATE W. BOW 1X2X2/3", (1, 2, 2), False),
    ):
        f = geometry.forma_de_nombre(nombre)
        comprobar((f.ancho, f.fondo, f.alto) == medidas, f"'{nombre}' mide {medidas[0]}x{medidas[1]}x{medidas[2]} placas")
        comprobar(f.exacta == exacta, f"'{nombre}' {'es exacta' if exacta else 'queda marcada como aproximada'}")
    comprobar(not geometry.forma_de_nombre("FLAT TILE 1X2").studs, "una loseta no lleva tetones")
    comprobar(
        geometry.celdas(2, 3, geometry.forma_de_nombre("BRICK 2X4"), 90)
        == {(x, z) for x in range(2, 6) for z in range(3, 5)},
        "girar 90 grados intercambia ancho y fondo",
    )

    print("\n[9] Diseñador 3D")
    with session_scope() as s:
        b = builds.create_build(s, "Modelo")
        comprobar(b["placa"] == {"ancho": 32, "fondo": 32}, "la placa por defecto es 32x32")

        r = designer.colocar_piezas(
            s,
            b["id"],
            [{"element_id": "303026", "x": 0, "z": 0}, {"element_id": "302126", "x": 0, "z": 0}],
            titulo_paso="Base",
        )
        comprobar(r["colocadas"] == 2, "se colocan dos piezas en un paso")

        modelo = designer.modelo(s, b["id"], con_mapa=True)
        encima = next(p for p in modelo["piezas"] if p["element_id"] == "302126")
        comprobar(encima["y"] == 1, "sin indicar altura, la pieza se apoya sobre la de debajo")
        comprobar(bool(modelo["mapa"]) and bool(modelo["mapa"][0]["filas"]), "el mapa por capas dibuja la planta")

        try:
            designer.colocar_piezas(s, b["id"], [{"element_id": "302126", "x": 0, "z": 0, "y": 1}])
            comprobar(False, "dos piezas en el mismo hueco deben chocar")
        except designer.DesignError:
            comprobar(True, "una pieza no puede ocupar un hueco lleno")

        try:
            designer.colocar_piezas(s, b["id"], [{"element_id": "303026", "x": 31, "z": 31}])
            comprobar(False, "salirse de la placa debe fallar")
        except designer.DesignError:
            comprobar(True, "no se puede colocar fuera de la placa")

        detalle = builds.build_detail(s, b["id"])
        cantidades = {p["element_id"]: p["cantidad"] for p in detalle["pasos"][0]["piezas"]}
        comprobar(cantidades.get("303026") == 1, "colocar reserva la pieza en el paso")

        try:
            builds.update_step(s, detalle["pasos"][0]["id"], parts=[{"element_id": "303026"}])
            comprobar(False, "editar a mano un paso con modelo 3D debe rechazarse")
        except builds.BuildError:
            comprobar(True, "un paso con piezas colocadas no se edita a mano")

        designer.quitar_pieza(s, r["ids"][1])
        detalle = builds.build_detail(s, b["id"])
        ids = {p["element_id"] for p in detalle["pasos"][0]["piezas"]}
        comprobar("302126" not in ids, "quitar la pieza libera también su reserva")

        # Un paso escrito antes ya reserva sus piezas: darles sitio en el
        # modelo no puede apartarlas del inventario por segunda vez.
        paso = builds.add_step(
            s, b["id"], "Declarado a mano", parts=[{"element_id": "302126", "cantidad": 2}]
        )["pasos"][-1]
        reservado = inventory.availability(s, ["302126"])["302126"]["reservado"]
        designer.colocar_piezas(s, b["id"], [{"element_id": "302126", "x": 6, "z": 6}], paso_id=paso["id"])
        comprobar(
            inventory.availability(s, ["302126"])["302126"]["reservado"] == reservado,
            "colocar una pieza que el paso ya declaraba no la reserva dos veces",
        )
        detalle = builds.build_detail(s, b["id"])
        declarado = next(p for p in detalle["pasos"] if p["id"] == paso["id"])
        comprobar(
            declarado["piezas"][0]["cantidad"] == 2 and declarado["colocaciones"] == 1,
            "el paso sigue pidiendo 2 piezas y ya tiene 1 con sitio",
        )
        builds.remove_step(s, paso["id"])

        try:
            designer.configurar_placa(s, b["id"], 8, 8)
            comprobar(False, "encoger dejando piezas fuera debe fallar")
        except designer.DesignError:
            comprobar(True, "no se encoge la placa si algo quedaría fuera")
        comprobar(
            designer.configurar_placa(s, b["id"], 48, 48)["placa"]["ancho"] == 48,
            "la placa se puede agrandar",
        )

        ins = designer.instrucciones(s, b["id"])
        comprobar(ins["pasos"][0]["piezas_del_paso"][0]["cantidad"] == 1, "las instrucciones agrupan por pieza")
        comprobar(ins["total_piezas"] == 1, "las instrucciones cuentan las piezas colocadas")

        builds.delete_build(s, b["id"])

    print("\n[9b] Geometría real de las piezas (LDraw)")
    # Se monta una pieza de mentira en la caché local para no depender de la red:
    # una placa de 2x1 studs hecha con un cuadrilátero y una subpieza.
    biblioteca = Path(os.environ["LEGO_DB_PATH"]).parent / "ldraw" / "parts"
    biblioteca.mkdir(parents=True, exist_ok=True)
    (biblioteca / "s").mkdir(exist_ok=True)
    (biblioteca / "s" / "pruebasub.dat").write_text(
        "0 Subpieza de prueba\n"
        # Un cuadrilátero horizontal de 40x20 LDU (2x1 studs) a 8 LDU de altura.
        "4 16 -20 -8 -10  20 -8 -10  20 -8 10  -20 -8 10\n",
        encoding="utf-8",
    )
    (biblioteca / "pruebapieza.dat").write_text(
        "0 Pieza de prueba\n"
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 s/pruebasub.dat\n"
        # Un triángulo con color fijo (4 = rojo LDraw), al nivel del suelo.
        "3 4 -20 0 -10  20 0 -10  20 0 10\n",
        encoding="utf-8",
    )

    forma = ldraw.construir_malla("pruebapieza", permitir_descarga=False)
    comprobar(forma["triangulos"] == 3, f"un cuadrilátero y un triángulo dan 3 caras ({forma['triangulos']})")
    comprobar(
        forma["medidas"] == {"ancho": 2.0, "alto": 0.4, "fondo": 1.0},
        f"las LDU se convierten a studs y placas ({forma['medidas']})",
    )
    comprobar(forma["caja"]["min"][1] == 0.0, "la pieza queda apoyada en y=0")
    comprobar(len(forma["grupos"]) == 2, "el color propio del molde va en su propio grupo")
    comprobar(
        forma["grupos"][0]["color"] is None,
        "el grupo heredado se pintará con el color del elemento",
    )
    comprobar(
        ldraw.malla_o_nada("no-existe-esta-pieza", permitir_descarga=False) is None,
        "una pieza que LDraw no tiene se resuelve como None, no como error",
    )
    comprobar(
        ldraw.placas_de_alto(0.6) == 1 and ldraw.placas_de_alto(1.4) == 3,
        "la altura real descuenta el tetón: 0,6 es una placa y 1,4 un ladrillo",
    )
    comprobar(
        geometry.forma_de_nombre("PLATE W. BOWS 2X1½").fondo == 1,
        "una fracción tipográfica no convierte un 2X1½ en un 2x11",
    )

    print("\n[9c] La altura y el relieve mandan sobre el nombre")
    with session_scope() as s:
        color = catalog.get_or_create_color(s, "Bright Red")
        # Un molde inventado con forma de escalón: bajo por un lado, alto por el
        # otro. Es el caso que el nombre nunca podría describir.
        catalog.get_or_create_part(s, "escalon", "CORNER PLATE 1X2X2", "Plates")
        escalon = catalog.get_or_create_element(s, "escalon-1", "escalon", color.id)
        ldraw.indice_medidas()["escalon"] = {
            "medidas": {"ancho": 1.0, "alto": 1.2, "fondo": 2.0},
            "perfil": {
                "origen": [-0.5, -1.0],
                "ancho": 1,
                "fondo": 2,
                "arriba": [[0.4], [1.2]],
                "abajo": [[0.0], [0.0]],
            },
        }
        designer._PERFILES.clear()

        forma = designer.forma_de("CORNER PLATE 1X2X2", "escalon")
        comprobar(
            forma.alto == 3,
            f"el nombre decía 6 placas de alto y la geometría real dice 3 ({forma.alto})",
        )
        relieve = designer.perfil_de("CORNER PLATE 1X2X2", "escalon")
        comprobar(
            relieve == {(0, 0): (0, 1), (0, 1): (0, 3)},
            f"el relieve distingue el lado bajo del alto ({relieve})",
        )

        inventory.add_stock(s, escalon.element_id, 2)
        inventory.add_stock(s, "302126", 2)  # PLATE 2X3, para apoyar encima
        b = builds.create_build(s, "Escalón")
        designer.colocar_piezas(s, b["id"], [{"element_id": escalon.element_id, "x": 5, "z": 5}])

        # Encima del lado bajo cae a 1 placa; encima del alto, a 3.
        bajo = designer.colocar_piezas(
            s, b["id"], [{"element_id": "302126", "x": 5, "z": 4, "rotacion": 90}]
        )
        alto = designer.colocar_piezas(
            s, b["id"], [{"element_id": "302126", "x": 5, "z": 6, "rotacion": 90}]
        )
        piezas = {p["id"]: p for p in designer.modelo(s, b["id"])["piezas"]}
        comprobar(
            piezas[bajo["ids"][0]]["y"] == 1,
            f"apoyada sobre el lado bajo, la pieza cae a 1 placa ({piezas[bajo['ids'][0]]['y']})",
        )
        comprobar(
            piezas[alto["ids"][0]]["y"] == 3,
            f"apoyada sobre el lado alto, cae a 3 placas ({piezas[alto['ids'][0]]['y']})",
        )
        # Una pieza a caballo del borde se sostiene, pero hay que avisar: casi
        # siempre es que se quería poner una casilla más allá.
        b2 = builds.create_build(s, "Voladizo")
        designer.colocar_piezas(s, b2["id"], [{"element_id": "302126", "x": 10, "z": 10}])
        volado = designer.colocar_piezas(
            s, b2["id"], [{"element_id": escalon.element_id, "x": 11, "z": 12, "y": 1}]
        )
        comprobar(
            any("voladizo" in a for a in volado.get("avisos", [])),
            f"se avisa de la pieza a medio apoyar ({volado.get('avisos')})",
        )
        builds.delete_build(s, b2["id"])
        builds.delete_build(s, b["id"])

    print("\n[9d] La base también tiene relieve: una placa cabe bajo una curva")
    with session_scope() as s:
        color = catalog.get_or_create_color(s, "White")
        # Un molde 1x2 con la base escalonada, como la placa curva 2x2x2/3: la
        # primera casilla apoya en el suelo y la segunda tiene la base una placa
        # más alta (por debajo sólo quedan unos labios de medio stud).
        catalog.get_or_create_part(s, "curva", "PLATE W. BOW 1X2X2/3", "Plates")
        curva = catalog.get_or_create_element(s, "curva-1", "curva", color.id)
        ldraw.indice_medidas()["curva"] = {
            "medidas": {"ancho": 1.0, "alto": 0.8, "fondo": 2.0},
            "perfil": {
                "origen": [-0.5, -1.0],
                "ancho": 1,
                "fondo": 2,
                "arriba": [[0.8], [0.8]],
                "abajo": [[0.0], [0.2]],
            },
        }
        designer._PERFILES.clear()
        relieve = designer.perfil_de("PLATE W. BOW 1X2X2/3", "curva")
        comprobar(
            relieve == {(0, 0): (0, 2), (0, 1): (1, 2)},
            f"la casilla trasera empieza una placa más arriba ({relieve})",
        )

        inventory.add_stock(s, curva.element_id, 2)
        b = builds.create_build(s, "Curva sobre placa")
        # Una placa 2x3 en el suelo, y la curva de modo que su mitad trasera
        # (la hueca) quede encima de la placa y la delantera en el suelo.
        designer.colocar_piezas(s, b["id"], [{"element_id": "302126", "x": 5, "z": 6}])
        r = designer.colocar_piezas(s, b["id"], [{"element_id": curva.element_id, "x": 5, "z": 5}])
        puesta = r["detalle"][0]
        comprobar(
            puesta["y"] == 0,
            f"la curva se queda en el suelo con la placa metida en su hueco (y={puesta['y']})",
        )
        comprobar(
            "PLATE 2X3" in " ".join(puesta["apoyada_sobre"]),
            "y consta que descansa sobre la placa",
        )
        # Girada 180 y con la mitad maciza encima de la placa (en (6,8)), sí
        # tiene que subir una placa.
        r2 = designer.colocar_piezas(
            s, b["id"], [{"element_id": curva.element_id, "x": 6, "z": 7, "rotacion": 180}]
        )
        comprobar(
            r2["detalle"][0]["y"] == 1,
            f"con la parte maciza encima de la placa sube a 1 ({r2['detalle'][0]['y']})",
        )
        builds.delete_build(s, b["id"])

    print("\n[9e] Editor: rectificar lo ya construido")
    with session_scope() as s:
        # Piezas de sobra para no depender de lo que hayan dejado las pruebas
        # anteriores, y el mismo molde en otro color para poder repintar.
        inventory.add_stock(s, "302126", 10)
        rojo = catalog.get_or_create_color(s, "Bright Red", "#d01012")
        catalog.get_or_create_element(s, "302121", "3021", rojo.id)
        inventory.add_stock(s, "302121", 4)

        b = builds.create_build(s, "Editable")
        r = designer.colocar_piezas(
            s,
            b["id"],
            [{"element_id": "302126", "x": 4, "z": 4}, {"element_id": "302126", "x": 4, "z": 7}],
            titulo_paso="Suelo",
        )
        ids = r["ids"]

        editor.mover(s, b["id"], ids, dx=2)
        piezas = {p["id"]: p for p in designer.modelo(s, b["id"])["piezas"]}
        comprobar(
            all(piezas[i]["x"] == 6 for i in ids),
            "el grupo se mueve entero sin chocar consigo mismo",
        )
        try:
            editor.mover(s, b["id"], ids, dx=40)
            comprobar(False, "sacar el grupo de la placa debe fallar")
        except designer.DesignError:
            comprobar(True, "un movimiento que se sale de la placa se rechaza")
        editor.mover(s, b["id"], ids, dx=-2)

        # Dos PLATE 2X3 en (4,4) y (4,7) forman un bloque de 2x6; girarlo lo
        # deja de 6x2 en la misma esquina.
        editor.girar(s, b["id"], ids, grados=90)
        piezas = {p["id"]: p for p in designer.modelo(s, b["id"])["piezas"]}
        comprobar(
            {piezas[i]["x"] for i in ids} == {4, 7}
            and all(piezas[i]["z"] == 4 and piezas[i]["rotacion"] == 90 for i in ids),
            f"el grupo gira como un bloque ({[(piezas[i]['x'], piezas[i]['z']) for i in ids]})",
        )
        editor.girar(s, b["id"], ids, grados=270)  # se deshace a mano el giro

        antes = inventory.availability(s, ["302126"])["302126"]["disponible"]
        copia = editor.duplicar(s, b["id"], ids, dx=4)
        comprobar(copia["afectadas"] == 2, "duplicar crea una copia de cada pieza")
        comprobar(
            inventory.availability(s, ["302126"])["302126"]["disponible"] == antes - 2,
            "las copias reservan piezas del inventario",
        )
        try:
            editor.duplicar(s, b["id"], ids, dx=4)
            comprobar(False, "duplicar sobre las copias debe chocar")
        except designer.DesignError:
            comprobar(True, "una copia encima de otra pieza se rechaza")

        # Repintar: mismo molde, otro color. La reserva se mueve con la pieza.
        editor.sustituir(s, b["id"], [ids[0]], color="rojo")
        piezas = {p["id"]: p for p in designer.modelo(s, b["id"])["piezas"]}
        comprobar(piezas[ids[0]]["element_id"] == "302121", "sustituir por color repinta la pieza")
        comprobar(
            inventory.availability(s, ["302121"])["302121"]["reservado"] == 1,
            "la pieza nueva queda reservada",
        )
        comprobar(
            inventory.availability(s, ["302126"])["302126"]["disponible"] == antes - 1,
            "y la vieja vuelve al fondo común",
        )

        espejo = editor.reflejar(s, b["id"], ids, eje="x")
        comprobar(espejo["afectadas"] == 2, "reflejar mueve el grupo al otro lado del espejo")

        seleccion = editor.seleccionar(s, b["id"], color="Bright Red")
        comprobar(seleccion["total"] == 1 and seleccion["ids"] == [ids[0]], "se busca por color dentro del modelo")
        comprobar(
            editor.seleccionar(s, b["id"], y=0)["total"] == 4,
            "se busca por capa: las cuatro piezas están apoyadas en el suelo",
        )

        # Mover de paso no toca el modelo, sólo las instrucciones.
        nuevo_paso = builds.add_step(s, b["id"], "Detalle")["pasos"][-1]
        reservado_antes = inventory.availability(s, ["302126"])["302126"]["reservado"]
        editor.cambiar_de_paso(s, b["id"], [ids[1]], paso_id=nuevo_paso["id"])
        detalle = builds.build_detail(s, b["id"])
        del_paso = next(p for p in detalle["pasos"] if p["id"] == nuevo_paso["id"])
        comprobar(del_paso["colocaciones"] == 1, "la pieza cambia de paso")
        comprobar(
            inventory.availability(s, ["302126"])["302126"]["reservado"] == reservado_antes,
            "cambiar de paso no altera lo reservado",
        )

        print("\n[9f] Deshacer y rehacer")
        total_antes = designer.modelo(s, b["id"])["total_piezas"]
        libre_antes = inventory.availability(s, ["302126"])["302126"]["disponible"]
        editor.quitar(s, b["id"], ids)
        comprobar(
            designer.modelo(s, b["id"])["total_piezas"] == total_antes - 2,
            "quitar en lote retira las dos piezas",
        )
        vuelta = historial.deshacer(s, b["id"])
        comprobar(vuelta["deshecho"], "deshacer encuentra la operación anterior")
        modelo = designer.modelo(s, b["id"])
        comprobar(modelo["total_piezas"] == total_antes, "deshacer devuelve las piezas al modelo")
        comprobar(
            {p["id"] for p in modelo["piezas"]} >= set(ids),
            "y conserva los ids de colocación, que son la referencia del chat",
        )
        comprobar(
            inventory.availability(s, ["302126"])["302126"]["disponible"] == libre_antes,
            "deshacer también restaura las reservas",
        )
        rehecho = historial.rehacer(s, b["id"])
        comprobar(rehecho["rehecho"], "rehacer repite lo deshecho")
        comprobar(
            designer.modelo(s, b["id"])["total_piezas"] == total_antes - 2,
            "y el modelo vuelve a quedarse sin esas piezas",
        )
        comprobar(
            not historial.estado(s, b["id"])["puede_rehacer"],
            "la pila de rehacer se agota al usarla",
        )
        historial.deshacer(s, b["id"])
        designer.colocar_piezas(s, b["id"], [{"element_id": "302126", "x": 20, "z": 20}])
        comprobar(
            not historial.estado(s, b["id"])["puede_rehacer"],
            "una operación nueva anula lo que quedaba por rehacer",
        )

        builds.delete_build(s, b["id"])

    # Va al final a propósito: confirmar duplica el inventario y falsearía
    # cualquier comprobación posterior.
    print("\n[10] Un set ya inventariado pide confirmación")
    with session_scope() as s:
        try:
            inventory.add_set_to_inventory(s, "31134")
            comprobar(False, "reinventariar sin confirmar debe rechazarse")
        except inventory.SetYaInventariadoError as exc:
            comprobar(True, "reinventariar sin confirmar se rechaza")
            comprobar(exc.detalle["piezas_anadidas"] == 144, "el aviso dice cuántas piezas se sumaron ya")
        antes = inventory.inventory_summary(s)["piezas_totales"]
        repetido = inventory.add_set_to_inventory(s, "31134", confirmar=True)
        despues = inventory.inventory_summary(s)["piezas_totales"]
        comprobar(repetido["repetido"], "el alta repetida se marca como tal")
        comprobar(despues - antes == 144, "confirmando sí se suman otra vez")

    print(f"\n{'='*56}\n{hechas - len(fallos)}/{hechas} comprobaciones correctas")
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print("  -", f)
    return 1 if fallos else 0


def limpiar() -> None:
    """Cierra el motor y borra la base de datos temporal."""
    import shutil

    from app.db import engine

    engine.dispose()
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        limpiar()
    raise SystemExit(codigo)
