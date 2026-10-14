"""HTTP auth attention blocks production drive admission and per-event guards."""
import unittest
from tests.v2 import browser_drive_gate_check as G
from jarvis.v2.browser import drive as D

class AuthDrive(unittest.TestCase):
    def setUp(self):
        self.fixture=G.Gate("run");self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
    def test_pending_auth_refuses_before_preparation_or_review(self):
        f=self.fixture;f.state["auth"]=True
        f.refused_without_io()
    def test_subresource_auth_between_events_prevents_the_next_input(self):
        f=self.fixture;events=[]
        def execute(snapshot,action,prepared,identity,forms,guard,**kwargs):
            guard();events.append("first")
            f.state["auth"]=True
            with self.assertRaises(D.LookRefused):guard()
            return D.ActResult("stopped","Stopped for owner authentication")
        f.host.execute_action.side_effect=execute
        self.assertEqual(f.action(text="x"*96).outcome,"stopped")
        self.assertEqual(events,["first"])

if __name__=="__main__":unittest.main()
