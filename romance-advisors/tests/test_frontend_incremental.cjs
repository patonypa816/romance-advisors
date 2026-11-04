// Offline protocol and DOM-operation checks. These do not measure browser frames.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const ui=fs.readFileSync(path.join(root,'static/ui.js'),'utf8').replaceAll('export ','');
const context=vm.createContext({URLSearchParams});
vm.runInContext(ui+';globalThis.cache=createOperatorCache();',context);
const cache=context.cache;
const base={session_id:'s',comment_cursor:1,proposal_version:'p1',vote_version:'v1',reset:true,comments:[{id:'a'}],proposals:[{id:'p',selection:{status:'selected',reason:'原结果'}}],votes:[{id:'v'}]};
let merged=cache.merge(base);
assert.equal(merged.comments.length,1);
const firstComments=merged.comments,firstVotes=merged.votes;
merged=cache.merge({...base,reset:false,comments:[],proposals:undefined,votes:undefined});
assert.equal(merged.comments,firstComments);
assert.equal(merged.votes,firstVotes);
assert.equal(merged.comment_update.revision,1);
const firstProposals=merged.proposals;
merged=cache.merge({...base,reset:false,comments:[],proposal_reset:false,proposal_version:'p2',proposals:[{id:'p2',text:'另一作者'}],votes:undefined});
merged=cache.merge({...base,reset:false,comments:[],proposal_reset:false,proposal_version:'p3',proposals:[{id:'p',text:'同一作者更新'}],votes:undefined});
assert.equal(merged.proposals,firstProposals);
assert.deepEqual(Array.from(merged.proposals,p=>p.id),['p','p2']);
assert.equal(merged.proposals[0].text,'同一作者更新');
assert.equal(merged.proposals[0].selection,undefined); // Same-ID replacement does not retain a prior result.
assert.equal(merged.proposals[1].text,'另一作者');
merged=cache.merge({...base,reset:false,comments:[],proposals:undefined,vote_version:'v2',vote_reset:false,votes:[{id:'v2',author_id:'other',option_id:'one'}]});
assert.equal(merged.votes,firstVotes);
assert.equal(merged.votes.length,2);
merged=cache.merge({...base,reset:false,comments:[],proposals:undefined,vote_version:'v3',vote_reset:false,votes:[{id:'v',author_id:'a',option_id:'two'}]});
assert.deepEqual(Array.from(merged.votes,v=>v.id),['v','v2']);
assert.equal(merged.votes[0].option_id,'two');
assert.equal(merged.votes[1].option_id,'one');
// Resetting just the comment cursor must not discard unchanged votes.
merged=cache.merge({...base,reset:true,comments:[{id:'a'}],proposals:undefined,votes:undefined,vote_version:'v3'});
assert.equal(merged.votes.length,2);
// A failed fetch never calls merge, so query remains at the confirmed cursor.
assert.equal(new URLSearchParams(cache.query()).get('comment_cursor'),'1');
merged=cache.merge({...base,reset:false,comment_cursor:3,comments:[{id:'b'},{id:'c'}],proposals:undefined,votes:undefined});
assert.deepEqual(Array.from(merged.comments,c=>c.id),['a','b','c']);
merged=cache.merge({...base,session_id:'new',comments:[],reset:true,comment_cursor:0,proposals:[],votes:[]});
assert.equal(merged.comments.length,0);
assert.equal(merged.votes.length,0);

const source=fs.readFileSync(path.join(root,'static/workbench.js'),'utf8');
const functions=source.slice(source.indexOf('function renderProposals(){'),source.indexOf("let performanceSignature='';"));
let created=0,replaces=0;
class Node {
  constructor(tag='div',className='',text=''){this.tag=tag;this.className=className;this.textContent=text;this.children=[];this.scrollTop=10;this.scrollHeight=1000;this.clientHeight=200;this.value='all';}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){replaces++;this.children=[];this.append(...nodes);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
  setAttribute(){}
  addEventListener(){}
  querySelector(selector){return this.children.find(n=>n.className===selector.slice(1))||null;}
  get options(){return this.children;}
}
const nodes=new Map(),getNode=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
const voteState={round:{id:'r',phase:'VOTING',options:[{id:'one',number:1},{id:'two',number:2}]},votes:[{id:'v',author_id:'a',round_id:'r',option_id:'one'}],vote_version:'v1',input_update:{votes:{reset:true,changed:[{id:'v',author_id:'a',round_id:'r',option_id:'one'}]}}};
const voteContext=vm.createContext({state:voteState,Map,JSON,$:getNode,el:(...args)=>new Node(...args),participant:()=>({id:'a',nickname:'模拟'}),displayName:(id,name)=>name});
vm.runInContext("let selectedOption=null,connected=true,submitting=false,commentSending=false,batchSending=false,currentParticipant='a',indexedVoteVersion=null,votesByAuthor=new Map(),voteSignature='',batchFeedback='';"+source.slice(source.indexOf('function renderVoteOptions(){'),source.indexOf('function renderProposals(){'))+';renderVoteOptions();',voteContext);
assert.match(getNode('vote-feedback').textContent,/选项 1/);
voteState.votes[0]={...voteState.votes[0],option_id:'two'}; // Same array identity.
voteState.vote_version='v2';voteState.input_update={votes:{reset:false,changed:[voteState.votes[0]]}};
vm.runInContext('renderVoteOptions();',voteContext);
assert.match(getNode('vote-feedback').textContent,/选项 2/);
const comment=(id,round_id='r')=>({id,round_id,author_id:'a',nickname:'同名',text:id,received_at:'2026-11-04T00:00:00Z',result:'candidate',reason:'已收到'});
const state={session:{id:'s'},round:{id:'r'},comment_rounds:[],comments:Array.from({length:5000},(_,i)=>comment('c'+i)),proposals:Array.from({length:5000},(_,i)=>({id:'p'+i,author_id:'a',nickname:'同名',text:'建议'+i})),proposal_version:'p1',comment_update:{revision:1,reset:true}};
const dom=vm.createContext({state,JSON,Date,Number,Map,Set,PHASES:{},$ :getNode,el:(...args)=>{created++;return new Node(...args);},displayName:(id,name)=>name});
vm.runInContext("let commentsSignature='',roundFilterSignature='[]',proposalsSignature='',renderedCommentRevision=-1,renderedCommentCursor=0,renderedNameRevision=-1,renderedProposalVersion=null,participantRevision=0;const commentRows=new Map(),proposalRows=new Map();"+functions,dom);
vm.runInContext('renderProposals();renderComments();',dom);
assert.equal(getNode('comment-list').children.length,5000);
created=0;replaces=0;
vm.runInContext('renderProposals();renderComments();',dom);
assert.equal(created,0);assert.equal(replaces,0);
state.comments.push(comment('new'));state.comment_update={revision:2,reset:false};
vm.runInContext('renderComments();',dom);
assert.equal(created,9);assert.equal(replaces,0);
assert.equal(getNode('comment-list').children.length,5001);
assert.equal(getNode('comment-list').scrollTop,10); // Preserve a scrolled-back reader.
created=0;state.proposals[0]={...state.proposals[0],text:'同一作者的更新建议'};state.proposal_version='p2';
vm.runInContext('renderProposals();',dom);
assert.equal(created,0);assert.equal(getNode('proposal-list').children[0].children[1].textContent,'同一作者的更新建议');
const selectionRow=()=>getNode('proposal-list').children[0].children[2];
assert.equal(selectionRow().children[0].textContent,'状态未记录');
state.proposals[0]={...state.proposals[0],selection:{status:'pending',reason:'等待编选。'}};state.proposal_version='p3';
vm.runInContext('renderProposals();',dom);
assert.equal(selectionRow().children[0].textContent,'待编选');
state.proposals[0]={...state.proposals[0],selection:{status:'not_selected',reason:'理由一'}};state.proposal_version='p4';
vm.runInContext('renderProposals();',dom);
assert.equal(selectionRow().children[0].textContent,'未入选');assert.equal(selectionRow().children[1].textContent,'理由一');
state.proposals[0]={...state.proposals[0],selection:{status:'not_selected',reason:'理由二'}};state.proposal_version='p5';
vm.runInContext('renderProposals();',dom);
assert.equal(selectionRow().children[1].textContent,'理由二');
state.proposals[0]={...state.proposals[0],selection:{status:'selected',reason:'已入选投票选项。'}};state.proposal_version='p6';
vm.runInContext('renderProposals();',dom);
assert.equal(selectionRow().children[0].textContent,'已入选');assert.equal(selectionRow().children[1].textContent,'已入选投票选项。');
state.proposals[0]={id:'p0',author_id:'a',nickname:'同名',text:'修改后的建议'};state.proposal_version='p7';
vm.runInContext('renderProposals();',dom);
assert.equal(selectionRow().children[0].textContent,'状态未记录');assert.equal(selectionRow().children[1].textContent,'本轮编选信息未记录。');
for(const status of ['duplicate','not_submitted']){
  state.proposals[0]={...state.proposals[0],selection:{status,reason:'系统原因'}};state.proposal_version=status;
  vm.runInContext('renderProposals();',dom);assert.equal(selectionRow().children[0].textContent,'未入选');assert.equal(selectionRow().children[1].textContent,'系统原因');
}
dom.displayName=(id,name)=>name+' · 2';
vm.runInContext('participantRevision++;renderComments();',dom);
assert.equal(created,0);
assert.equal(getNode('comment-list').children[0].children[0].children[0].textContent,'同名 · 2');
getNode('comment-filter').value='current';state.round={id:'r2'};state.comments.push(comment('next-round','r2'));state.comment_update={revision:3,reset:false};
vm.runInContext('renderComments();',dom);
assert.equal(getNode('comment-list').children.length,1);
assert.equal(getNode('comment-list').children[0].children[2].textContent,'next-round');
getNode('comment-filter').value='all';vm.runInContext('renderComments();',dom);
assert.equal(getNode('comment-list').children.length,5002);
state.session={id:'s2'};state.comments=[comment('fresh','r2')];state.comment_update={revision:4,reset:true};
vm.runInContext('renderComments();',dom);
assert.equal(getNode('comment-list').children.length,1);
console.log(JSON.stringify({status:'passed',existing_comments:5000,unchanged_created_nodes:0,one_new_comment_created_nodes:9,one_new_comment_replace_children_calls:0,checks:['cursor gap recovery','session reset','vote cache','vote delta preserves other authors','same-array vote switch','comment-only reset preserves inputs','same-ID proposal update','selection status and reason deltas','name refresh','filter and round rebuild','scroll preservation']}));
