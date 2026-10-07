import threading

from chess_analyzer.ui_dispatch import UiDispatcher


class FakeRoot:
    """Records after()/after_cancel() calls; the test drives the pump by hand."""

    def __init__(self):
        self.scheduled = []
        self.cancelled = []
        self._next = 0

    def after(self, ms, fn):
        self._next += 1
        ident = f"after#{self._next}"
        self.scheduled.append((ident, ms, fn))
        return ident

    def after_cancel(self, ident):
        self.cancelled.append(ident)

    def fire(self):
        ident, _ms, fn = self.scheduled.pop(0)
        fn()
        return ident


def test_posts_run_in_order_on_the_pump():
    root = FakeRoot()
    d = UiDispatcher(root, interval_ms=5)
    d.start()
    seen = []
    assert d.post(seen.append, 1)
    assert d.post(seen.append, 2)
    root.fire()
    assert seen == [1, 2]
    assert len(root.scheduled) == 1  # pump rescheduled itself


def test_post_from_another_thread_and_kwargs():
    root = FakeRoot()
    d = UiDispatcher(root)
    d.start()
    seen = []
    t = threading.Thread(target=lambda: d.post(lambda a, b=None: seen.append((a, b)), 1, b=2))
    t.start()
    t.join()
    root.fire()
    assert seen == [(1, 2)]


def test_exception_in_callback_does_not_stop_the_pump():
    root = FakeRoot()
    d = UiDispatcher(root)
    d.start()
    seen = []

    def boom():
        raise RuntimeError("nope")

    d.post(boom)
    d.post(seen.append, "after")
    root.fire()
    assert seen == ["after"]
    assert len(root.scheduled) == 1


def test_close_cancels_pump_and_ignores_later_posts():
    root = FakeRoot()
    d = UiDispatcher(root)
    d.start()
    pending = root.scheduled[0][0]
    d.post(lambda: None)
    d.close()
    assert d.closed
    assert root.cancelled == [pending]
    assert d.post(lambda: None) is False
    assert d.drain() == 0


def test_callback_that_closes_the_dispatcher_stops_the_tick():
    root = FakeRoot()
    d = UiDispatcher(root)
    d.start()
    seen = []
    d.post(d.close)
    d.post(seen.append, "never")
    root.fire()
    assert seen == []
    assert root.scheduled == []  # not rescheduled after close


def test_drain_runs_everything_synchronously():
    root = FakeRoot()
    d = UiDispatcher(root)
    seen = []
    d.post(seen.append, "a")
    d.post(seen.append, "b")
    assert d.drain() == 2
    assert seen == ["a", "b"]


def test_max_per_tick_leaves_the_rest_for_the_next_tick():
    root = FakeRoot()
    d = UiDispatcher(root, max_per_tick=2)
    d.start()
    seen = []
    for i in range(5):
        d.post(seen.append, i)
    root.fire()
    assert seen == [0, 1]
    root.fire()
    root.fire()
    assert seen == [0, 1, 2, 3, 4]
