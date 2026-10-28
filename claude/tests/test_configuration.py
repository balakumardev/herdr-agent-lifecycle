import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configuration import load_config
from transport import Transport


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lifecycle-config-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / "settings.json"
        self.env = {"HERDR_LIFECYCLE_CONFIG": str(self.path)}

    def config(self, value=None):
        if value is not None:
            self.path.write_text(json.dumps(value))
        return load_config(env=self.env, home=self.home)

    def test_unconfigured_install_is_strict_and_notifications_are_disabled(self):
        config = self.config()
        self.assertIs(config["claude"]["allowIdleTeammates"], False)
        self.assertIs(config["notifications"]["enabled"], False)
        self.assertEqual(config["opencode"]["command"], "opencode")

    def test_direct_topic_reaches_the_real_transport_admission(self):
        config = self.config({"notifications": {"enabled": True, "topic": "fixture-topic", "endpoint": "https://notify.example.test/base"},
                              "claude": {"allowIdleTeammates": True}})
        transport = Transport(self.home / "state", topic=config["notifications"]["topic"], endpoint=config["notifications"]["endpoint"])
        with mock.patch.object(transport, "_send", return_value={"httpStatus": 200}) as send:
            result = transport.publish(session_id="root", kind="done", notice_id="turn", message="Final reply")
        self.assertEqual(result["status"], "sent")
        send.assert_called_once_with("fixture-topic", "Claude Code - Task Complete", "Final reply")

    def test_xdg_path_is_used(self):
        directory = self.home / "config/herdr-agent-lifecycle"
        directory.mkdir(parents=True)
        (directory / "config.json").write_text('{"opencode":{"command":"opencode2"}}')
        self.assertEqual(load_config(home=self.home, env={"XDG_CONFIG_HOME": str(directory.parent)})["opencode"]["command"], "opencode2")

    def test_invalid_values_do_not_enable_notifications_or_fallback(self):
        for value in ([], {"claude": {"allowIdleTeammates": "true"}}, {"notifications": {"enabled": "false"}},
                      {"notifications": {"enabled": True}}, {"notifications": {"enabled": True, "topic": "../private"}},
                      {"notifications": {"endpoint": "https://user:PRIVATE@notify.example.test"}},
                      {"notifications": {"endpoint": "file:///private"}}, {"opencode": {"command": ""}}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^Invalid lifecycle configuration$"):
                self.config(value)

    def test_malformed_json_does_not_leak_its_source(self):
        self.path.write_text("{PRIVATE")
        with self.assertRaisesRegex(ValueError, "^Invalid lifecycle configuration$"):
            self.config()
