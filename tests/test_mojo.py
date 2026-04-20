import struct

from harness_android.mojo import MojoJS, MOJOJS_FLAGS


def test_mojojs_flags_constant():
    assert "--enable-blink-features=MojoJS,MojoJSTest" in MOJOJS_FLAGS


def test_make_header_layout():
    hdr = MojoJS.make_header()
    assert len(hdr) == 24
    num_bytes, version, name, flags, request_id = struct.unpack("<IIIIq", hdr)
    assert num_bytes == 24
    assert version == 1
    assert name == 0
    assert flags == 0
    assert request_id == 0


def test_make_header_custom_fields():
    hdr = MojoJS.make_header(name=7, flags=2, request_id=42)
    num_bytes, version, name, flags, request_id = struct.unpack("<IIIIq", hdr)
    assert (num_bytes, version, name, flags, request_id) == (24, 1, 7, 2, 42)


def test_default_payloads_well_formed():
    payloads = MojoJS.default_payloads()
    assert len(payloads) >= 5
    assert all(isinstance(p, (bytes, bytearray)) for p in payloads)
    assert b"" in payloads
    # At least one payload starts with a valid header
    assert any(p.startswith(MojoJS.make_header()) for p in payloads if len(p) >= 24)
    # And there is a large payload for stress
    assert any(len(p) > 60000 for p in payloads)
