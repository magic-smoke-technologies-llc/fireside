"""A hosted deployment must be able to say so from its environment.

`hosted` carries one behaviour: a hosted server refuses to create an account
with no email on file, because a social login can never match such an account
and silently mints a second one (#159). A container app sets its environment,
not its argv, so if the flag can only be read from the command line the fix is
merged, deployed, and switched off.
"""
import importlib.util
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _app_module():
    spec = importlib.util.spec_from_file_location("fs_host_app", ROOT / "host" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flag_from_command_line():
    app = _app_module()
    os.environ.pop("FIRESIDE_HOSTED", None)
    assert app.hosted_from(True) is True
    assert app.hosted_from(False) is False


def test_flag_from_environment():
    app = _app_module()
    os.environ["FIRESIDE_HOSTED"] = "1"
    try:
        assert app.hosted_from(False) is True
    finally:
        os.environ.pop("FIRESIDE_HOSTED", None)


def test_a_junk_value_is_not_hosted():
    app = _app_module()
    os.environ["FIRESIDE_HOSTED"] = "0"
    try:
        assert app.hosted_from(False) is False
    finally:
        os.environ.pop("FIRESIDE_HOSTED", None)
