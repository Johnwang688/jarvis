"""BR9b bounded turn ownership/ref/lease tests; no Chrome or provider calls."""
from dataclasses import replace
from pathlib import Path
import sys
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import runtime
from jarvis.v2.browser.drive_state import (
    DriveState, DriveStateRefused, DriveTurn, LEASE_S, REFS_S, MAX_TABS,
    current_turn, turn_scope,
)
from jarvis.v2.browser.panes import PaneSnapshot, ResolvedSource


def pane(tab="1234abcd"):
    return PaneSnapshot("a"*32,1,1,"abcdef","remote",tab,tab,"p-0123abcd",None,
        "https://fixture.test/",True,True,True,False,1,1,1,ResolvedSource("owner"))


def turn(thread="thread",identifier="turn"):
    return DriveTurn(thread,identifier,False,"/project","owner's exact words")


class State(unittest.TestCase):
    def setUp(self):
        self.now=1.0;self.state=DriveState(clock=lambda:self.now);self.turn=turn();self.pane=pane()

    def test_only_remote_delivered_refs_can_be_capabilities(self):
        for snapshot,serial,refs in [
            (replace(self.pane,mode="direct",tab_id=None),"a"*32,["e1"]),
            (replace(self.pane,see=False),"a"*32,["e1"]),
            (replace(self.pane,covered=True),"a"*32,["e1"]),
            (self.pane,"bad",["e1"]),(self.pane,"a"*32,["e0"]),
            (self.pane,"a"*32,["e1"]*201),
        ]:
            self.state.publish(self.turn,snapshot,serial,refs)
            with self.assertRaises(DriveStateRefused):self.state.capability(self.turn,self.pane,"e1")
        self.state.publish(self.turn,self.pane,"a"*32,["e1","e2"])
        self.assertEqual(self.state.capability(self.turn,self.pane,"e1").serial,"a"*32)
        with self.assertRaises(DriveStateRefused):self.state.capability(self.turn,self.pane,"e3")

    def test_thread_turn_snapshot_and_fresh_serial_are_independent(self):
        self.state.publish(self.turn,self.pane,"a"*32,["e1"])
        for wrong in (turn("other"),turn(identifier="next")):
            with self.assertRaises(DriveStateRefused):self.state.capability(wrong,self.pane,"e1")
        for snapshot in (replace(self.pane,page_identity=2),replace(self.pane,document_generation=2),
                         replace(self.pane,source=ResolvedSource("unknown"))):
            with self.assertRaises(DriveStateRefused):self.state.capability(self.turn,snapshot,"e1")
        old=self.state.capability(self.turn,self.pane,"e1")
        self.assertTrue(self.state.refs_current(old))
        other=turn("other")
        self.state.publish(other,self.pane,"b"*32,["e1"])
        self.assertFalse(self.state.refs_current(old))
        with self.assertRaises(DriveStateRefused):self.state.capability(self.turn,self.pane,"e1")
        self.assertEqual(self.state.capability(other,self.pane,"e1").serial,"b"*32)

    def test_ref_expiry_retirement_and_stop(self):
        self.state.publish(self.turn,self.pane,"a"*32,["e1"])
        self.now+=REFS_S
        with self.assertRaises(DriveStateRefused):self.state.capability(self.turn,self.pane,"e1")
        self.state.publish(self.turn,self.pane,"a"*32,["e1"]);self.state.retire(self.turn.key)
        with self.assertRaises(DriveStateRefused):self.state.capability(self.turn,self.pane,"e1")
        self.state.stop();self.state.publish(self.turn,self.pane,"a"*32,["e1"])
        self.assertEqual(len(self.state.refs),0)

    def test_one_atomic_driver_wins_and_same_turn_cannot_parallelize(self):
        gate=threading.Barrier(3);won=[];lost=[]
        def claim(t):
            gate.wait()
            try:won.append(self.state.reserve(t,self.pane.tab_id))
            except DriveStateRefused:lost.append(t)
        workers=[threading.Thread(target=claim,args=(turn("t"+str(n)),)) for n in range(2)]
        for w in workers:w.start()
        gate.wait()
        for w in workers:w.join(1)
        self.assertEqual((len(won),len(lost)),(1,1))
        winner=turn(*won[0].owner)
        with self.assertRaises(DriveStateRefused):self.state.reserve(winner,self.pane.tab_id)
        self.state.finish(won[0])
        fresh=self.state.reserve(winner,self.pane.tab_id)
        self.assertNotEqual(fresh.token,won[0].token)

    def test_active_card_does_not_expire_and_idle_exact_30_seconds_does(self):
        lease=self.state.reserve(self.turn,self.pane.tab_id);other=turn("other")
        self.now+=200
        with self.assertRaises(DriveStateRefused):self.state.reserve(other,self.pane.tab_id)
        self.assertTrue(self.state.current(lease))
        self.state.finish(lease);self.now+=LEASE_S-.001
        with self.assertRaises(DriveStateRefused):self.state.reserve(other,self.pane.tab_id)
        self.now+=.001
        self.assertTrue(self.state.current(self.state.reserve(other,self.pane.tab_id)))

    def test_retire_active_revokes_input_but_waits_for_cleanup_before_next_driver(self):
        lease=self.state.reserve(self.turn,self.pane.tab_id);self.state.retire(self.turn.key)
        self.assertFalse(self.state.current(lease))
        self.now+=300
        with self.assertRaises(DriveStateRefused):self.state.reserve(turn("other"),self.pane.tab_id)
        self.state.finish(lease)
        new=self.state.reserve(turn("other"),self.pane.tab_id)
        self.state.finish(lease)
        self.assertTrue(self.state.current(new))

    def test_old_completion_never_releases_new_action(self):
        old=self.state.reserve(self.turn,self.pane.tab_id);self.state.finish(old)
        new=self.state.reserve(self.turn,self.pane.tab_id)
        self.state.finish(old)
        self.assertTrue(self.state.current(new));self.assertFalse(self.state.current(old))

    def test_40_judgements_per_turn_survive_action_and_tab_changes(self):
        lease=self.state.reserve(self.turn,self.pane.tab_id)
        for _ in range(40):self.state.charge_judgement(lease)
        self.assertEqual(self.state.counts(lease),(40,0))
        with self.assertRaises(DriveStateRefused):self.state.charge_judgement(lease)
        self.state.finish(lease);new=self.state.reserve(self.turn,"other-tab")
        with self.assertRaises(DriveStateRefused):self.state.charge_judgement(new)

    def test_hourly_actions_count_across_turns_and_expire_exactly(self):
        lease=self.state.reserve(self.turn,self.pane.tab_id)
        for _ in range(200):self.state.charge_action(lease)
        with self.assertRaises(DriveStateRefused):self.state.charge_action(lease)
        self.state.finish(lease);self.state.retire(self.turn.key)
        other=self.state.reserve(turn("other"),self.pane.tab_id)
        with self.assertRaises(DriveStateRefused):self.state.charge_action(other)
        self.now+=3600
        self.state.charge_action(other);self.assertEqual(self.state.counts(other),(0,1))

    def test_caps_refuse_without_evicting_active_driver_and_stop_revokes_all(self):
        leases=[self.state.reserve(turn("t"+str(i)),str(i)) for i in range(MAX_TABS)]
        with self.assertRaises(DriveStateRefused):self.state.reserve(turn("extra"),"extra")
        self.assertTrue(all(self.state.current(x) for x in leases))
        self.state.stop()
        self.assertTrue(all(not self.state.current(x) for x in leases))
        for lease in leases:self.state.finish(lease)
        self.assertEqual(self.state.drivers,{})


class TurnScope(unittest.TestCase):
    def setUp(self):
        self.saved=runtime.caller()
    def tearDown(self):
        # Restore exact ContextVar state; generic runtime bindings are not changed
        # by production drive_state.turn_scope.
        runtime._CALLER.set(self.saved)

    def test_context_is_caller_and_turn_bound_and_restored_on_error(self):
        t=turn();retired=[]
        runtime.bind_caller(t.thread_id,"project","fast",t.turn_id)
        self.assertIsNone(current_turn())
        with self.assertRaisesRegex(RuntimeError,"fixture"):
            with turn_scope(t,retired.append):
                self.assertIs(current_turn(),t)
                runtime.bind_caller("other","project","fast",t.turn_id)
                self.assertIsNone(current_turn())
                runtime.bind_caller(t.thread_id,"project","fast","other")
                self.assertIsNone(current_turn())
                runtime.bind_caller(t.thread_id,"project","claude",t.turn_id)
                self.assertIsNone(current_turn())
                raise RuntimeError("fixture")
        self.assertIsNone(current_turn());self.assertTrue(t.closed);self.assertEqual(retired,[t.key])

    def test_retire_exception_still_restores_context(self):
        t=turn();runtime.bind_caller(t.thread_id,"project","fast",t.turn_id)
        def fail(owner):raise RuntimeError("retire")
        with self.assertRaisesRegex(RuntimeError,"retire"):
            with turn_scope(t,fail):self.assertIs(current_turn(),t)
        self.assertIsNone(current_turn())

if __name__=="__main__":unittest.main()
