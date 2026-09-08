from __future__ import annotations

from urllib.parse import unquote

import pytest

from app.download_headers import attachment_content_disposition


@pytest.mark.parametrize("filename", ["安装支架 · 版本 02.step", '尺寸 "复核"; 50%.json', "model.glb", "设计😀.dxf"])
def test_attachment_filename_round_trips_utf8_with_ascii_fallback(filename: str) -> None:
    header = attachment_content_disposition(filename)
    header.encode("ascii")
    assert header.startswith('attachment; filename="')
    assert unquote(header.split("filename*=UTF-8''", 1)[1]) == filename
    fallback = header.split('filename="', 1)[1].split('";', 1)[0]
    assert '"' not in fallback
    assert ";" not in fallback


def test_attachment_filename_cannot_add_paths_or_header_lines() -> None:
    header = attachment_content_disposition('C:\\项目\\图纸\r\nX-Injected: true.step')
    assert "\r" not in header and "\n" not in header
    assert "\\" not in header
    decoded = unquote(header.split("filename*=UTF-8''", 1)[1])
    assert decoded == "图纸X-Injected: true.step"
    assert attachment_content_disposition("").startswith('attachment; filename="download";')
