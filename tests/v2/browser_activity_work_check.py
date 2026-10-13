"""BR10 actual async cleanup, not concurrent-Future completion."""
from pathlib import Path
import asyncio, sys, threading, unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser.activity import BrowserActivity, ActivityRefused
from jarvis.v2.browser.activity_work import ActivityWork


class Work(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate=BrowserActivity();self.gate.opened('main',1);self.gate.desired(1)
        self.work=ActivityWork(self.gate)

    async def test_cancelled_caller_keeps_actual_cleanup_admitted(self):
        entered=asyncio.Event();cleaning=asyncio.Event();finish=asyncio.Event()
        async def operation():
            try:entered.set();await asyncio.Event().wait()
            finally:cleaning.set();await finish.wait()
        task=asyncio.create_task(self.work.run('main',1,operation))
        await entered.wait();task.cancel();await cleaning.wait()
        self.assertEqual(self.gate.counts('main')[0],1)
        self.assertIsNone(self.gate.try_restart('main',1,1))
        finish.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.gate.counts('main')[0],0)
        self.assertIsNotNone(self.gate.try_restart('main',1,1))

    async def test_shutdown_does_not_cancel_already_running_cleanup_again(self):
        entered=asyncio.Event();cleaning=asyncio.Event();finish=asyncio.Event()
        async def operation():
            try:entered.set();await asyncio.Event().wait()
            finally:cleaning.set();await finish.wait()
        task=asyncio.create_task(self.work.run('main',1,operation))
        await entered.wait();task.cancel();await cleaning.wait()
        closing=asyncio.create_task(self.work.settle());await asyncio.sleep(0)
        self.assertEqual(task.cancelling(),1)
        self.assertEqual(self.gate.counts('main')[0],1)
        self.assertFalse(closing.done())
        finish.set();await closing
        self.assertEqual(self.gate.counts('main')[0],0)
    async def test_lazy_operation_never_starts_after_restart_admission(self):
        token=self.gate.try_restart('main',1,1);calls=[]
        async def operation():calls.append('CDP')
        with self.assertRaises(ActivityRefused):await self.work.run('main',1,operation)
        self.assertEqual(calls,[]);self.assertTrue(self.gate.commit_restart(token))

    async def test_nested_work_stays_registered_until_outer_cleanup(self):
        exited=asyncio.Event();finish=asyncio.Event()
        async def inner():return 7
        async def outer():
            self.assertEqual(await self.work.run('main',1,inner),7)
            exited.set();await finish.wait()
        task=asyncio.create_task(self.work.run('main',1,outer));await exited.wait()
        self.assertEqual(self.gate.counts('main')[0],1)
        self.assertEqual(set(self.work._tasks.values()),{task})
        await self.work.settle()
        self.assertTrue(task.cancelled());self.assertEqual(self.work._tasks,{})
        self.assertFalse(self.gate.opened('main',2))
        with self.assertRaises(ActivityRefused):self.gate.enter('main',1)

    async def test_error_releases_exact_token_and_other_profile_is_independent(self):
        self.gate.opened('other',2)
        other=self.gate.enter('other',2)
        async def operation():raise ValueError('synthetic')
        with self.assertRaisesRegex(ValueError,'synthetic'):await self.work.run('main',1,operation)
        self.assertEqual(self.work._tasks,{})
        self.assertEqual(self.gate.counts('main')[0],0)
        self.assertEqual(self.gate.counts('other')[0],1)
        self.gate.leave(other)


class Threaded(unittest.TestCase):
    def test_concurrent_future_cancel_is_not_coroutine_completion(self):
        loop=asyncio.new_event_loop();ready=threading.Event();entered=threading.Event();cleaning=threading.Event();done=threading.Event()
        gate=BrowserActivity();gate.opened('main',1);gate.desired(1);work=ActivityWork(gate)
        finish=None
        async def operation():
            nonlocal finish
            finish=asyncio.Event()
            try:entered.set();await asyncio.Event().wait()
            finally:cleaning.set();await finish.wait();done.set()
        def run():asyncio.set_event_loop(loop);ready.set();loop.run_forever()
        thread=threading.Thread(target=run);thread.start();self.assertTrue(ready.wait(1))
        future=asyncio.run_coroutine_threadsafe(work.run('main',1,operation),loop)
        try:
            self.assertTrue(entered.wait(1));future.cancel();self.assertTrue(future.done())
            self.assertTrue(cleaning.wait(1));self.assertFalse(done.is_set())
            self.assertIsNone(gate.try_restart('main',1,1))
            loop.call_soon_threadsafe(finish.set);self.assertTrue(done.wait(1))
            async def barrier():await asyncio.sleep(0)
            asyncio.run_coroutine_threadsafe(barrier(),loop).result(1)
            self.assertIsNotNone(gate.try_restart('main',1,1))
        finally:
            if finish is not None:loop.call_soon_threadsafe(finish.set)
            asyncio.run_coroutine_threadsafe(work.settle(),loop).result(2)
            loop.call_soon_threadsafe(loop.stop);thread.join(2);loop.close()
        self.assertFalse(thread.is_alive())

if __name__=='__main__':unittest.main()