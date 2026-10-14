import { parseBrowserAuth, validAuthFields } from "./browserauth";
const raw={t:"auth",id:"a".repeat(32),origin:"https://example.test",realm:"The server name",scheme:"basic",expires_in:60};
it("keeps only bounded auth metadata, never unknown credential fields",()=>{
  const result=parseBrowserAuth({...raw,username:"discard",password:"discard"});
  expect(result).toEqual({kind:"prompt",auth:{id:raw.id,origin:raw.origin,realm:raw.realm,scheme:"basic",expiresIn:60}});
});
it.each([{id:"bad"},{origin:"https://name:word@example.test"},{origin:"https://example.test/path"},{origin:"file://host"},
  {realm:"x".repeat(201)},{realm:"hidden\u202e"},{scheme:"ntlm"},{expires_in:0},{expires_in:61},{expires_in:NaN}])("refuses malformed auth metadata %j",delta=>{
  expect(parseBrowserAuth({...raw,...delta})).toBeNull();
});
it("closed frames bind an exact nonce and fixed reason",()=>{
  expect(parseBrowserAuth({t:"auth_closed",id:raw.id,reason:"expired"})).toEqual({kind:"closed",id:raw.id,reason:"expired"});
  expect(parseBrowserAuth({t:"auth_closed",id:raw.id,reason:"raw page text"})).toBeNull();
});
it("bounds UTF8 bytes and rejects control fields without changing their value",()=>{
  expect(validAuthFields("u".repeat(1024),"s".repeat(4096))).toBe(true);
  for(const [user,secret] of [["😀".repeat(257),""],["","😀".repeat(1025)],["x\n",""],["","hidden\u202e"]])expect(validAuthFields(user,secret)).toBe(false);
});
