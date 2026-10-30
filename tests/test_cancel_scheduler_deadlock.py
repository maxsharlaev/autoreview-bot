"""AR-030-005: no deadlock when add_done_callback runs with an already-done future."""

from __future__ import annotations

import threading
from concurrent.futures import Future

import pytest
from app.launcher import cancel_scheduler as sched


def test_submit_cancel_no_deadlock_when_future_already_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """If submit returns an already-completed Future, callback must not re-enter the lock."""
    jid = "a" * 32
    done: Future = Future()
    done.set_result({"job_id": jid, "removed": 0, "confirmed": True})

    monkeypatch.setattr(sched._CANCEL_POOL, "submit", lambda _fn: done)

    holder: list[Future | BaseException] = []
    finished = threading.Event()

    def _call() -> None:
        try:
            holder.append(sched._submit_cancel(jid, client=None))
        except BaseException as exc:  # noqa: BLE001 - surface any failure
            holder.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=_call, name="opr-ar030-005")
    thread.start()
    # Deadlock would hang forever; fail fast if the lock is stuck.
    assert finished.wait(timeout=2.0), "AR-030-005 deadlock: _submit_cancel did not finish within 2s"
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert holder and holder[0] is done
    # Callback cleared the map (identity check).
    with sched._INFLIGHT_LOCK:
        assert jid not in sched._INFLIGHT


def test_submit_cancel_no_deadlock_when_future_completes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Future that completes before add_done_callback is registered (same sync-callback path)."""
    jid = "b" * 32
    calls = {"n": 0}

    def instant(_fn):  # noqa: ANN001
        calls["n"] += 1
        fut: Future = Future()
        fut.set_result({"job_id": jid, "removed": 0, "confirmed": True})
        return fut

    monkeypatch.setattr(sched._CANCEL_POOL, "submit", instant)

    finished = threading.Event()
    err: list[BaseException] = []

    def _call() -> None:
        try:
            fut = sched._submit_cancel(jid, client=None)
            assert fut.done()
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=_call)
    thread.start()
    assert finished.wait(timeout=2.0), "AR-030-005 deadlock on immediately completing future"
    thread.join(timeout=1.0)
    assert not err
    assert calls["n"] == 1
    with sched._INFLIGHT_LOCK:
        assert jid not in sched._INFLIGHT
