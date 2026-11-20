"""A small 2D-projected 3D view of a body box and its axes for one rotation.

Drawn with QPainter (no OpenGL). The fixed camera looks at the origin from
azimuth 35 deg and elevation 25 deg above the reference x-y plane, so with the
identity rotation the x axis points down-left, y down-right and z up.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.frames import Rotation

AZIMUTH_DEG = 35.0
ELEVATION_DEG = 25.0
# Half extents of the drawn body box along its own x, y and z axes.
BOX = (0.6, 0.35, 0.15)
AXIS_LENGTH = 0.95
AXIS_COLORS = ("#d62728", "#2ca02c", "#1f77b4")


def _camera() -> tuple[np.ndarray, np.ndarray]:
    azimuth, elevation = math.radians(AZIMUTH_DEG), math.radians(ELEVATION_DEG)
    toward_camera = np.array(
        [
            math.cos(elevation) * math.cos(azimuth),
            math.cos(elevation) * math.sin(azimuth),
            math.sin(elevation),
        ]
    )
    forward = -toward_camera
    right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    return right, up


_RIGHT, _UP = _camera()


def project(point: np.ndarray) -> tuple[float, float]:
    """Return the camera-plane coordinates (right, up) of a 3D point."""

    return float(point @ _RIGHT), float(point @ _UP)


class AttitudeView(QWidget):
    """Draw reference axes always, and the body only for a pose worth showing.

    ``pose_style`` is ``"valid"`` (solid body), ``"uncalibrated"`` (dashed grey body)
    or ``"none"`` (no body: missing, stale, invalid or no data). The caption is
    drawn over the view so a stale or missing state can never look like a pose.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rotation: Rotation | None = None
        self.pose_style = "none"
        self.caption = ""
        self.setMinimumSize(220, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Attitude view")

    def show_attitude(self, rotation: Rotation | None, style: str, caption: str) -> None:
        if style not in {"valid", "uncalibrated", "none"}:
            raise ValueError(f"unknown attitude style {style!r}")
        self.rotation = rotation if style != "none" else None
        self.pose_style = style if self.rotation is not None else "none"
        self.caption = caption
        self.setAccessibleDescription(caption)
        self.update()

    def body_axes(self) -> tuple[tuple[float, float], ...] | None:
        """Return the projected (right, up) tips of the body x, y and z axes, or None."""

        if self.rotation is None:
            return None
        matrix = self.rotation.matrix
        return tuple(project(matrix[:, index] * AXIS_LENGTH) for index in range(3))

    @staticmethod
    def reference_axes() -> tuple[tuple[float, float], ...]:
        return tuple(project(np.eye(3)[index] * AXIS_LENGTH) for index in range(3))

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.base())
        text = palette.text().color()
        center = QPointF(self.width() / 2, self.height() / 2 + 8)
        scale = 0.42 * min(self.width(), self.height())

        def screen(point: tuple[float, float]) -> QPointF:
            return QPointF(center.x() + scale * point[0], center.y() - scale * point[1])

        muted = QColor(text)
        muted.setAlpha(110)
        pen = QPen(muted, 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        for name, tip in zip("XYZ", self.reference_axes(), strict=True):
            end = screen(tip)
            painter.drawLine(center, end)
            painter.drawText(end + QPointF(3, -3), f"{name} ref")

        if self.rotation is not None:
            matrix = self.rotation.matrix
            corners = {
                (sx, sy, sz): screen(
                    project(matrix @ np.array([sx * BOX[0], sy * BOX[1], sz * BOX[2]]))
                )
                for sx in (-1, 1)
                for sy in (-1, 1)
                for sz in (-1, 1)
            }
            valid = self.pose_style == "valid"
            body_pen = QPen(text if valid else muted, 2 if valid else 1)
            if not valid:
                body_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(body_pen)
            for corner, point in corners.items():
                for axis in range(3):
                    if corner[axis] < 0:
                        other = list(corner)
                        other[axis] = 1
                        painter.drawLine(point, corners[(other[0], other[1], other[2])])
            body = self.body_axes()
            assert body is not None
            for name, tip, color in zip("xyz", body, AXIS_COLORS, strict=True):
                axis_pen = QPen(QColor(color) if valid else muted, 3 if valid else 1)
                if not valid:
                    axis_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(axis_pen)
                end = screen(tip)
                painter.drawLine(center, end)
                painter.drawText(end + QPointF(4, 4), name)

        if self.caption:
            painter.setPen(text)
            font = painter.font()
            font.setBold(self.pose_style != "valid")
            painter.setFont(font)
            painter.drawText(
                QRectF(6, 4, self.width() - 12, 40),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                self.caption,
            )
        painter.end()
