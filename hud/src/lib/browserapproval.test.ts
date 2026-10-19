import { browserApprovalDeadline, parseBrowserApproval, parseBrowserPrivateApproval, type BrowserApprovalPublic } from "./browserapproval";
const expected: BrowserApprovalPublic = {req_id:"request_1234",code:"ABCD",tool:"hud_browser_act",layer:"browser-gate",thread_id:"thread",hud_only:true,asked_at:"2026-10-13T00:00:00+00:00",timeout_s:120};
function fixture() {
  const args={action:"type",pane:"2",target:"e1",text:"hello \nworld",file:""};
  return {...expected,allowlistable:false,args,command:JSON.stringify(args),reason:"Never use model wording",headline:"not consent",
    browser:{tab_id:"0123abcd",action_id:"a".repeat(32),pane:2,page_identity:7,document_generation:3,rect:{x:1,y:2,width:3,height:4},action:"type",category:"ordinary",host:"example.test",path:"/path",target:"The field",text:args.text}};
}
it("accepts exact private data but discards arbitrary wording, raw args and unproven rect",()=>{
  const raw=fixture(), parsed=parseBrowserApproval(raw,expected)!;expect(parsed.command).toBe(raw.command);expect(parsed.browser.text).toBe(raw.args.text);
  expect(parsed).not.toHaveProperty("reason");expect(parsed).not.toHaveProperty("args");expect(parsed.browser).not.toHaveProperty("rect");
});
it.each(["req_id","tool","layer","code","thread_id","asked_at","timeout_s","hud_only","allowlistable"])("rejects mismatched identity %s",field=>{
  expect(parseBrowserApproval({...fixture(),[field]:"wrong"},expected)).toBeNull();
});
it.each([['tab_id','bad'],['action_id','f'.repeat(31)],['pane',0],['pane',13],['page_identity',-1],['document_generation',NaN],['action','run_script'],['category','approved'],['host','x'.repeat(201)],['path','x'.repeat(401)],['target','x'.repeat(121)],['text','x'.repeat(10001)],['rect',{x:1,y:2,width:-1,height:3}]])("rejects malformed private metadata %s",(field,value)=>{
  const raw=fixture();expect(parseBrowserApproval({...raw,browser:{...raw.browser,[field]:value}},expected)).toBeNull();
});
// FS0: a twelve-pane HUD's panes 1..12 are valid; only the upper bound moved.
// The card's args name the same pane, so only the bound itself can refuse.
function withPane(pane: unknown) {
  const raw=fixture(), args={...raw.args,pane:String(pane)};
  return {...raw,args,command:JSON.stringify(args),browser:{...raw.browser,pane}};
}
it.each([1,8,9,10,11,12])("accepts pane %s of a twelve-pane HUD",pane=>{
  expect(parseBrowserApproval(withPane(pane),expected)?.browser.pane).toBe(pane);
});
it.each([13,14,99,0,-1,1.5,12.000001,NaN,Infinity,-Infinity,true,false,null,"12","1",[12],{}])("rejects pane %s",pane=>{
  expect(parseBrowserApproval(withPane(pane),expected)).toBeNull();
});
it("rejects missing, altered or extra command fields, including text mismatch",()=>{
  const raw=fixture();
  for(const command of ["not JSON",JSON.stringify({...raw.args,text:"different"}),JSON.stringify({...raw.args,extra:"hidden"})])expect(parseBrowserApproval({...raw,command},expected)).toBeNull();
  expect(parseBrowserApproval({...raw,browser:{...raw.browser,text:"different"}},expected)).toBeNull();
  expect(parseBrowserApproval({...raw,args:{...raw.args,extra:"unshown"}},expected)).toBeNull();
});
it("accepts maximum Unicode text by code point, without truncating exact consent",()=>{
  const raw=fixture(), value="😀".repeat(10000);raw.args.text=value;raw.browser.text=value;raw.command=JSON.stringify(raw.args);
  expect(parseBrowserApproval(raw,expected)?.browser.text).toBe(value);
});
it("release download is a separate exact card, not an agent action",()=>{
  const raw=fixture(), download={id:"abcdef12",name:"sample.bin",size:123};
  const args={action:"release_download",download_id:download.id,name:download.name,size:download.size};
  const release={...raw,args,command:JSON.stringify(args),browser:{...raw.browser,action:"release_download",category:"executable_download",rect:null,text:"",download}};
  expect(parseBrowserApproval(release,expected)?.browser.download).toEqual(download);
  expect(parseBrowserApproval({...release,browser:{...release.browser,category:"ordinary"}},expected)).toBeNull();
  expect(parseBrowserApproval({...release,browser:{...release.browser,download:{...download,name:"x".repeat(201)}}},expected)).toBeNull();
  expect(parseBrowserApproval({...release,command:JSON.stringify({...args,size:124})},expected)).toBeNull();
});
it("derives a finite maximum120-second deadline from issued time, never load time",()=>{
  expect(browserApprovalDeadline(expected)).toBe(Date.parse(expected.asked_at)+120000);
  for(const timeout_s of [0,-1,121,Infinity,NaN])expect(browserApprovalDeadline({...expected,timeout_s})).toBe(0);
  expect(browserApprovalDeadline({...expected,asked_at:"bad"})).toBe(0);
});

it("keeps the conservative unjudged fallback usable as one exact owner card",()=>{
  const raw=fixture();expect(parseBrowserApproval({...raw,browser:{...raw.browser,category:"unjudged"}},expected)?.browser.category).toBe("unjudged");
});

it("accepts only the exact metadata-only named-rule delegation shape",()=>{
  const req={...expected,layer:"always-ask"};
  const raw={...req,allowlistable:false,args:{},command:null,browser:{kind:"delegation",tool:"hud_browser_act"},headline:"unused",reason:"unused"};
  expect(parseBrowserPrivateApproval(raw,req)).toEqual({kind:"delegation",reqId:req.req_id});
  expect(parseBrowserApproval(raw,req)).toBeNull();
  for(const delta of [{command:"private"},{args:{target:"private"}},{browser:null},{browser:{...raw.browser,page_identity:1}},
    {browser:{...raw.browser,tool:"run_command"}},{layer:"browser-gate"},{allowlistable:true},{req_id:"request_5678"},{timeout_s:121}]) {
    expect(parseBrowserPrivateApproval({...raw,...delta},req)).toBeNull();
  }
  expect(parseBrowserPrivateApproval(fixture(),req)).toBeNull();
  expect(parseBrowserPrivateApproval(raw,expected)).toBeNull();
});
