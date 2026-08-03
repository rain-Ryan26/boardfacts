from importlib import import_module


def test_package_is_importable() -> None:
    package = import_module("flyboard")

    assert package.__name__ == "flyboard"
