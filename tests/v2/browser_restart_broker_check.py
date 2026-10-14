"""BR10 real broker commitment ordering; no model, IO or permission shortcut."""
from pathlib import Path
import sys, threading, unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.approvals import PendingApprovals, ApprovalRequest, Decision
from jarvis.v2.browser.activity import BrowserActivity


class Commit(unittest.TestCase):
    def setUp(self):
        self.gate=BrowserActivity();self.assertTrue(self.gate.opened('main',1));self.gate.desired(1)
        self.token=self.gate.try_restart('main',1,1)

    def test_pending_before_announcement_prevents_commit(self):
        announced=threading.Event();release=threading.Event();answers=[]
        def on_request(request):announced.set();release.wait(2)
        broker=PendingApprovals(on_request=on_request,timeout_s=2)
        worker=threading.Thread(target=lambda:answers.append(broker.ask(ApprovalRequest(tool='run_command',args={'command':'echo synthetic'}))))
        worker.start()
        try:
            self.assertTrue(announced.wait(1));calls=[]
            result=broker.run_if_idle(lambda:calls.append('commit') or self.gate.commit_restart(self.token))
            self.assertFalse(result);self.assertEqual(calls,[])
            self.assertEqual(self.gate.counts('main'),(0,True,False))
        finally:broker.shutdown();release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(answers,[Decision.DENY])

    def test_commit_before_new_card_finishes_without_answering_the_card(self):
        broker=PendingApprovals(timeout_s=2);calls=[]
        self.assertTrue(broker.run_if_idle(lambda:self.gate.commit_restart(self.token)))
        admitted=threading.Event();broker._on_request=lambda request:admitted.set()
        worker=threading.Thread(target=lambda:broker.ask(ApprovalRequest(tool='run_command',args={'command':'echo synthetic'})))
        worker.start()
        try:
            self.assertTrue(admitted.wait(1));self.assertEqual(len(broker.pending()),1)
            self.assertTrue(self.gate.opened('main',2,self.token));self.gate.finish_restart(self.token)
            self.assertFalse(broker.run_if_idle(lambda:calls.append('later') or True))
            self.assertEqual(calls,[]);self.assertEqual(len(broker.pending()),1)
        finally:broker.shutdown();worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_callback_and_approval_admission_share_one_lock(self):
        broker=PendingApprovals(timeout_s=2);inside=threading.Event();release=threading.Event();admitted=threading.Event()
        broker._on_request=lambda request:admitted.set()
        def commit():inside.set();release.wait(2);return self.gate.commit_restart(self.token)
        committed=[];first=threading.Thread(target=lambda:committed.append(broker.run_if_idle(commit)))
        first.start();self.assertTrue(inside.wait(1))
        started=threading.Event()
        def ask():started.set();broker.ask(ApprovalRequest(tool='run_command',args={'command':'echo synthetic'}))
        second=threading.Thread(target=ask);second.start()
        try:
            self.assertTrue(started.wait(1));self.assertFalse(admitted.wait(.05))
            release.set();first.join(1);self.assertEqual(committed,[True]);self.assertTrue(admitted.wait(1))
        finally:release.set();broker.shutdown();first.join(2);second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())

    def test_closed_and_changed_generation_never_commit(self):
        broker=PendingApprovals();self.gate.desired(2)
        self.assertFalse(broker.run_if_idle(lambda:self.gate.commit_restart(self.token)))
        broker.shutdown();calls=[]
        self.assertFalse(broker.run_if_idle(lambda:calls.append('after shutdown') or True))
        self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()