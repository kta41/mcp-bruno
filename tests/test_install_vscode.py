import unittest

from scripts.install_vscode import build_remote_server_config, build_server_config


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