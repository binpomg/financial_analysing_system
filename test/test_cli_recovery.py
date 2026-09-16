import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

from finresearch.cli import dispatch, main, parser
from finresearch.storage import Store


class CLIRecoveryTests(unittest.TestCase):
    def test_failed_resumed_audit_has_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("finresearch.cli.resume_audit", return_value={"status": "failed"}), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["--data-dir", folder, "resume-audit", "run-1"])
            self.assertEqual(error.exception.code, 2)

    def test_collection_holds_lock_for_fetch_but_releases_during_interval(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            arguments = parser().parse_args(["collect", "--rounds", "2", "--interval", "1"])
            events = []

            def collect(*args):
                with self.assertRaises(RuntimeError):
                    with store.exclusive():
                        pass
                events.append("collect")
                return {"status": "complete"}

            def interval(seconds):
                with store.exclusive():
                    events.append("unlocked_interval")

            with patch("finresearch.cli.configuration", return_value={"sources": [{}]}), \
                    patch("finresearch.cli.collect_once", side_effect=collect), \
                    patch("finresearch.cli.time.sleep", side_effect=interval), contextlib.redirect_stdout(io.StringIO()):
                dispatch(store, arguments)
            self.assertEqual(events, ["collect", "unlocked_interval", "collect"])


if __name__ == "__main__":
    unittest.main()
