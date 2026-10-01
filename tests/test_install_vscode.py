import importlib.util
import unittest
from pathlib import Path

_INSTALL_VSCODE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_vscode.py"
_INSTALL_VSCODE_SPEC = importlib.util.spec_from_file_location("install_vscode", _INSTALL_VSCODE_PATH)
if _INSTALL_VSCODE_SPEC is None or _INSTALL_VSCODE_SPEC.loader is None:
    raise ImportError(f"Unable to load install_vscode module from {_INSTALL_VSCODE_PATH}")
_INSTALL_VSCODE_MODULE = importlib.util.module_from_spec(_INSTALL_VSCODE_SPEC)
_INSTALL_VSCODE_SPEC.loader.exec_module(_INSTALL_VSCODE_MODULE)

build_remote_server_config = _INSTALL_VSCODE_MODULE.build_remote_server_config
build_server_config = _INSTALL_VSCODE_MODULE.build_server_config


class InstallVscodeConfigTests(unittest.TestCase):
    def test_local_server_config_passes_bearer_token_input_to_env(self) -> None:
        config = build_remote_server_config()

        self.assertEqual(config["env"]["BRUNO_BEARER_TOKEN"], "${input:BRUNO_BEARER_TOKEN}")

    def test_wsl_server_config_passes_bearer_token_input_to_env_command(self) -> None:
        config = build_server_config()

        if config["command"] == "wsl.exe":
            self.assertIn("BRUNO_BEARER_TOKEN=${input:BRUNO_BEARER_TOKEN}", config["args"])
        else:
            self.assertEqual(config["env"]["BRUNO_BEARER_TOKEN"], "${input:BRUNO_BEARER_TOKEN}")


if __name__ == "__main__":
    unittest.main()