"""run_fetch helpers (pipeline KB artifacts)."""

from runtime.tools.run_fetch import run_root_from_scratch_scope


def test_run_root_from_scratch_scope():
    assert run_root_from_scratch_scope("/runs/abc-123/scratch") == "/runs/abc-123"
    assert run_root_from_scratch_scope("/runs/abc-123/scratch/") == "/runs/abc-123"
    assert run_root_from_scratch_scope(None) is None
    assert run_root_from_scratch_scope("/tasks/foo") is None
