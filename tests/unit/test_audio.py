import base64

import pytest

from app.modules.realtime.audio import AudioCodec


def test_koreaaitel_mulaw_decode_and_encode():
    codec = AudioCodec()
    pcm = codec.decode(base64.b64encode(b"\xff" * 160).decode())
    assert len(pcm) in {638, 640} and set(pcm) == {0}
    result = base64.b64decode(codec.encode(b"\x00" * 960))
    assert len(result) == 160 and set(result) == {255}


def test_malformed_and_oversize_media_rejected():
    with pytest.raises(ValueError):
        AudioCodec().decode("!bad!")
    with pytest.raises(ValueError):
        AudioCodec().decode(base64.b64encode(b"x" * 8001).decode())
