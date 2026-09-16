"""The dispatcher's zombie-reap log must say WHY each worker died.

The 2026-09-12 debug report showed ``reaped 1 zombie worker(s), pids=[258668]``
— accurate but not actionable. The reap log now classifies each exit through
the same registry the reaper populates (``_record_worker_exit`` →
``_classify_worker_exit``), so a protocol-violation exit, a provider quota
wall, and an OOM kill are distinguishable in the log line.
"""

from gateway.kanban_watchers import _describe_reaped_workers


def _exited_status(code):
    """Encode an exit status the way os.waitpid reports WIFEXITED statuses."""
    return code << 8


def _signaled_status(sig):
    """Encode a signal status the way os.waitpid reports WIFSIGNALED statuses."""
    return sig


def test_describe_reaped_workers_classifies_exits_via_registry():
    from hermes_cli import kanban_db

    # The reaper records these right before returning the pids; the classifier
    # reads them in the same tick. Exit code 0 on a zombie = protocol
    # violation (worker died without kanban_complete/kanban_block).
    kanban_db._record_worker_exit(258668, _exited_status(0))
    kanban_db._record_worker_exit(258669, _exited_status(kanban_db.KANBAN_RATE_LIMIT_EXIT_CODE))
    kanban_db._record_worker_exit(258670, _signaled_status(9))

    described = _describe_reaped_workers(
        [258668, 258669, 258670], kanban_db._classify_worker_exit
    )

    assert "258668:clean_exit" in described
    assert "258669:rate_limited" in described
    assert "258670:signaled (9)" in described


def test_describe_reaped_workers_degrades_to_unknown_for_unregistered_pid():
    from hermes_cli import kanban_db

    described = _describe_reaped_workers([999999], kanban_db._classify_worker_exit)
    assert described == "999999:unknown"


def test_describe_reaped_workers_survives_classifier_crash():
    def _broken(_pid):
        raise RuntimeError("registry corrupted")

    assert _describe_reaped_workers([1, 2], _broken) == "1:unknown, 2:unknown"
