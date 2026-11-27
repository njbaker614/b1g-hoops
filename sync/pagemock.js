/* Read only stand in for the Firebase connection, used only by the daily update (sync.py).
   It hands the hub page a snapshot of the league database so the update can run the hub's own
   round, lock and lineup code. Nothing the page does here is saved anywhere. */
(()=>{const STORE=__STORE__;
const snapDoc=p=>({exists:p in STORE,id:p.split('/').pop(),data:()=>structuredClone(STORE[p]),metadata:{fromCache:false}});
const snapCol=c=>({docs:Object.keys(STORE).filter(k=>k.startsWith(c+'/')&&k.split('/').length===c.split('/').length+1).map(k=>({id:k.split('/').pop(),data:()=>structuredClone(STORE[k])})),metadata:{fromCache:false}});
const ro=async()=>{throw {code:'read_only'}};
const doc=p=>({onSnapshot(cb){setTimeout(()=>cb(snapDoc(p)),0);return()=>{}},async get(){return snapDoc(p)},set:ro,update:ro,delete:ro});
const col=c=>({onSnapshot(cb){setTimeout(()=>cb(snapCol(c)),0);return()=>{}},orderBy(){return this},limit(){return this},add:ro,doc:k=>doc(c+'/'+k)});
const user={canEdit:async()=>false,isOwner:async()=>false,id:async()=>'daily-update',profiles:async ids=>Object.fromEntries(ids.map(i=>[i,{name:''}]))};
window.claude={use:async n=>n==='db'?{doc,collection:col}:n==='user'?user:null};})();
