import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tempfile
from bridge_config import core_process_ids
import unittest
from unittest import mock
spec = importlib.util.spec_from_file_location("herdr_app", Path(__file__).with_name("herdr_bridge_app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class ActionTests(unittest.TestCase):
    def test_start_running_does_not_spawn_or_clear_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/"stop.request").write_text("keep")
            with mock.patch.object(app,"ROOT",root), mock.patch.object(app,"require_account"), mock.patch.object(app,"verify_transport"), mock.patch.object(app,"helper_locked",return_value=True), mock.patch.object(app.subprocess,"Popen") as spawn:
                self.assertIn("Already running",app.Controller().start())
                spawn.assert_not_called()
                self.assertEqual((root/"stop.request").read_text(),"keep")
    def test_hash_failure_does_not_spawn(self):
        with mock.patch.object(app,"require_account"), mock.patch.object(app,"verify_transport",side_effect=RuntimeError("changed")), mock.patch.object(app.subprocess,"Popen") as spawn:
            self.assertRaises(RuntimeError,app.Controller().start)
            spawn.assert_not_called()
    def test_account_failure_does_not_spawn(self):
        with mock.patch.object(app,"require_account",side_effect=RuntimeError("admin")), mock.patch.object(app.subprocess,"Popen") as spawn:
            self.assertRaises(RuntimeError,app.Controller().start)
            spawn.assert_not_called()
    def test_start_exact_existing_transport(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/"stop.request").write_text("old")
            with mock.patch.object(app,"ROOT",root), mock.patch.object(app,"require_account"), mock.patch.object(app,"verify_transport"), mock.patch.object(app,"helper_locked",return_value=False), mock.patch.object(app.subprocess,"Popen",return_value=mock.Mock(pid=99)) as spawn, mock.patch.dict(app.os.environ,{"HC_SOCKET":"wrong","HC_SESSION":"wrong"}):
                self.assertIn("99",app.Controller().start())
                args,kw=spawn.call_args
                self.assertEqual(args[0],[str(app.PYTHON),"-I",str(app.TRANSPORT)])
                self.assertNotIn("shell",kw)
                self.assertNotIn("HC_SOCKET",kw["env"])
                self.assertNotIn("HC_SESSION",kw["env"])
                self.assertEqual(kw["creationflags"],app.NO_WINDOW)
                self.assertFalse((root/"stop.request").exists())
    def test_stop_writes_only_sentinel(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with mock.patch.object(app,"ROOT",root), mock.patch.object(app,"require_account"), mock.patch.object(app.subprocess,"Popen") as spawn:
                app.Controller().stop()
                self.assertEqual([p.name for p in root.iterdir()],["stop.request"])
                spawn.assert_not_called()
    def test_repeat_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with mock.patch.object(app,"ROOT",root), mock.patch.object(app,"require_account"):
                app.Controller().stop();before=(root/"stop.request").read_bytes()
                app.Controller().stop()
                self.assertEqual((root/"stop.request").read_bytes(),before)
    def test_oversize_status_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/"status.json").write_bytes(b" "*70000)
            self.assertEqual(app.read_status(root)["phase"],"unreadable")
    def test_status_array_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/"status.json").write_text("[]")
            self.assertEqual(app.read_status(root)["phase"],"unreadable")
    def test_unreadable_locked_not_connected(self):
        self.assertNotEqual(app.view_state({"phase":"unreadable"},True,True)[0],"Helper reports connected")
    def test_snapshot_checks_existing_helper_only(self):
        with mock.patch.object(app,"read_status",return_value={"helper_pid":42}), mock.patch.object(app,"helper_locked",return_value=False), mock.patch.object(app,"python_process_alive") as alive:
            result=app.Controller().snapshot()
            self.assertFalse(result[2]);alive.assert_not_called()

class CoreStatusTests(unittest.TestCase):
    def test_returns_server_and_holder_pids(self):
        self.assertEqual(core_process_ids({"pid": 101, "holder": {"pid": 202}}), (101, 202))

    def test_missing_holder_fails_with_actionable_message(self):
        with self.assertRaisesRegex(RuntimeError, "No active Herdr holder"):
            core_process_ids({"pid": 101, "holder": None})

    def test_uses_unique_active_holder_when_current_holder_is_null(self):
        status = {"pid": 33396, "holder": None, "holders": [
            {"current": False, "instance": "example", "pid": 17988, "terminals": 12}
        ]}
        self.assertEqual(core_process_ids(status), (33396, 17988))

    def test_multiple_active_holders_fail_closed(self):
        status = {"pid": 33396, "holder": None, "holders": [
            {"pid": 17988, "terminals": 12}, {"pid": 19000, "terminals": 2}
        ]}
        with self.assertRaisesRegex(RuntimeError, "Multiple Herdr holders"):
            core_process_ids(status)

    def test_invalid_holder_pid_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no valid PID"):
            core_process_ids({"pid": 101, "holder": {"pid": None}})

if __name__=="__main__":
    unittest.main(verbosity=2)
