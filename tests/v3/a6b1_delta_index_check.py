"""Reading an initialized index must leave its directory untouched."""

import unittest

from a6b1_acceptance_check import Base, Model, reply
from jarvis.v3.index import Index


class ChatIndexRead(Base):
    def test_search_closed_wal_index_without_creating_sidecars(self):
        index_path = self.v3 / "index.sqlite"
        with Index(index_path) as index:
            index.add("jarvis", "fixture", title="Fixture", ts=1,
                      messages=[("user", "A seismograph measures motion.", 1)])
        original = {path.name: path.read_bytes() for path in self.v3.glob("index.sqlite*")}
        self.assertEqual(set(original), {"index.sqlite"})

        model = Model(reply(calls=[("index_search", {"query": "seismograph"})]), reply("Found it."))
        chat = self.service(model)
        chat.send("Search the index for seismograph.")
        self.assertEqual(self.wait_idle(chat), "idle")
        results = [row["content"] for row in model.seen[-1]["messages"] if row["role"] == "tool"]
        self.assertIn("A seismograph measures motion.", results[0])
        current = {path.name: path.read_bytes() for path in self.v3.glob("index.sqlite*")}
        self.assertEqual(set(current), set(original))
        self.assertEqual(current, original)


if __name__ == "__main__":
    unittest.main()
