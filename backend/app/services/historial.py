"""Deshacer y rehacer en el diseñador.

Antes de tocar el modelo, cada operación guarda una foto de lo que había:
las colocaciones, lo que cada paso tiene reservado y el tamaño de la placa.
Deshacer es volver a poner esa foto.

Guardar el estado entero es más simple que anotar el inverso de cada
operación —y sobre todo, es igual de válido para lo que se hace desde la web
que para lo que hace el chat, porque ambos pasan por los mismos servicios: si
el chat coloca veinte piezas, deshacer desde la web las quita.

Cada montaje tiene dos pilas. Una operación nueva apila en «deshacer» y vacía
«rehacer», que es lo que espera cualquiera que haya usado un editor.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    Build,
    BuildStep,
    BuildStepPart,
    DesignSnapshot,
    Placement,
)

# Más allá de esto, deshacer deja de ser útil y la base de datos crece sin
# motivo: cada foto lleva el modelo entero.
MAX_PILA = 30


# --------------------------------------------------------------------------
# La foto
# --------------------------------------------------------------------------
def capturar(session: Session, build_id: int) -> dict[str, Any]:
    """El estado del modelo ahora mismo, en datos planos."""
    build = session.get(Build, int(build_id))
    if build is None:
        raise ValueError(f"No existe el montaje {build_id}.")

    colocaciones = [
        {
            "id": p.id,
            "paso_id": p.step_id,
            "element_id": p.element_id,
            "x": p.x,
            "y": p.y,
            "z": p.z,
            "rotacion": p.rotation,
        }
        for p in session.scalars(
            select(Placement).where(Placement.build_id == build.id).order_by(Placement.id)
        )
    ]
    reservas = [
        {"paso_id": linea.step_id, "element_id": linea.element_id, "cantidad": linea.quantity}
        for linea in session.scalars(
            select(BuildStepPart)
            .join(BuildStep, BuildStep.id == BuildStepPart.step_id)
            .where(BuildStep.build_id == build.id)
        )
    ]
    return {
        "placa": {"ancho": build.baseplate_w, "fondo": build.baseplate_d},
        "estado": build.status,
        "colocaciones": colocaciones,
        "reservas": reservas,
    }


def _restaurar(session: Session, build_id: int, datos: dict[str, Any]) -> dict[str, Any]:
    """Deja el modelo tal y como estaba en la foto."""
    build = session.get(Build, int(build_id))
    if build is None:
        raise ValueError(f"No existe el montaje {build_id}.")

    pasos = {
        paso.id: paso
        for paso in session.scalars(select(BuildStep).where(BuildStep.build_id == build.id))
    }
    # Un paso puede haberse borrado después de tomar la foto. Antes que perder
    # las piezas, se recogen en el último paso que quede.
    refugio = max(pasos.values(), key=lambda p: p.position).id if pasos else None

    for placement in session.scalars(select(Placement).where(Placement.build_id == build.id)):
        session.delete(placement)
    session.execute(
        delete(BuildStepPart).where(
            BuildStepPart.step_id.in_(select(BuildStep.id).where(BuildStep.build_id == build.id))
        )
    )
    session.flush()

    huerfanas = 0
    for c in datos["colocaciones"]:
        paso_id = c["paso_id"] if c["paso_id"] in pasos else refugio
        if paso_id is None:
            huerfanas += 1
            continue
        if paso_id != c["paso_id"]:
            huerfanas += 1
        placement = Placement(
            build_id=build.id,
            step_id=paso_id,
            element_id=c["element_id"],
            x=c["x"],
            y=c["y"],
            z=c["z"],
            rotation=c["rotacion"],
        )
        # Se conserva el id de la colocación siempre que siga libre: es la
        # referencia que el chat y la selección de la web tienen en la mano.
        if session.get(Placement, c["id"]) is None:
            placement.id = c["id"]
        session.add(placement)

    for r in datos["reservas"]:
        if r["paso_id"] in pasos:
            session.add(
                BuildStepPart(
                    step_id=r["paso_id"], element_id=r["element_id"], quantity=r["cantidad"]
                )
            )

    build.baseplate_w = datos["placa"]["ancho"]
    build.baseplate_d = datos["placa"]["fondo"]
    if datos.get("estado"):
        build.status = datos["estado"]
    session.flush()
    return {"piezas": len(datos["colocaciones"]), "reubicadas": huerfanas}


# --------------------------------------------------------------------------
# Las dos pilas
# --------------------------------------------------------------------------
def _apilar(session: Session, build_id: int, pila: str, descripcion: str, datos: dict) -> None:
    siguiente = (
        session.scalar(
            select(func.max(DesignSnapshot.seq)).where(
                DesignSnapshot.build_id == build_id, DesignSnapshot.pila == pila
            )
        )
        or 0
    ) + 1
    session.add(
        DesignSnapshot(
            build_id=build_id,
            pila=pila,
            seq=siguiente,
            descripcion=descripcion[:160],
            datos=json.dumps(datos, separators=(",", ":")),
        )
    )
    session.flush()
    _recortar(session, build_id, pila)


def _recortar(session: Session, build_id: int, pila: str) -> None:
    sobran = list(
        session.scalars(
            select(DesignSnapshot)
            .where(DesignSnapshot.build_id == build_id, DesignSnapshot.pila == pila)
            .order_by(DesignSnapshot.seq.desc())
            .offset(MAX_PILA)
        )
    )
    for foto in sobran:
        session.delete(foto)
    if sobran:
        session.flush()


def _desapilar(session: Session, build_id: int, pila: str) -> DesignSnapshot | None:
    return session.scalar(
        select(DesignSnapshot)
        .where(DesignSnapshot.build_id == build_id, DesignSnapshot.pila == pila)
        .order_by(DesignSnapshot.seq.desc())
        .limit(1)
    )


def _vaciar(session: Session, build_id: int, pila: str) -> None:
    session.execute(
        delete(DesignSnapshot).where(
            DesignSnapshot.build_id == build_id, DesignSnapshot.pila == pila
        )
    )
    session.flush()


# --------------------------------------------------------------------------
# Lo que usa el resto de la aplicación
# --------------------------------------------------------------------------
def registrar(session: Session, build_id: int, descripcion: str) -> None:
    """Guarda el estado previo a una operación. Llámalo justo antes de tocar nada.

    Rehacer deja de tener sentido en cuanto se hace algo nuevo, así que esa
    pila se vacía: es lo que hace cualquier editor.
    """
    _apilar(session, int(build_id), "deshacer", descripcion, capturar(session, build_id))
    _vaciar(session, int(build_id), "rehacer")


def deshacer(session: Session, build_id: int) -> dict[str, Any]:
    """Vuelve al estado anterior a la última operación."""
    build_id = int(build_id)
    foto = _desapilar(session, build_id, "deshacer")
    if foto is None:
        return {"deshecho": False, "motivo": "No hay nada que deshacer en este montaje."}

    _apilar(session, build_id, "rehacer", foto.descripcion, capturar(session, build_id))
    resultado = _restaurar(session, build_id, json.loads(foto.datos))
    descripcion = foto.descripcion
    session.delete(foto)
    session.flush()
    return {"deshecho": True, "operacion": descripcion, **resultado, **estado(session, build_id)}


def rehacer(session: Session, build_id: int) -> dict[str, Any]:
    """Repite lo último que se deshizo."""
    build_id = int(build_id)
    foto = _desapilar(session, build_id, "rehacer")
    if foto is None:
        return {"rehecho": False, "motivo": "No hay nada que rehacer en este montaje."}

    # El estado actual pasa a poder deshacerse otra vez, sin vaciar «rehacer»:
    # por eso no se usa registrar().
    _apilar(session, build_id, "deshacer", foto.descripcion, capturar(session, build_id))
    resultado = _restaurar(session, build_id, json.loads(foto.datos))
    descripcion = foto.descripcion
    session.delete(foto)
    session.flush()
    return {"rehecho": True, "operacion": descripcion, **resultado, **estado(session, build_id)}


def estado(session: Session, build_id: int) -> dict[str, Any]:
    """Cuántos pasos atrás y adelante hay, y cómo se llaman."""
    build_id = int(build_id)
    ultima = _desapilar(session, build_id, "deshacer")
    siguiente = _desapilar(session, build_id, "rehacer")
    cuenta = dict(
        session.execute(
            select(DesignSnapshot.pila, func.count(DesignSnapshot.id))
            .where(DesignSnapshot.build_id == build_id)
            .group_by(DesignSnapshot.pila)
        ).all()
    )
    return {
        "puede_deshacer": ultima is not None,
        "puede_rehacer": siguiente is not None,
        "deshacer": int(cuenta.get("deshacer", 0)),
        "rehacer": int(cuenta.get("rehacer", 0)),
        "ultima_operacion": ultima.descripcion if ultima else None,
        "siguiente_operacion": siguiente.descripcion if siguiente else None,
    }


def lista(session: Session, build_id: int, limite: int = 20) -> dict[str, Any]:
    """Las últimas operaciones deshacibles, de la más reciente a la más antigua."""
    fotos = list(
        session.scalars(
            select(DesignSnapshot)
            .where(DesignSnapshot.build_id == int(build_id), DesignSnapshot.pila == "deshacer")
            .order_by(DesignSnapshot.seq.desc())
            .limit(limite)
        )
    )
    return {
        "montaje_id": int(build_id),
        "operaciones": [
            {
                "descripcion": f.descripcion,
                "cuando": f.created_at.isoformat(timespec="seconds") if f.created_at else None,
            }
            for f in fotos
        ],
        **estado(session, build_id),
    }
