"""The code-deployed Linux app prefers the FFmpeg executables in its artifact."""

from pathlib import Path

from atlas_backend import media_tools


def test_linux_uses_bundled_tools_even_when_they_arrive_without_execute_bits(
    tmp_path, monkeypatch,
):
    binary = tmp_path / "ffprobe"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(media_tools, "_BUNDLED_BIN", tmp_path)
    monkeypatch.setattr(media_tools.sys, "platform", "linux")
    monkeypatch.setattr(media_tools.os, "access", lambda *_: False)
    original_chmod = Path.chmod
    chmod_calls = []

    def chmod(path, mode):
        chmod_calls.append((path, mode))
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", chmod)
    media_tools.media_tool.cache_clear()
    try:
        assert media_tools.media_tool("ffprobe") == str(binary)
        assert chmod_calls and chmod_calls[0][0] == binary
    finally:
        media_tools.media_tool.cache_clear()


def test_linux_copies_to_temp_if_deployment_directory_is_read_only(tmp_path, monkeypatch):
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(media_tools, "_BUNDLED_BIN", tmp_path)
    monkeypatch.setattr(media_tools.sys, "platform", "linux")
    monkeypatch.setattr(media_tools.os, "access", lambda *_: False)
    original_chmod = Path.chmod

    def chmod(path, mode):
        if path == binary:
            raise PermissionError("read only")
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(media_tools.tempfile, "mkdtemp", lambda **_: str(tmp_path / "copy"))
    (tmp_path / "copy").mkdir()
    media_tools.media_tool.cache_clear()
    try:
        executable = Path(media_tools.media_tool("ffmpeg"))
        assert executable == tmp_path / "copy" / "ffmpeg"
        assert executable.read_bytes() == b"binary"
    finally:
        media_tools.media_tool.cache_clear()
