import sagasmith_core.managed_artifacts as managed_artifacts
from sagasmith_core.managed_artifacts import read_content_archive, write_content_archive


def test_managed_content_archive_round_trip(tmp_path, monkeypatch) -> None:
    package = {
        "checksum": "a" * 64,
        "kind": "addon",
        "id": "module.test",
        "version": "1",
    }
    blobs = {"blob": b"payload"}
    monkeypatch.setattr(managed_artifacts, "dumps_content_archive", lambda *_: b"archive")
    monkeypatch.setattr(
        managed_artifacts,
        "loads_content_archive",
        lambda content: (package, blobs) if content == b"archive" else None,
    )
    stored = write_content_archive(tmp_path, package, blobs)
    loaded, loaded_blobs = read_content_archive(tmp_path, artifact=stored["artifact"])
    assert loaded == package
    assert loaded_blobs == blobs
