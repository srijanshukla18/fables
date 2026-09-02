import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import serve
from fables_library import Library
from tests.test_library import make_zip


class ImportedServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library_root = self.root / "library"
        bundle = make_zip(self.root / "export.zip", text="server imported text")
        library = Library(self.library_root)
        digest = library.inspect(bundle, origin="m1-air")["sha256"]
        self.result = library.apply(bundle, "m1-air", digest)
        self.sid = self.result["created"][0]

        self.old_env = os.environ.get("FABLES_LIBRARY")
        os.environ["FABLES_LIBRARY"] = str(self.library_root)
        self.old_discover = serve.discover
        serve.discover = lambda home=None: ([], {}, [])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        serve.discover = self.old_discover
        if self.old_env is None:
            os.environ.pop("FABLES_LIBRARY", None)
        else:
            os.environ["FABLES_LIBRARY"] = self.old_env
        self.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.read(), response.headers.get_content_type()

    def test_imported_session_uses_same_ui_api_and_stable_id(self):
        body, _type = self.get("/api/sessions")
        sessions = json.loads(body)["sessions"]
        self.assertEqual(sessions[0]["id"], self.sid)
        self.assertTrue(sessions[0]["archived"])
        self.assertEqual(sessions[0]["origin"], "m1-air")
        self.assertTrue(sessions[0]["incomplete"])

        transcript, content_type = self.get("/api/session/" + self.sid)
        self.assertEqual(content_type, "application/json")
        archive = json.loads(transcript)
        self.assertEqual(archive["items"][0]["text"], "server imported text")

        provenance, _type = self.get("/api/provenance/" + self.sid)
        value = json.loads(provenance)
        self.assertEqual(value["session"]["id"], self.sid)
        self.assertEqual(value["provenance"][0]["import_id"], self.result["import_id"])


if __name__ == "__main__":
    unittest.main()
