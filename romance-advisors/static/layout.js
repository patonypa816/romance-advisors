export const DEFAULT_LAYOUT = {top:336,right:48,bottom:312,left:48,avatarX:552,avatarY:360,avatarWidth:480};
const KEY='romance-advisors:layout:v1';
export function loadLayout() { try {return normalizeLayout({...DEFAULT_LAYOUT,...JSON.parse(localStorage.getItem(KEY)||'{}')});} catch {return {...DEFAULT_LAYOUT};} }
export function normalizeLayout(raw) {
  if(!raw||typeof raw!=='object'||Array.isArray(raw)||Object.keys(raw).some(key=>!Object.hasOwn(DEFAULT_LAYOUT,key)))throw new Error('invalid_layout');
  const numeric = (key,min,max)=>{const value=Object.hasOwn(raw,key)?raw[key]:DEFAULT_LAYOUT[key];if(typeof value!=='number'||!Number.isFinite(value))throw new Error('invalid_layout');return Math.max(min,Math.min(max,value));};
  const top=numeric('top',320,380), left=numeric('left',0,120), right=numeric('right',0,120);
  // The undistorted reference leaves room down to 1640 inside the 1920 canvas.
  // Keep a 304px two-line poll and place standings beneath the portrait.
  const bottom=numeric('bottom',280,400);
  const bottomEdge=1920-bottom;
  const phoneWidth=480, rightColumnX=left+phoneWidth+24;
  const rightColumnWidth=1080-right-rightColumnX;
  const avatarY=numeric('avatarY',top,440);
  const maxWidth=Math.min(rightColumnWidth,(bottomEdge-avatarY-608)*3/4);
  const avatarWidth=numeric('avatarWidth',200,maxWidth);
  const avatarX=numeric('avatarX',rightColumnX,1080-right-avatarWidth);
  return {top,right,bottom,left,avatarX,avatarY,avatarWidth};
}
export function saveLayout(raw) {const value=normalizeLayout(raw);localStorage.setItem(KEY,JSON.stringify(value));return value;}
export function layoutRects(layout) {
  const bottomEdge=1920-layout.bottom, safeWidth=1080-layout.left-layout.right;
  const phoneWidth=480, upperBottom=bottomEdge-328;
  const avatar={x:layout.avatarX,y:layout.avatarY,w:layout.avatarWidth,h:layout.avatarWidth*4/3};
  const phone={x:layout.left,y:layout.top,w:phoneWidth,h:upperBottom-layout.top};
  const pollY=upperBottom+24;
  const boardsX=layout.left+phoneWidth+24;
  return {
    safe:{x:layout.left,y:layout.top,w:safeWidth,h:bottomEdge-layout.top}, avatar,
    phone,
    poll:{x:layout.left,y:pollY,w:safeWidth,h:bottomEdge-pollY},
    boards:{x:boardsX,y:upperBottom-256,w:1080-layout.right-boardsX,h:256}
  };
}
export function observeLayout(callback) {const handler=e=>{if(e.key===KEY)callback(loadLayout());};window.addEventListener('storage',handler);return ()=>window.removeEventListener('storage',handler);}
