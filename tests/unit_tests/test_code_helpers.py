from climateclaw.tools.code.helpers import (
    detect_created_or_modified_files,
    snapshot_files,
)
from climateclaw.tools.models import CreatedFile


def test_snapshot_files_ignores_runtime_artifacts(tmp_path):
    (tmp_path / "kept.txt").write_text("keep")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "ignored.pyc").write_bytes(b"compiled")
    (tmp_path / ".ipynb_checkpoints").mkdir()
    (tmp_path / ".ipynb_checkpoints" / "ignored.txt").write_text("checkpoint")
    (tmp_path / "scratch.tmp").write_text("partial")

    snapshot = snapshot_files(tmp_path)

    assert list(snapshot) == ["kept.txt"]


def test_detect_created_or_modified_files_reports_new_files(tmp_path):
    before = snapshot_files(tmp_path)
    (tmp_path / "plot.png").write_bytes(b"png data")
    after = snapshot_files(tmp_path)

    assert detect_created_or_modified_files(tmp_path, before, after) == [
        CreatedFile(path="plot.png", mime_type="image/png")
    ]


def test_detect_created_or_modified_files_reports_modified_files(tmp_path):
    result_file = tmp_path / "result.csv"
    result_file.write_text("a,b\n1,2\n")
    before = snapshot_files(tmp_path)

    result_file.write_text("a,b\n3,4\n")
    after = snapshot_files(tmp_path)

    assert detect_created_or_modified_files(tmp_path, before, after) == [
        CreatedFile(path="result.csv", mime_type="text/csv")
    ]


def test_detect_created_or_modified_files_ignores_unchanged_files(tmp_path):
    unchanged = tmp_path / "unchanged.txt"
    unchanged.write_text("same")
    before = snapshot_files(tmp_path)
    after = snapshot_files(tmp_path)

    assert detect_created_or_modified_files(tmp_path, before, after) == []


def test_detect_created_or_modified_files_returns_relative_path_for_nested_files(
    tmp_path,
):
    nested = tmp_path / "plots"
    nested.mkdir()
    before = snapshot_files(tmp_path)

    (nested / "figure.png").write_bytes(b"png data")
    after = snapshot_files(tmp_path)

    assert detect_created_or_modified_files(tmp_path, before, after) == [
        CreatedFile(path="plots/figure.png", mime_type="image/png")
    ]
