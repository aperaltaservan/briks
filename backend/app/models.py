"""Modelo de datos.

Vocabulario LEGO usado en toda la app:
  - DesignID  -> el molde de la pieza (ej. 3021 = PLATE 2X3), sin color.
  - ElementID -> molde + color, el identificador que LEGO imprime en las bolsas
                 (ej. 302126 = PLATE 2X3 en Black). Es la unidad de inventario.
  - Colour    -> nomenclatura oficial LEGO ("Brick Yellow", no "beige").
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Catálogo
# --------------------------------------------------------------------------
class Color(Base):
    __tablename__ = "colors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    hex_code: Mapped[str | None] = mapped_column(String(7))
    is_transparent: Mapped[bool] = mapped_column(default=False)

    elements: Mapped[list["Element"]] = relationship(back_populates="color")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Color {self.name}>"


class Part(Base):
    """El molde, independiente del color."""

    __tablename__ = "parts"

    design_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    category: Mapped[str | None] = mapped_column(String(80), index=True)
    # Texto normalizado para búsquedas (minúsculas, sin acentos).
    search_text: Mapped[str] = mapped_column(Text, default="", index=True)

    elements: Mapped[list["Element"]] = relationship(back_populates="part")


class Element(Base):
    """Molde + color. Unidad real de inventario."""

    __tablename__ = "elements"

    element_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    design_id: Mapped[str] = mapped_column(ForeignKey("parts.design_id"), index=True)
    color_id: Mapped[int] = mapped_column(ForeignKey("colors.id"), index=True)
    image_url: Mapped[str | None] = mapped_column(String(400))

    part: Mapped[Part] = relationship(back_populates="elements")
    color: Mapped[Color] = relationship(back_populates="elements")

    __table_args__ = (Index("ix_elements_design_color", "design_id", "color_id"),)

    @property
    def label(self) -> str:
        return f"{self.part.name} · {self.color.name}"


# --------------------------------------------------------------------------
# Sets
# --------------------------------------------------------------------------
class LegoSet(Base):
    __tablename__ = "sets"

    set_number: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(String(400))
    owned: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    parts: Mapped[list["SetPart"]] = relationship(
        back_populates="lego_set", cascade="all, delete-orphan"
    )


class SetPart(Base):
    __tablename__ = "set_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    set_number: Mapped[str] = mapped_column(
        ForeignKey("sets.set_number", ondelete="CASCADE"), index=True
    )
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.element_id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    is_spare: Mapped[bool] = mapped_column(default=False)

    lego_set: Mapped[LegoSet] = relationship(back_populates="parts")
    element: Mapped[Element] = relationship()

    __table_args__ = (UniqueConstraint("set_number", "element_id", "is_spare"),)


# --------------------------------------------------------------------------
# Inventario
# --------------------------------------------------------------------------
class InventoryItem(Base):
    """Cuántas unidades tengo de un elemento, opcionalmente por ubicación."""

    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.element_id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    location: Mapped[str] = mapped_column(String(80), default="", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    element: Mapped[Element] = relationship()

    __table_args__ = (
        UniqueConstraint("element_id", "location", name="uq_inventory_element_location"),
        CheckConstraint("quantity >= 0", name="ck_inventory_qty_positive"),
    )


class InventoryMovement(Base):
    """Historial de cada cambio: de dónde salió cada pieza."""

    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.element_id"), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(40))  # set | imagen | manual | ajuste | montaje
    reference: Mapped[str | None] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# --------------------------------------------------------------------------
# Montajes
# --------------------------------------------------------------------------
# Un montaje activo mantiene sus piezas reservadas: si el modelo está armado,
# esas piezas no están disponibles para otro montaje. "desmontado" las libera.
BUILD_ACTIVE_STATES = ("planificado", "en_progreso", "completado")
BUILD_STATES = BUILD_ACTIVE_STATES + ("desmontado",)


class Build(Base):
    """Un montaje: secuencia de pasos que consume piezas del inventario."""

    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="planificado", index=True)
    # Tamaño de la placa base sobre la que se diseña, en studs.
    baseplate_w: Mapped[int] = mapped_column(Integer, default=32)
    baseplate_d: Mapped[int] = mapped_column(Integer, default=32)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    steps: Mapped[list["BuildStep"]] = relationship(
        back_populates="build",
        cascade="all, delete-orphan",
        order_by="BuildStep.position",
    )
    placements: Mapped[list["Placement"]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )


class BuildStep(Base):
    __tablename__ = "build_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    instruction: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pendiente")  # pendiente | hecho

    build: Mapped[Build] = relationship(back_populates="steps")
    parts: Mapped[list["BuildStepPart"]] = relationship(
        back_populates="step", cascade="all, delete-orphan"
    )
    placements: Mapped[list["Placement"]] = relationship(
        back_populates="step", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("build_id", "position", name="uq_step_position"),)


class BuildStepPart(Base):
    __tablename__ = "build_step_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("build_steps.id", ondelete="CASCADE"), index=True
    )
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.element_id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    step: Mapped[BuildStep] = relationship(back_populates="parts")
    element: Mapped[Element] = relationship()


class Placement(Base):
    """Una pieza puesta en un sitio concreto del modelo 3D.

    Coordenadas en unidades LEGO y enteras, que es lo que impone la rejilla:
      - x, z -> esquina de menor coordenada del hueco que ocupa, en studs.
      - y    -> altura desde la placa base, en placas (un ladrillo son 3).
      - rotation -> 0, 90, 180 o 270 grados sobre el eje vertical.

    Cada colocación pertenece a un paso: eso es lo que permite reproducir el
    montaje como un manual de instrucciones, paso a paso.
    """

    __tablename__ = "build_placements"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("build_steps.id", ondelete="CASCADE"), index=True
    )
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.element_id"), index=True)
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    z: Mapped[int] = mapped_column(Integer, default=0)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    build: Mapped[Build] = relationship(back_populates="placements")
    step: Mapped[BuildStep] = relationship(back_populates="placements")
    element: Mapped[Element] = relationship()

    __table_args__ = (
        Index("ix_placements_build_step", "build_id", "step_id"),
        CheckConstraint("rotation in (0, 90, 180, 270)", name="ck_placement_rotation"),
    )


class DesignSnapshot(Base):
    """Una foto del modelo, para poder deshacer.

    Guardar el estado anterior entero es más simple que anotar el inverso de
    cada operación, y vale igual para lo que se hace desde la web que para lo
    que hace el chat: los dos pasan por los mismos servicios. Cada montaje tiene
    dos pilas, la de deshacer y la de rehacer, ordenadas por `seq`.
    """

    __tablename__ = "design_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), index=True)
    pila: Mapped[str] = mapped_column(String(10), index=True)  # deshacer | rehacer
    seq: Mapped[int] = mapped_column(Integer)
    descripcion: Mapped[str] = mapped_column(String(160), default="")
    datos: Mapped[str] = mapped_column(Text)  # JSON con placa, colocaciones y reservas
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_snapshots_build_pila", "build_id", "pila", "seq"),)


# --------------------------------------------------------------------------
# OAuth del servidor MCP
# --------------------------------------------------------------------------
# Clientes como ChatGPT no admiten un token fijo: se registran solos y piden
# un token siguiendo el flujo de autorización de MCP. Aquí sólo se guarda el
# estado; el protocolo lo pone el SDK (ver services -> app/oauth.py).
class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # OAuthClientInformationFull serializado: el SDK define los campos y no
    # tiene sentido duplicar su esquema aquí.
    data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthCode(Base):
    """Código de autorización: de un solo uso y con minutos de vida."""

    __tablename__ = "oauth_codes"

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float)


class OAuthTokenRow(Base):
    __tablename__ = "oauth_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # access | refresh
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    resource: Mapped[str | None] = mapped_column(String(400))
    expires_at: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------
# Reconocimiento (foto o descripción libre desde el chat)
# --------------------------------------------------------------------------
class RecognitionSession(Base):
    """Un lote de piezas detectadas, pendiente de confirmar antes de tocar el inventario."""

    __tablename__ = "recognition_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), default="imagen")  # imagen | texto
    image_ref: Mapped[str | None] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="pendiente")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list["RecognitionItem"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class RecognitionItem(Base):
    __tablename__ = "recognition_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("recognition_sessions.id", ondelete="CASCADE"), index=True
    )
    raw_description: Mapped[str] = mapped_column(String(300))
    raw_color: Mapped[str | None] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    element_id: Mapped[str | None] = mapped_column(ForeignKey("elements.element_id"))
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="pendiente")
    candidates: Mapped[str | None] = mapped_column(Text)  # JSON con las opciones propuestas

    session: Mapped[RecognitionSession] = relationship(back_populates="items")
    element: Mapped[Element | None] = relationship()
