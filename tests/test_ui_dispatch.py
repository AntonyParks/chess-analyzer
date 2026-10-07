import threading

from chess_analyzer.ui_dispatch import QueueDispatcher


def test_posts_run_in_order_on_drain():
    d = QueueDispatcher()
    seen = []
    assert d.post(seen.append, 1)
    assert d.post(seen.append, 2)
    assert d.drain() == 2
    assert seen == [1, 2]


def test_post_from_another_thread_and_kwargs():
    d = QueueDispatcher()
    seen = []
    t = threading.Thread(target=lambda: d.post(lambda a, b=None: seen.append((a, b)), 1, b=2))
    t.start()
    t.join()
    d.drain()
    assert seen == [(1, 2)]


def test_exception_in_callback_does_not_stop_the_drain():
    d = QueueDispatcher()
    seen = []

    def boom():
        raise RuntimeError("nope")

    d.post(boom)
    d.post(seen.append, "after")
    assert d.drain() == 2
    assert seen == ["after"]


def test_close_drops_queued_work_and_ignores_later_posts():
    d = QueueDispatcher()
    d.post(lambda: None)
    d.close()
    assert d.closed
    assert d.post(lambda: None) is False
    assert d.drain() == 0
