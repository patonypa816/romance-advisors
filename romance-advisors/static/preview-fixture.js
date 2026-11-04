// Static preview portrait: approved evening Vidu asset 1015465797675401216, 1086×1448.
// preview-male-3x4.png is an unchanged local cache copy (SHA-256 2940c9ea0de3b55047d1939102dc1692a8866b389c49e54c4c5a0b928ef28fe8).
// Disposable front-end data only: no API, storage, model or avatar session is involved.
// Keep option summaries within the same concise 8–12-character display contract.
export function createPreviewFixture() {
  return {
    episode:{id:'preview-episode',title:'周末的那场电影',man_name:'林屿',woman_name:'许知夏',score:68,status:'ACTIVE'},
    round:{id:'preview-round',number:3,phase:'VOTING',remaining_seconds:28,options:[
      {id:'preview-option-1',number:1,text:'先问她周末哪天方便',votes:128,author:{nickname:'认真分析每一句话的军师'}},
      {id:'preview-option-2',number:2,text:'不催她先聊喜欢的电影',votes:96,author:{nickname:'今天也在认真出主意'}},
      {id:'preview-option-3',number:3,text:'她有空再约附近咖啡馆',votes:75,author:{nickname:'路过但想帮你牵手成功'}},
      {id:'preview-option-4',number:4,text:'先回应加班再轻松邀约',votes:43,author:{nickname:'恋爱观察员的一本笔记'}}
    ]},
    messages:[
      {id:'preview-msg-1',round_id:'preview-round-1',role:'woman',text:'刚忙完一整天，终于可以回家了。路过上次说的那家面包店，发现他们真的有抹茶可颂。'},
      {id:'preview-msg-2',round_id:'preview-round-1',role:'man',text:'那家店果然没让你失望。我今天也被工作追着跑，听到可颂两个字，突然觉得这一周还有救。'},
      {id:'preview-msg-3',round_id:'preview-round-2',role:'woman',text:'哈哈，那你下次可以试试。不过别去太晚，我今天差点只买到最后一个。最近总是加班，周末想找个舒服的地方坐一坐。'},
      {id:'preview-msg-4',round_id:'preview-round-2',role:'man',text:'记下了，早点去，给可颂留个位置。你上次说想看的那部电影也快上映了，要是周末不累，可以顺路给自己放个小假。'},
      {id:'preview-msg-5',round_id:'preview-round',role:'woman',text:'电影我挺想看的，不过周六可能还要加班。如果周日有空，我想先睡到自然醒，再慢慢出门，不想把一天排得太满。你周末喜欢提前计划，还是看天气临时决定？'}
    ],
    leaderboards:{
      best:[{nickname:'认真分析每一句话的军师（排版示例）',rate:.92,adopted:123}],
      worst:[{nickname:'路过但总把天聊歪的狗头军师（排版示例）',rate:.18,adopted:46}]
    }
  };
}
