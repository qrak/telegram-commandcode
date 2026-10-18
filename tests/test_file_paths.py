"""Path detection for auto-attached files (MEDIA: markers and bare paths)."""

from telegram_commandcode.chunking import _candidate_paths, find_file_paths


def test_media_marker_is_detected():
    assert _candidate_paths("done MEDIA:/tmp/chart.png") == ["/tmp/chart.png"]


def test_bare_posix_path_is_detected():
    assert _candidate_paths("wrote /tmp/report.pdf ok") == ["/tmp/report.pdf"]


def test_windows_drive_path_is_detected():
    paths = _candidate_paths(r"wrote C:\Users\qrak\out\report.pdf")
    assert paths == [r"C:\Users\qrak\out\report.pdf"]


def test_windows_media_path_is_detected():
    paths = _candidate_paths(r"MEDIA:C:\Temp\shot.png")
    assert paths == [r"C:\Temp\shot.png"]


def test_quoted_path_with_spaces_is_detected():
    paths = _candidate_paths(r'MEDIA:"C:\Program Files\app\shot.png" done')
    assert paths == [r"C:\Program Files\app\shot.png"]


def test_quoted_posix_path_with_spaces():
    paths = _candidate_paths('MEDIA:"/tmp/my reports/final v2.pdf"')
    assert paths == ["/tmp/my reports/final v2.pdf"]


def test_media_wins_over_bare_and_duplicates_are_dropped():
    text = "MEDIA:/tmp/a.png and again /tmp/a.png"
    assert _candidate_paths(text) == ["/tmp/a.png"]


def test_trailing_punctuation_is_stripped():
    assert _candidate_paths("see /tmp/log.txt.") == ["/tmp/log.txt"]


def test_non_paths_are_ignored():
    assert _candidate_paths("http://example.com/a.png") == []
    assert _candidate_paths("relative/path.png") == []


def test_find_file_paths_classifies_images(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"pdf")

    found = find_file_paths(f"MEDIA:{image} and MEDIA:{doc}")
    assert [(item["path"], item["type"]) for item in found] == [
        (str(image), "photo"),
        (str(doc), "document"),
    ]


def test_find_file_paths_skips_missing_files(tmp_path):
    missing = tmp_path / "nope.png"
    assert find_file_paths(f"MEDIA:{missing}") == []


def test_find_file_paths_caps_at_five(tmp_path):
    paths = []
    for i in range(7):
        p = tmp_path / f"{i}.txt"
        p.write_text("x")
        paths.append(f"MEDIA:{p}")
    assert len(find_file_paths(" ".join(paths))) == 5
