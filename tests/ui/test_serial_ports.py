"""The QtSerialPort implementation reads a real tty read-only (POSIX pseudo-terminal)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from runpy import run_path

import pytest
from PySide6.QtSerialPort import QSerialPort

from lbr_dashboard.core import SourceState
from lbr_dashboard.importers.serial_source import SerialLiveSource, SerialPortError
from lbr_dashboard.serial_ports import QtSerialPort, list_serial_ports

TESTS = Path(__file__).parents[1]
FAKE = run_path(str(TESTS / "source_contract" / "fake_serial.py"))
CHECKS = run_path(str(TESTS / "source_contract" / "checks.py"))
GOLDEN_STREAM: bytes = FAKE["GOLDEN_STREAM"]
until = CHECKS["until"]

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "openpty"), reason="needs a POSIX pseudo-terminal"
)


class RecordingPort(QtSerialPort):
    """Remembers the mode the port was opened with (checked on the worker thread)."""

    modes: list[QSerialPort.OpenModeFlag] = []

    def open(self) -> None:
        super().open()
        assert self._port is not None
        RecordingPort.modes.append(self._port.openMode())


@posix_only
def test_qt_port_reads_a_tty_read_only_and_releases_it(qapp) -> None:
    master, slave = os.openpty()
    name = os.ttyname(slave)
    ports: list[RecordingPort] = []

    def opener(port_name: str) -> RecordingPort:
        ports.append(RecordingPort(port_name))
        return ports[-1]

    source = SerialLiveSource(name, opener, read_timeout_s=0.02)
    try:
        source.open()
        source.start()
        until(lambda: source.link_status().connected, "the tty was not opened")
        os.write(master, GOLDEN_STREAM)
        until(lambda: source.link_status().packets == 2, "the tty stream was not decoded")
        assert source.link_status().rejected_frames == 1
        assert RecordingPort.modes[-1] == QSerialPort.OpenModeFlag.ReadOnly
        os.set_blocking(master, False)
        with pytest.raises(BlockingIOError):
            os.read(master, 64)  # nothing was written towards the device
        assert source.close(2.0)
        assert source.state is SourceState.CLOSED
        assert not ports[0].is_open
    finally:
        source.close(2.0)
        os.close(master)
        os.close(slave)


def test_missing_port_is_a_readable_error(qapp) -> None:
    port = QtSerialPort("lbr-no-such-port-42")
    with pytest.raises(SerialPortError) as error:
        port.open()
    assert str(error.value)
    port.close()
    port.close()  # closing twice is harmless


def test_port_list_has_names_and_labels(qapp) -> None:
    for port in list_serial_ports():
        assert port.name and port.label.startswith(port.name)
        assert port.usb or port.description
