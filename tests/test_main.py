import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from run import write_env_file


class FakeResponse:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.text = ""

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class MooviDumpTests(unittest.TestCase):
    def setUp(self):
        main.DUMP_ALL = False
        main.FULL_SANITIZER = False
        main.private_access_key = "private-key"

    def test_parse_course_selection_prefers_indexes_and_deduplicates(self):
        visible_courses = [{"id": 1678}, {"id": 1684}, {"id": 1702}]

        result = main.parse_course_selection("1,1684,2,1684,invalid", visible_courses)

        self.assertEqual(result, [1678, 1684])

    def test_validate_moodle_site_requires_https_without_credentials(self):
        self.assertEqual(main.validate_moodle_site("https://moodle.example/"), "https://moodle.example")
        self.assertIsNone(main.validate_moodle_site("http://moodle.example"))
        self.assertIsNone(main.validate_moodle_site("https://user:password@moodle.example"))

    def test_write_env_file_escapes_credentials(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"

            self.assertTrue(write_env_file(env_path, "user\"name", "pa\\ss\"word\nnext"))

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                'MOODLE_SITE="user\\"name"\n'
                'MOODLE_USERNAME="pa\\\\ss\\"word\\nnext"\n'
                'MOODLE_PASSWORD=""\n',
            )

    def test_download_to_path_resumes_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            target_path = tmp_path / "demo.txt"
            partial_path = target_path.with_suffix(".txt.part")
            partial_path.write_bytes(b"abc")

            response = FakeResponse(206, [b"defg"], headers={"Content-Length": "4"})
            fake_session = FakeSession(response)

            ok, bytes_written = main.download_to_path(
                "https://example.com/file",
                target_path,
                request_session=fake_session,
                resume=True,
            )

            self.assertTrue(ok)
            self.assertEqual(bytes_written, 7)
            self.assertEqual(target_path.read_bytes(), b"abcdefg")
            self.assertFalse(partial_path.exists())
            self.assertEqual(fake_session.calls[0][1]["headers"], {"Range": "bytes=3-"})

    def test_build_download_tasks_skips_existing_and_tracks_missing_urls(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dumps_dir = Path(tmp_dir)
            course_id = 1678
            course_dir = dumps_dir / main.sanitize(main.COURSE_ALIASES[course_id])
            existing_file = course_dir / main.sanitize("Tema 1") / main.sanitize("Modulo 1") / main.sanitize("existing.txt")
            existing_file.parent.mkdir(parents=True, exist_ok=True)
            existing_file.write_text("already-there", encoding="utf-8")

            courses = [{"id": course_id, "fullname": "FMI: Fundamentos", "hidden": False}]
            contents = [
                {
                    "section": 1,
                    "name": "Tema 1",
                    "modules": [
                        {
                            "name": "Modulo 1",
                            "contents": [
                                {
                                    "type": "file",
                                    "filename": "existing.txt",
                                    "fileurl": "https://moodle.example/webservice/pluginfile.php/10/mod_resource/content/0/existing.txt",
                                },
                                {
                                    "type": "file",
                                    "filename": "missing-url.txt",
                                    "fileurl": None,
                                },
                                {
                                    "type": "file",
                                    "filename": "new.txt",
                                    "fileurl": "https://moodle.example/webservice/pluginfile.php/10/mod_resource/content/0/new.txt",
                                },
                            ],
                        }
                    ],
                }
            ]

            def fake_post_webservice(function, arguments=None):
                self.assertEqual(function, "core_course_get_contents")
                self.assertEqual(arguments, {"courseid": course_id})
                return contents

            with patch.object(main, "post_webservice", side_effect=fake_post_webservice):
                tasks, preflight = main.build_download_tasks(courses, [course_id], dumps_dir, force_download=False)

            self.assertEqual(preflight["skipped_count"], 1)
            self.assertEqual(preflight["failed_count"], 1)
            self.assertEqual(len(tasks), 1)
            self.assertTrue(tasks[0]["relative_target"].endswith("new.txt"))

    def test_execute_download_tasks_runs_workers_in_parallel(self):
        barrier = threading.Barrier(2)
        thread_names = set()
        lock = threading.Lock()

        def fake_worker(task):
            with lock:
                thread_names.add(threading.current_thread().name)
            barrier.wait(timeout=2)
            return {
                "ok": True,
                "bytes_written": 10,
                "relative_target": task["relative_target"],
                "file_name": task["file_name"],
            }

        tasks = [
            {"relative_target": "one.txt", "file_name": "one.txt", "download_url": "https://example.com/1", "target_path": Path("one.txt"), "resume": True},
            {"relative_target": "two.txt", "file_name": "two.txt", "download_url": "https://example.com/2", "target_path": Path("two.txt"), "resume": True},
        ]

        with patch.object(main, "download_task_worker", side_effect=fake_worker):
            summary = main.execute_download_tasks(tasks, max_workers=2)

        self.assertEqual(summary["downloaded_count"], 2)
        self.assertEqual(summary["failed_count"], 0)
        self.assertGreaterEqual(len(thread_names), 2)


if __name__ == "__main__":
    unittest.main()
