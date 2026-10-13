"""Repeated cancellation must settle dispatched input and cleanup first."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import unittest
from jarvis.v2.browser import drive_execute as E
from jarvis.v2.browser.drive import Action
from jarvis.v2.browser.drive_scan import prepare_result
from tests.v2.browser_drive_scan_check import row
from tests.v2.browser_drive_state_check import pane

class Cancellation(unittest.IsolatedAsyncioTestCase):
    async def fixture(self,action):
        snap=pane();prepared=replace(prepare_result(row(),"e1",()),focused_target=True)
        tab=SimpleNamespace(input_task=None,dialogs=[],pressed={})
        host=SimpleNamespace(_look_lock=None,_look_tasks=set(),_let_go=lambda params:{"type":"mouseMoved","button":"none","buttons":0})
        guard=Mock();guard.return_value=(snap,{})
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        block="Input.insertText" if action=="type" else "Input.dispatchMouseEvent"
        async def send(method,params):
            calls.append((method,dict(params)))
            if method==block and (action=="type" or params.get("type")=="mousePressed"):
                entered.set();await release.wait()
            return {}
        cdp=SimpleNamespace(send=send)
        world=patch.object(E,"_world",new=AsyncMock(return_value=(tab,cdp,1)));world.start();self.addCleanup(world.stop)
        prep=patch.object(E,"_prepared",new=AsyncMock(return_value=prepared));prep.start();self.addCleanup(prep.stop)
        async def evaluate(*args,**kwargs):return "object" if kwargs.get("by_value")==False else {"x":20,"y":20}
        ev=patch.object(E,"_evaluate",side_effect=evaluate);ev.start();self.addCleanup(ev.stop)
        task=asyncio.create_task(E.execute(host,snap,Action(action,"1","e1","x"*96 if action=="type" else ""),prepared,"pin",(),guard))
        await asyncio.wait_for(entered.wait(),1)
        return host,tab,task,release,calls

    async def test_two_cancels_wait_for_ack_count_it_and_send_no_next_chunk(self):
        host,tab,task,release,calls=await self.fixture("type")
        task.cancel();await asyncio.sleep(0);task.cancel()
        for _ in range(5):await asyncio.sleep(0)
        self.assertFalse(task.done());self.assertIn(task,host._look_tasks)
        self.assertIsNotNone(tab.input_task);self.assertFalse(tab.input_task.done())
        self.assertEqual(sum(method=="Input.insertText" for method,_ in calls),1)
        release.set();result=await asyncio.wait_for(task,1)
        self.assertEqual(result.outcome,"stopped");self.assertIn("32 characters",result.text)
        self.assertIsNone(tab.input_task);self.assertEqual(host._look_tasks,set())
        self.assertEqual(sum(method=="Input.insertText" for method,_ in calls),1)

    async def test_cancelled_press_has_neutral_reset_and_never_mouseup(self):
        host,tab,task,release,calls=await self.fixture("click")
        task.cancel();await asyncio.sleep(0);task.cancel();release.set()
        result=await asyncio.wait_for(task,1)
        self.assertEqual(result.outcome,"stopped")
        mouse=[params for method,params in calls if method=="Input.dispatchMouseEvent"]
        self.assertEqual([params["type"] for params in mouse],["mousePressed","mouseMoved"])
        self.assertEqual(mouse[-1]["buttons"],0);self.assertEqual(host._look_tasks,set())

    async def test_cleanup_does_not_reset_new_owner_pressed_button(self):
        host,tab,task,release,calls=await self.fixture("click")
        tab.pressed["left"]=4;task.cancel();release.set();await asyncio.wait_for(task,1)
        self.assertEqual(tab.pressed,{"left":4})
        self.assertEqual([p["type"] for m,p in calls if m=="Input.dispatchMouseEvent"],["mousePressed"])

if __name__=="__main__":unittest.main()
