import pytest

from uv_readiness import SetupError, Target, parse_target, wheel_supports

PY313 = Target(13, False)
PY313T = Target(13, True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3.13", (13, False)), ("3.13.2", (13, False)), ("3.14t", (14, True))],
)
def test_parse_target(text: str, expected: tuple[int, bool]) -> None:
    assert parse_target(text) == expected


@pytest.mark.parametrize("text", ["", "3", "2.7", "py313", "3.x", "313"])
def test_parse_target_rejects_other_text(text: str) -> None:
    with pytest.raises(SetupError):
        parse_target(text)


def test_target_formats() -> None:
    assert (str(PY313), PY313.tag) == ("3.13", "cp313")
    assert (str(PY313T), PY313T.tag) == ("3.13t", "cp313t")


@pytest.mark.parametrize(
    ("filename", "standard", "free_threaded"),
    [
        ("six-1.17.0-py2.py3-none-any.whl", True, True),
        ("idna-3.20-py3-none-any.whl", True, True),
        ("foo-1.0-py313-none-any.whl", True, True),
        ("foo-1.0-py314-none-any.whl", False, False),
        ("foo-1.0-py2-none-any.whl", False, False),
        ("numpy-2.5.3-cp313-cp313-macosx_14_0_arm64.whl", True, False),
        ("numpy-2.5.3-cp313-cp313t-macosx_14_0_arm64.whl", False, True),
        ("numpy-1.26.4-cp312-cp312-macosx_11_0_arm64.whl", False, False),
        ("cryptography-44.0.0-cp39-abi3-manylinux_2_28_x86_64.whl", True, False),
        ("foo-1.0-cp314-abi3-manylinux_2_28_x86_64.whl", False, False),
        ("foo-1.0-cp313-none-win_amd64.whl", True, True),
        ("foo-1.0-pp311-pypy311_pp73-win_amd64.whl", False, False),
        ("garbage.whl", False, False),
    ],
)
def test_wheel_supports(filename: str, standard: bool, free_threaded: bool) -> None:
    assert wheel_supports(filename, PY313) is standard
    assert wheel_supports(filename, PY313T) is free_threaded
