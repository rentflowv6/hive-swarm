const cv=$("#brain"), ctx=cv.getContext("2d"); let W=0,H=0,DPR=1;
function resize(){const r=cv.getBoundingClientRect();DPR=window.devicePixelRatio||1;W=r.width;H=r.height;cv.width=Math.max(1,W*DPR);cv.height=Math.max(1,H*DPR);ctx.setTransform(DPR,0,0,DPR,0,0);layout()}
window.addEventListener("resize",resize);
const COL={queued:[70,90,140],researching:[62,230,255],working:[255,183,43],done:[92,255,157],error:[255,92,122]};
const ambient=[]; for(let i=0;i<95;i++) ambient.push({x:Math.random(),y:Math.random(),vx:(Math.random()-.5)*.00025,vy:(Math.random()-.5)*.00025,r:Math.random()*1.6+.4});
let neurons=[], pulses=[], flows=[], core={x:0,y:0,fire:0}, gate={x:0,y:0}, phaseName="idle";
function layout(){
  core.x=W*.62; core.y=H*.54; gate.x=Math.max(32,W*.065); gate.y=H*.51;
  const mains=agents.map((a,i)=>({a,i})).filter(o=>o.a.parent==null);
  const n=mains.length, R=Math.min(W*.30,H*.40);
  mains.forEach((o,k)=>{
    const t=(k+.5)/Math.max(n,1), ang=k*2.39996+.3, rad=R*(.47+.53*Math.sqrt(t));
    setN(o.i,core.x+Math.cos(ang)*rad,core.y+Math.sin(ang)*rad*.72);
  });
  agents.forEach((a,i)=>{if(a.parent!=null){
    const p=neurons[a.parent]||core,sibs=agents.filter(b=>b.parent===a.parent),k=sibs.indexOf(a),base=Math.atan2(p.y-core.y,p.x-core.x),ang=base+(k-(sibs.length-1)/2)*.7;
    setN(i,p.x+Math.cos(ang)*40,p.y+Math.sin(ang)*34);
  }});
}
function setN(i,x,y){if(!neurons[i])neurons[i]={x:core.x,y:core.y,tx:x,ty:y,fire:0,ph:Math.random()*6};else{neurons[i].tx=x;neurons[i].ty=y}}
function fire(i){const n=i<0?core:neurons[i];if(!n)return;n.fire=1;
  if(i>=0){const a=agents[i],src=a.parent!=null?neurons[a.parent]:core;if(src)pulses.push({a:src,b:n,q:n,t:0,sp:.013+Math.random()*.008,c:COL[a.status]||COL.working})}}
function flow(src){flows.push({owner_i:src.owner_i??-1,stage:src.stage||"found",kind:src.kind||"web",t:0,age:0});if(flows.length>120)flows.splice(0,flows.length-120)}
function rgba(c,a){return `rgba(${c[0]},${c[1]},${c[2]},${a})`}
function draw(ts){
  if(!W||!H){requestAnimationFrame(draw);return}
  ctx.clearRect(0,0,W,H);
  ambient.forEach(p=>{p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>1)p.vx*=-1;if(p.y<0||p.y>1)p.vy*=-1});
  for(let i=0;i<ambient.length;i++){const a=ambient[i],ax=a.x*W,ay=a.y*H;
    for(let j=i+1;j<ambient.length;j++){const b=ambient[j],dx=ax-b.x*W,dy=ay-b.y*H,d=dx*dx+dy*dy;if(d<4400){ctx.strokeStyle=`rgba(80,110,190,${.08*(1-d/4400)})`;ctx.lineWidth=.6;ctx.beginPath();ctx.moveTo(ax,ay);ctx.lineTo(b.x*W,b.y*H);ctx.stroke()}}
    ctx.fillStyle="rgba(120,150,230,.32)";ctx.beginPath();ctx.arc(ax,ay,a.r,0,Math.PI*2);ctx.fill()}
  if(running&&Math.random()<.055){const a=ambient[Math.floor(Math.random()*ambient.length)],b=ambient[Math.floor(Math.random()*ambient.length)];pulses.push({a:{x:a.x*W,y:a.y*H},b:{x:b.x*W,y:b.y*H},t:0,sp:.018,c:[110,140,245],amb:1})}
  neurons.forEach(n=>{if(!n)return;n.x+=(n.tx-n.x)*.065;n.y+=(n.ty-n.y)*.065;n.fire*=.95});core.fire*=.95;
  // visible internet-to-queen trunk, with a scan beam while queries are running
  const searching=SEARCHES.some(q=>q.stage==="searching");
  ctx.save();ctx.beginPath();ctx.moveTo(gate.x+18,gate.y);ctx.quadraticCurveTo((gate.x+core.x)/2,gate.y-38,core.x-29,core.y);
  ctx.strokeStyle=searching?"rgba(62,230,255,.55)":"rgba(62,230,255,.18)";ctx.lineWidth=searching?2:1;ctx.setLineDash(searching?[5,7]:[]);ctx.lineDashOffset=-ts/55;ctx.stroke();ctx.restore();
  // ordinary agent synapses
  agents.forEach((a,i)=>{const n=neurons[i];if(!n)return;const src=a.parent!=null?neurons[a.parent]:core;if(!src)return;
    const c=a.parent!=null?[192,92,255]:(COL[a.status]||COL.queued),act=a.status==="working"||a.status==="researching";
    const mx=(src.x+n.x)/2+(n.y-src.y)*.15,my=(src.y+n.y)/2-(n.x-src.x)*.15;
    ctx.strokeStyle=rgba(c,act?.48:.19);ctx.lineWidth=act?1.7:1;ctx.beginPath();ctx.moveTo(src.x,src.y);ctx.quadraticCurveTo(mx,my,n.x,n.y);ctx.stroke();n.mx=mx;n.my=my;
  });
  for(let i=0;i<agents.length;i++)for(let j=i+1;j<agents.length;j++){const a=neurons[i],b=neurons[j];if(!a||!b)continue;const d=Math.hypot(a.x-b.x,a.y-b.y);if(d<100){ctx.strokeStyle=`rgba(90,120,200,${.10*(1-d/100)})`;ctx.lineWidth=.7;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}}
  // evidence packets travel from the public-web gateway to the agent that requested them
  flows=flows.filter(f=>f.age<1.35);flows.forEach(f=>{f.age+=.012;f.t=Math.min(1,f.t+.018);const target=f.owner_i>=0&&neurons[f.owner_i]?neurons[f.owner_i]:core;
    const sx=gate.x+17,sy=gate.y,tx=target.x,ty=target.y,mx=(sx+tx)/2,my=(sy+ty)/2-30,u=f.t;
    const x=(1-u)*(1-u)*sx+2*(1-u)*u*mx+u*u*tx,y=(1-u)*(1-u)*sy+2*(1-u)*u*my+u*u*ty;
    const c=f.stage==="read"?[92,255,157]:f.stage==="read_error"?[255,92,122]:[62,230,255];
    ctx.strokeStyle=rgba(c,.16);ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(sx,sy);ctx.quadraticCurveTo(mx,my,tx,ty);ctx.stroke();
    const gl=ctx.createRadialGradient(x,y,0,x,y,10);gl.addColorStop(0,rgba(c,.95));gl.addColorStop(1,rgba(c,0));ctx.fillStyle=gl;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.fill();
  });
  pulses=pulses.filter(p=>p.t<1);pulses.forEach(p=>{p.t+=p.sp;if(p.t<0)return;const t=p.t;let x,y;
    if(p.q&&p.q.mx!=null){const u=1-t;x=u*u*p.a.x+2*u*t*p.q.mx+t*t*p.b.x;y=u*u*p.a.y+2*u*t*p.q.my+t*t*p.b.y}
    else{x=p.a.x+(p.b.x-p.a.x)*t;y=p.a.y+(p.b.y-p.a.y)*t}
    const g=ctx.createRadialGradient(x,y,0,x,y,p.amb?5:9);g.addColorStop(0,rgba(p.c,.95));g.addColorStop(1,rgba(p.c,0));ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,p.amb?5:9,0,Math.PI*2);ctx.fill()});
  agents.forEach((a,i)=>{if((a.status==="working"||a.status==="researching")&&Math.random()<.025)fire(i)});
  // rotating public-web gateway and scanning rings
  const pulse=4+Math.sin(ts/260)*2+(searching?5:0),gx=gate.x,gy=gate.y;
  for(let r=0;r<3;r++){ctx.beginPath();ctx.arc(gx,gy,18+r*8+((ts/35+r*14)%9),0,Math.PI*2);ctx.strokeStyle=`rgba(62,230,255,${.21-r*.045})`;ctx.lineWidth=1;ctx.stroke()}
  ctx.save();ctx.translate(gx,gy);ctx.rotate(ts/2200);ctx.strokeStyle="rgba(62,230,255,.76)";ctx.lineWidth=1.2;ctx.beginPath();ctx.ellipse(0,0,24+pulse,9+pulse*.25,.55,0,Math.PI*2);ctx.stroke();ctx.restore();
  const gg=ctx.createRadialGradient(gx,gy,0,gx,gy,36);gg.addColorStop(0,"rgba(62,230,255,.30)");gg.addColorStop(1,"rgba(62,230,255,0)");ctx.fillStyle=gg;ctx.beginPath();ctx.arc(gx,gy,36,0,Math.PI*2);ctx.fill();
  ctx.fillStyle="#0a1721";ctx.strokeStyle="rgba(62,230,255,.95)";ctx.lineWidth=2;ctx.beginPath();ctx.arc(gx,gy,15,0,Math.PI*2);ctx.fill();ctx.stroke();
  ctx.font="17px system-ui";ctx.textAlign="center";ctx.textBaseline="middle";ctx.fillText("🌐",gx,gy+1);ctx.font="9px system-ui";ctx.fillStyle="#81bfd2";ctx.fillText("PUBLIC WEB",gx,gy+46);ctx.fillText("+ OFFICIAL DATA",gx,gy+58);
  // queen core
  const cr=27+Math.sin(ts/500)*2+core.fire*8;let g=ctx.createRadialGradient(core.x,core.y,0,core.x,core.y,cr*3.2);g.addColorStop(0,"rgba(255,183,43,.42)");g.addColorStop(1,"rgba(255,183,43,0)");ctx.fillStyle=g;ctx.beginPath();ctx.arc(core.x,core.y,cr*3.2,0,Math.PI*2);ctx.fill();
  ctx.strokeStyle="rgba(255,183,43,.92)";ctx.lineWidth=2;ctx.beginPath();for(let k=0;k<6;k++){const an=Math.PI/3*k+Math.PI/6+ts/4000;ctx.lineTo(core.x+Math.cos(an)*cr,core.y+Math.sin(an)*cr)}ctx.closePath();ctx.fillStyle="#1a1206";ctx.fill();ctx.stroke();
  ctx.font="20px system-ui";ctx.fillStyle="#fff";ctx.fillText({idle:"🧠",plan:"👑",work:"👑",critic:"🔍",refine:"♻️",synth:"🧬",verify:"🔬",done:"✅"}[phaseName]||"🧠",core.x,core.y+1);
  // live agents and nested helper clusters
  const R0=agents.length>30?8:agents.length>14?11:15;
  agents.forEach((a,i)=>{const n=neurons[i];if(!n)return;const c=a.parent!=null&&a.status==="queued"?[192,92,255]:(COL[a.status]||COL.queued);
    const r=(a.parent!=null?R0*.68:R0)+n.fire*5+((a.status==="working"||a.status==="researching")?Math.sin(ts/200+n.ph)*1.3:0);
    const ng=ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,r*2.5);ng.addColorStop(0,rgba(c,.48+n.fire*.4));ng.addColorStop(1,rgba(c,0));ctx.fillStyle=ng;ctx.beginPath();ctx.arc(n.x,n.y,r*2.5,0,Math.PI*2);ctx.fill();
    ctx.fillStyle="#0b1020";ctx.strokeStyle=rgba(c,.95);ctx.lineWidth=2;ctx.beginPath();ctx.arc(n.x,n.y,r,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.font=`${Math.round(r*1.05)}px system-ui`;ctx.fillStyle="#fff";ctx.fillText(a.emoji||"🐝",n.x,n.y+1);
    if(agents.length<=14&&a.parent==null){ctx.font="10px system-ui";ctx.fillStyle="#aab6d8";ctx.fillText(a.name.slice(0,14),n.x,n.y+r+11)}});
  requestAnimationFrame(draw);
}
