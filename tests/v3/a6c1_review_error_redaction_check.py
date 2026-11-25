"""Invalid tool arguments must not be copied into chat error results."""

import unittest

from a6c1_acceptance_check import Base, CREATE, FAKE_TOKEN, reply


class ErrorRedactionReview(Base):
    def test_invalid_calendar_arguments_keep_credentials_out_of_the_error(self):
        self.token.write_text("fake-token")
        invalid = reply(calls=[("calendar_create", CREATE)])
        arguments = invalid.message["tool_calls"][0]["function"]
        arguments["arguments"] = arguments["arguments"].removesuffix("}")
        service = self.talk(invalid, reply("Done."))
        service.send("Book an appointment.")

        self.assertEqual(self.wait_idle(service), "idle")
        self.assertEqual((self.broker.pending(), self.states(None), self.cal.writes()), ([], [], []))
        (error,) = self.results()
        self.assertTrue(error.startswith("Error"))
        self.assertTrue(FAKE_TOKEN not in error, "The tool error repeated the fake credential.")


if __name__ == "__main__":
    unittest.main()
