"""Serial port access through QtSerialPort (part of PySide6), read-only.

The serial source (``importers/serial_source.py``) only knows the small
``SerialPort`` interface, so tests feed it in-memory byte streams. This module
is the real implementation the app composes in:

- :func:`list_serial_ports` lists the ports the operating system reports
  (USB ports first); legacy built-in ports without a description are left out;
- :class:`QtSerialPort` opens a port **read-only** at 921600 baud, 8N1, no flow
  control, and clears DTR and RTS so the ESP32's auto-reset circuit is not
  triggered. It has no write method: the dashboard never sends a byte.

A ``QtSerialPort`` is created, used and destroyed on the source worker thread.
It creates a ``QEventLoop`` there first, which gives the thread the event
dispatcher QSerialPort's notifiers need; ``waitForReadyRead`` then waits
without running the loop. If no byte arrives for a second, it checks that a
port listed when it was opened is still listed, so an unplugged adapter is
reported even on platforms that do not signal an error on the open handle.
"""

from __future__ import annotations

from time import monotonic

from PySide6.QtCore import QEventLoop
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from .importers.serial_source import (
    DEFAULT_BAUD,
    SerialDisconnected,
    SerialPortError,
    SerialPortInfo,
)

_FATAL = {
    QSerialPort.SerialPortError.ResourceError,
    QSerialPort.SerialPortError.PermissionError,
    QSerialPort.SerialPortError.DeviceNotFoundError,
}


def list_serial_ports() -> list[SerialPortInfo]:
    """Ports to offer in the chooser: USB ports first, then other described ports."""

    ports = []
    for info in QSerialPortInfo.availablePorts():
        usb = info.hasVendorIdentifier()
        description = info.description().strip()
        if not usb and not description:
            continue  # e.g. /dev/ttyS0..31 on Linux: built-in, almost never connected.
        ports.append(
            SerialPortInfo(
                info.portName(),
                description,
                info.manufacturer().strip(),
                usb,
            )
        )
    ports.sort(key=lambda port: (not port.usb, port.name))
    return ports


class QtSerialPort:
    """One serial port opened read-only; see the module docstring."""

    def __init__(self, name: str, baud: int = DEFAULT_BAUD) -> None:
        self._name = name
        self._baud = baud
        self._port: QSerialPort | None = None
        self._loop: QEventLoop | None = None
        self._listed = False
        self._quiet_since = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_open(self) -> bool:
        return self._port is not None and self._port.isOpen()

    def open(self) -> None:
        self._loop = QEventLoop()
        self._listed = _listed(self._name)
        port = QSerialPort()
        port.setPortName(self._name)
        port.setBaudRate(self._baud)
        port.setDataBits(QSerialPort.DataBits.Data8)
        port.setParity(QSerialPort.Parity.NoParity)
        port.setStopBits(QSerialPort.StopBits.OneStop)
        port.setFlowControl(QSerialPort.FlowControl.NoFlowControl)
        if not port.open(QSerialPort.OpenModeFlag.ReadOnly):
            message = port.errorString() or "unknown error"
            error = port.error()
            if error == QSerialPort.SerialPortError.PermissionError:
                raise SerialPortError(
                    f"access denied ({message}); close any other program using the port"
                )
            if error == QSerialPort.SerialPortError.DeviceNotFoundError:
                raise SerialPortError(f"the port does not exist ({message}); click Refresh ports")
            raise SerialPortError(message)
        # Modem control lines, not data: deasserted, the ESP32 keeps running.
        port.setDataTerminalReady(False)
        port.setRequestToSend(False)
        port.clearError()
        self._port = port
        self._quiet_since = monotonic()

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        port = self._port
        if port is None:
            raise SerialPortError("the port is not open")
        if port.bytesAvailable() <= 0:
            port.waitForReadyRead(max(1, int(timeout_s * 1000)))
        data = bytes(port.read(max_bytes).data())
        error = port.error()
        if error in _FATAL or not port.isOpen():
            raise SerialDisconnected(port.errorString() or "the port closed")
        if error != QSerialPort.SerialPortError.NoError:
            port.clearError()  # A timeout, or a framing/parity glitch the CRC will catch.
        now = monotonic()
        if data:
            self._quiet_since = now
        elif self._listed and now - self._quiet_since >= 1.0:
            self._quiet_since = now
            if not _listed(self._name):
                raise SerialDisconnected("the port is no longer present")
        return data

    def close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            if port.isOpen():
                port.close()
            # The wrapper owns the C++ object; dropping it here deletes it on
            # this (worker) thread, whose event loop never runs deleteLater.
        self._loop = None


def _listed(name: str) -> bool:
    return any(info.portName() == name for info in QSerialPortInfo.availablePorts())


def open_serial_port(name: str) -> QtSerialPort:
    return QtSerialPort(name)
