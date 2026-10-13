import {beforeEach,it,expect,vi} from "vitest";
const mocks=vi.hoisted(()=>({get:vi.fn(),write:vi.fn()}));
vi.mock("../api",()=>({browserApi:mocks}));
import {browserSettings as settings} from "./browserSettings";
beforeEach(()=>{mocks.get.mockReset();mocks.write.mockReset();settings.apply(null);});
it.each([undefined,null,1,"true",[],{}])("missing/malformed %j never enables controls",async(value)=>{
 mocks.get.mockResolvedValue({agents_may_drive:value});await settings.refresh();expect(settings.drive()).toBeNull();
});
it("accepts only actual boolean owner receipts",async()=>{
 mocks.get.mockResolvedValue({agents_may_drive:true});await settings.refresh();expect(settings.drive()).toBe(true);
 mocks.write.mockResolvedValue({agents_may_drive:false});await settings.setDrive(false);expect(settings.drive()).toBe(false);
 expect(mocks.write).toHaveBeenCalledWith("/settings",{agents_may_drive:false},"PATCH");
});
it("late GET and PATCH cannot revive disconnected settings",async()=>{
 let get!:(x:any)=>void,write!:(x:any)=>void;
 mocks.get.mockImplementation(()=>new Promise(r=>get=r));const reading=settings.refresh();settings.apply(null);
 get({agents_may_drive:true});await reading;expect(settings.drive()).toBeNull();
 mocks.write.mockImplementation(()=>new Promise(r=>write=r));const writing=settings.setDrive(true);settings.apply(null);
 write({agents_may_drive:true});await writing;expect(settings.drive()).toBeNull();
});
it("failed or malformed save makes permission unknown",async()=>{
 settings.apply(true);mocks.write.mockResolvedValue({agents_may_drive:"true"});await expect(settings.setDrive(false)).rejects.toThrow();expect(settings.drive()).toBeNull();
 settings.apply(true);mocks.write.mockRejectedValue(Error("offline"));await expect(settings.setDrive(false)).rejects.toThrow("offline");expect(settings.drive()).toBeNull();
});
