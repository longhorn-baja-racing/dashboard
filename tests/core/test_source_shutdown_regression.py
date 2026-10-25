"""The issue #20 full-control-queue timeout regression."""

from threading import Event, Thread

from lbr_dashboard.core.contracts import SourceEvent, SourceEventKind, SourceEventQueue


def test_full_control_queue_honors_publish_timeout() -> None:
    queue = SourceEventQueue(1)
    end = SourceEvent(SourceEventKind.END_OF_STREAM, "reference", reason="complete")
    assert queue.publish(end)
    finished = Event()
    result = []

    def publish():
        result.append(queue.publish(end, timeout=0.02))
        finished.set()

    worker = Thread(target=publish, daemon=True)
    worker.start()
    try:
        assert finished.wait(0.5), "Control-event publication ignored its timeout"
        assert result == [False]
    finally:
        queue.drain()
        worker.join(1)
        assert not worker.is_alive()
