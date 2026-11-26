from pathlib import Path
import unittest

import messages_check as fixtures
from jarvis.v3.messages import Message


class ProjectRouting(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Delivery()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_successful_list_can_resolve_an_enclosing_t3_project(self):
        fixture = self.fixture
        for registered in (False, True):
            with self.subTest(registered=registered):
                enclosing = fixture.data if not registered else fixture.root
                expected = (fixture.data / "messages" if not registered
                            else Path(fixture.register("example")))
                fixture.projects.add(str(enclosing))
                fixture.t3.reset_mock()

                def resolve(path):
                    folder = Path(path)
                    return str(next(p for p in (folder, *folder.parents)
                                    if str(p) in fixture.projects))

                actual = []

                def start(path, prompt, **options):
                    actual.append(resolve(path))
                    return fixtures.Run(f"test-thread-{registered}", "test-run", "queued")

                def list_project(path):
                    resolve(path)
                    return []

                fixture.t3.list.side_effect = list_project
                fixture.t3.start.side_effect = start
                name = "example" if registered else None
                result = fixture.messages.send(Message("Fixture subject", "Fixture body", name))

                self.assertNotIn(str(enclosing), actual)
                if result.outcome == "sent":
                    self.assertEqual(actual, [str(expected)])


if __name__ == "__main__":
    unittest.main()
