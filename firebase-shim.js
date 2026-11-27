/* Connects the league hub to Firebase (Firestore + Google sign-in).
   It provides window.claude.use('db') and use('user') with the same calls the hub already makes,
   so the hub page itself did not need rewriting. Settings live in firebase-config.js. */
(function(){
  const V='10.12.2';const CDN=`https://www.gstatic.com/firebasejs/${V}/`;
  let ready=null;
  const INVITE=(()=>{try{return new URLSearchParams(location.search).get('invite')||''}catch(e){return ''}})();
  const dropInvite=()=>{try{if(INVITE){const u=new URL(location.href);u.searchParams.delete('invite');history.replaceState(null,'',u.pathname+u.search+u.hash)}}catch(e){}};
  const clean=v=>JSON.parse(JSON.stringify(v));
  function init(){
    if(ready)return ready;
    ready=(async()=>{
      const cfg=window.FIREBASE_CONFIG;
      if(!cfg||!cfg.apiKey){bar({state:'nocfg'});return null}
      const [{initializeApp},fs,au]=await Promise.all([import(CDN+'firebase-app.js'),import(CDN+'firebase-firestore.js'),import(CDN+'firebase-auth.js')]);
      const app=initializeApp(cfg);const F=fs.getFirestore(app);const A=au.getAuth(app);
      const user=await new Promise(r=>{const un=au.onAuthStateChanged(A,u=>{un();r(u)})});
      au.onAuthStateChanged(A,u=>{if((u&&u.uid)!==(user&&user.uid))location.reload()});
      let admins=null,member=false;
      if(user){
        try{const s=await fs.getDoc(fs.doc(F,'meta/admins'));admins=s.exists()?(s.data().uids||[]):null}catch(e){admins=[]}
        if(admins&&admins.includes(user.uid))member=true;
        else{try{member=(await fs.getDoc(fs.doc(F,'members/'+user.uid))).exists()}catch(e){member=false}}
        if(member)fs.setDoc(fs.doc(F,'profiles/'+user.uid),{name:user.displayName||user.email||'',email:user.email||''},{merge:true}).catch(()=>{});
      }
      const ctx={fs,F,A,au,user,admins,member};
      if(member)dropInvite();
      bar({state:user?'in':'out',ctx});
      if(user&&admins&&admins.includes(user.uid)){importOffer(ctx);
        fs.getDoc(fs.doc(F,'secret/league')).then(s=>{if(!s.exists())pwPanel(ctx,true)}).catch(()=>{})}
      return ctx;
    })().catch(e=>{console.error(e);bar({state:'err',msg:String(e.message||e)});return null});
    return ready;
  }
  function wrapDoc(c,p){const r=c.fs.doc(c.F,p);return{
    onSnapshot(cb,err){return c.fs.onSnapshot(r,s=>cb({exists:s.exists(),id:s.id,data:()=>s.data(),metadata:{fromCache:false}}),e=>{console.warn(p,e);err&&err(e)})},
    set:v=>c.fs.setDoc(r,clean(v)),update:v=>c.fs.setDoc(r,clean(v),{merge:true}),delete:()=>c.fs.deleteDoc(r),
    async get(){const s=await c.fs.getDoc(r);return{exists:s.exists(),id:s.id,data:()=>s.data()}}}}
  function wrapCol(c,p,cons=[]){return{
    orderBy:(f,d)=>wrapCol(c,p,[...cons,c.fs.orderBy(f,d)]),limit:n=>wrapCol(c,p,[...cons,c.fs.limit(n)]),
    doc:k=>wrapDoc(c,p+'/'+k),add:v=>c.fs.addDoc(c.fs.collection(c.F,p),clean(v)),
    onSnapshot(cb,err){return c.fs.onSnapshot(c.fs.query(c.fs.collection(c.F,p),...cons),q=>cb({docs:q.docs.map(d=>({id:d.id,data:()=>d.data()})),metadata:{fromCache:false}}),e=>{console.warn(p,e);err&&err(e)})}}}
  window.claude={async use(name){const c=await init();if(!c||!c.user||!c.member)return null;
    if(name==='db')return{doc:p=>wrapDoc(c,p),collection:p=>wrapCol(c,p)};
    if(name==='user'){const isAdm=()=>!!(c.admins&&c.admins.includes(c.user.uid));return{
      canEdit:async()=>isAdm(),isOwner:async()=>isAdm(),id:async()=>c.user.uid,
      async profiles(ids){const out={};await Promise.all(ids.map(async id=>{try{const s=await c.fs.getDoc(c.fs.doc(c.F,'profiles/'+id));out[id]={name:s.exists()?(s.data().name||''):''}}catch(e){out[id]={name:''}}}));return out}}}
    return null}};
  /* Sign in bar */
  function el(h){const d=document.createElement('div');d.innerHTML=h;return d.firstElementChild}
  function bar({state,ctx,msg}){
    const go=()=>{let b=document.getElementById('fbBar');if(b)b.remove();
      const css='position:fixed;right:12px;bottom:12px;z-index:9999;background:#0E2A47;color:#fff;font:500 13px/1.3 system-ui,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 6px 20px rgba(0,0,0,.25);display:flex;gap:10px;align-items:center;max-width:calc(100vw - 24px);flex-wrap:wrap';
      const btn='background:#E8702A;color:#fff;border:0;border-radius:6px;padding:6px 10px;font:600 13px system-ui;cursor:pointer';
      const ghost='background:transparent;color:#fff;border:1px solid rgba(255,255,255,.5);border-radius:6px;padding:5px 9px;font:500 12px system-ui;cursor:pointer';
      let h='';
      if(state==='nocfg')h=`<div id="fbBar" style="${css}">Database not connected yet (firebase-config.js is empty). Showing preseason data only.</div>`;
      else if(state==='err')h=`<div id="fbBar" style="${css}">Could not connect: ${msg.replace(/</g,'&lt;')}</div>`;
      else if(state==='out')h=`<div id="fbBar" style="${css}"><span>${INVITE?"<strong>You're invited to the league.</strong> Sign in with Google to join.":'Sign in to see the league and set your lineup.'}</span><button id="fbIn" style="${btn}">Sign in with Google</button></div>`;
      else{const u=ctx.user;const adm=ctx.admins&&ctx.admins.includes(u.uid);
        const nm=(u.displayName||u.email||'Signed in').replace(/</g,'&lt;');
        if(!ctx.member&&ctx.admins!==null)h=`<div id="fbBar" style="${css}"><span><strong>This league is private.</strong> Enter the league password from the commissioner.</span><input id="fbCode" type="password" autocomplete="off" placeholder="League password" value="${INVITE.replace(/[&<>"]/g,'')}" style="padding:6px 8px;border-radius:6px;border:0;font:500 13px system-ui;min-width:150px"><button id="fbJoin" style="${btn}">Enter league</button><button id="fbOut" style="${ghost}">Sign out</button><span id="fbCodeMsg" style="flex-basis:100%;color:#FFC9A8;display:none"></span></div>`;
        else h=`<div id="fbBar" style="${css}"><span>${nm}${adm?' · Commissioner':''}</span>${ctx.admins===null?`<button id="fbClaim" style="${btn}">I am the commissioner</button>`:''}${adm?`<button id="fbPw" style="${ghost}">League password</button>`:''}<button id="fbOut" style="${ghost}">Sign out</button></div>`}
      b=el(h);document.body.appendChild(b);
      b.querySelector('#fbIn')?.addEventListener('click',async()=>{const c=await init();try{await c.au.signInWithPopup(c.A,new c.au.GoogleAuthProvider())}catch(e){alert('Sign in failed: '+e.message)}});
      b.querySelector('#fbOut')?.addEventListener('click',async()=>{await ctx.au.signOut(ctx.A)});
      const jb=b.querySelector('#fbJoin');
      if(jb){const inp=b.querySelector('#fbCode'),m=b.querySelector('#fbCodeMsg');
        const tryJoin=async()=>{const code=(inp.value||'').trim().toLowerCase();if(!code){inp.focus();return}
          jb.disabled=true;jb.textContent='Checking…';
          try{await ctx.fs.setDoc(ctx.fs.doc(ctx.F,'members/'+ctx.user.uid),{code,name:ctx.user.displayName||ctx.user.email||'',email:ctx.user.email||'',ts:Date.now()});dropInvite();location.reload()}
          catch(e){m.style.display='block';m.textContent='That password is not right. Check with the commissioner and try again.';jb.disabled=false;jb.textContent='Enter league'}};
        jb.addEventListener('click',tryJoin);inp.addEventListener('keydown',e=>{if(e.key==='Enter')tryJoin()});if(INVITE)setTimeout(tryJoin,0)}
      b.querySelector('#fbPw')?.addEventListener('click',()=>pwPanel(ctx));
      b.querySelector('#fbClaim')?.addEventListener('click',async()=>{if(!confirm('Make this Google account the league commissioner? Only the first person can do this.'))return;try{await ctx.fs.setDoc(ctx.fs.doc(ctx.F,'meta/admins'),{uids:[ctx.user.uid]});location.reload()}catch(e){alert('Could not set commissioner: '+e.message)}});
    };
    if(document.body)go();else document.addEventListener('DOMContentLoaded',go);
  }
  /* Commissioner: set the league password and see or remove members */
  async function pwPanel(c,first){
    document.getElementById('fbPwPanel')?.remove();
    const esc=t=>String(t||'').replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
    let cur='';try{const s=await c.fs.getDoc(c.fs.doc(c.F,'secret/league'));if(s.exists())cur=s.data().code||''}catch(e){}
    let mem=[];try{const q=await c.fs.getDocs(c.fs.collection(c.F,'members'));mem=q.docs.map(d=>({id:d.id,...d.data()})).sort((a,b)=>(a.ts||0)-(b.ts||0))}catch(e){}
    const p=el(`<div id="fbPwPanel" style="position:fixed;right:12px;top:12px;z-index:10000;background:#fff;color:#111;border:2px solid #0E2A47;font:500 13px/1.4 system-ui,sans-serif;padding:14px;border-radius:10px;width:min(380px,calc(100vw - 24px));max-height:calc(100vh - 110px);overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.25)">
      <div style="display:flex;justify-content:space-between;align-items:center"><strong>League password</strong><button id="fbPwX" style="border:0;background:none;font-size:18px;cursor:pointer" aria-label="Close">×</button></div>
      <div style="color:#555;margin:4px 0 8px">${first?'<strong style="color:#B3261E">No password set yet.</strong> Nobody but you can get in until you set one. ':''}People enter this once after signing in with Google. Share it only with your league. It is not case sensitive.</div>
      <div style="display:flex;gap:6px"><input id="fbPwIn" value="${esc(cur)}" placeholder="e.g. hoosierhoops27" style="flex:1;padding:7px 8px;border:1px solid #bbb;border-radius:6px;font:500 13px system-ui"><button id="fbPwSave" style="background:#E8702A;color:#fff;border:0;border-radius:6px;padding:7px 12px;font:600 13px system-ui;cursor:pointer">Save</button></div>
      <div id="fbPwMsg" style="margin-top:6px;color:#555"></div>
      <div style="margin-top:12px"><strong>Members (${mem.length})</strong> <span style="color:#555">· people who entered the password. You are always in as commissioner.</span></div>
      <div id="fbMem">${mem.length?mem.map(m=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-top:1px solid #eee"><span>${esc(m.name||'Unknown')}<br><span style="color:#777;font-size:12px">${esc(m.email)}</span></span><button data-rm="${esc(m.id)}" style="background:#fff;color:#B3261E;border:1px solid #B3261E;border-radius:6px;padding:4px 8px;font:600 12px system-ui;cursor:pointer">Remove</button></div>`).join(''):'<div style="color:#777;padding:6px 0">No one yet.</div>'}</div>
      <div style="color:#777;font-size:12px;margin-top:8px">Changing the password does not remove anyone already in. Removing a member takes away access; also remove his team in Commissioner, Step 4.</div></div>`);
    document.body.appendChild(p);
    p.querySelector('#fbPwX').addEventListener('click',()=>p.remove());
    p.querySelector('#fbPwSave').addEventListener('click',async()=>{const v=p.querySelector('#fbPwIn').value.trim().toLowerCase();const m=p.querySelector('#fbPwMsg');
      if(v.length<6){m.style.color='#B3261E';m.textContent='Use at least 6 characters.';return}
      try{await c.fs.setDoc(c.fs.doc(c.F,'secret/league'),{code:v,updatedAt:Date.now()});p.querySelector('#fbPwIn').value=v;m.style.color='#1B6E3A';m.textContent='Saved. New people use this password from now on.'}catch(e){m.style.color='#B3261E';m.textContent='Could not save: '+e.message}});
    p.querySelectorAll('[data-rm]').forEach(bt=>bt.addEventListener('click',async()=>{if(!confirm('Remove this person from the league? He will lose access until he enters the password again.'))return;
      try{await c.fs.deleteDoc(c.fs.doc(c.F,'members/'+bt.dataset.rm));bt.closest('div').remove()}catch(e){alert('Could not remove: '+e.message)}}));
  }
  /* One time import of the league from the Claude hub export (commissioner only, only while the database is empty) */
  async function importOffer(c){
    const s=await c.fs.getDoc(c.fs.doc(c.F,'league/config'));if(s.exists())return;
    const go=()=>{const b=el(`<div style="position:fixed;left:12px;bottom:12px;z-index:9999;background:#fff;color:#111;border:2px solid #E8702A;font:500 13px system-ui;padding:12px;border-radius:10px;max-width:360px;box-shadow:0 6px 20px rgba(0,0,0,.2)"><strong>Empty league database.</strong> Import the league export file (league-export.json) to bring over settings, coaches, draft, rosters and stats.<div style="margin-top:8px"><input type="file" accept=".json,application/json" id="fbImp"></div><div id="fbImpMsg" style="margin-top:6px;color:#555"></div></div>`);
      document.body.appendChild(b);
      b.querySelector('#fbImp').addEventListener('change',async e=>{const f=e.target.files[0];if(!f)return;const m=b.querySelector('#fbImpMsg');
        try{const data=JSON.parse(await f.text());let n=0;for(const [path,v] of Object.entries(data.docs||{})){await c.fs.setDoc(c.fs.doc(c.F,path),clean(v));n++;m.textContent=`Imported ${n} items…`}
          m.textContent=`Done: ${n} items imported. Reloading…`;setTimeout(()=>location.reload(),900)}catch(err){m.textContent='Import failed: '+err.message}});};
    if(document.body)go();else document.addEventListener('DOMContentLoaded',go);
  }
})();
