"""Local size refusal happens before upload; hashing cannot outrun its bounds."""

from linkedin_mcp_server.linkedin import ext_media as media


def test_image_over_limit_is_refused_before_upload(tmp_path):
    image = tmp_path / "large.jpg"
    with image.open("wb") as stream:
        stream.truncate(media.IMAGE_MAX_BYTES + 1)
    items, bad = media.check_media([{"path": str(image)}])
    assert items == []
    assert bad is not None
    assert bad["status"] == "media_too_large"


def test_total_limit_applies_even_when_individual_images_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "IMAGES_TOTAL_MAX_BYTES", 15)
    paths = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    for p in paths:
        p.write_bytes(b"12345678")
    _, bad = media.check_media([{"path": str(p)} for p in paths])
    assert bad is not None
    assert bad["status"] == "media_too_large"


def test_hashing_catches_a_file_that_grew_after_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "IMAGE_MAX_BYTES", 16)
    p = tmp_path / "growing.jpg"
    p.write_bytes(b"12345678")
    items, bad = media.check_media([{"path": str(p)}])
    assert bad is None
    p.write_bytes(b"changed file after validation")
    assert media._local_image_hashes(items) is None
