'use strict';

const localized=(hk,cn,en)=>({'zh-HK':hk,'zh-CN':cn,en});

function frameworkDemoFixture(){
  const definitions=[
    ['online','traditional_online','SERVICEENTRY','service-entry',localized('服務申請受理','服务申请受理','Service request intake')],
    ['client_server','screen_controlled_online','SCREENCONTROL','screen-control',localized('申請覆核決定','申请复核决定','Request review decision')],
    ['batch','batch','NIGHTBATCH','night-batch',localized('夜間申請生效','夜间申请生效','Nightly activation')],
  ];
  return {
    id:'framework-workbench',title:localized('服務申請業務分析','服务申请业务分析','Service request business analysis'),
    description:localized('受理、覆核與夜間生效。','受理、复核与夜间生效。','Intake, review and nightly activation.'),
    default_case:'online',source_path:'fixtures/framework/source',file_count:7,snapshot_id:'sha256:fixture-snapshot',analysis_origin:'synthetic_source_guide',model_called:false,
    programs:definitions.map(([, ,name,file])=>({program_name:name,relative_path:`programs/${file}.cbl`,start_line:2})),
    cases:definitions.map(([id,kind,name,file,title],index)=>({
      id,kind,title,entry_program:name,entry_path:`programs/${file}.cbl`,
      question:localized(`請解釋${title['zh-HK']}。`,`请解释${title['zh-CN']}。`,`Explain ${title.en}.`),
      purpose:localized('說明受理條件、狀態變化與業務結果。','说明受理条件、状态变化与业务结果。','Explain eligibility, status changes and business outcomes.'),
      steps:[{title:localized('判斷申請資格','判断申请资格','Determine request eligibility'),description:localized('讀取申請後檢查狀態。','读取申请后检查状态。','Read the request and check the status.'),evidence_id:`ev_guide_${id}`}],
      evidence:[{evidence_id:`ev_guide_${id}`,title:localized('申請處理','申请处理','Request processing'),relative_path:`programs/${file}.cbl`,start_line:10+index,end_line:11+index,source_text:`           CALL 'REQUESTIO'.\n           DISPLAY '${name}'.`,integrity:'SYNTHETIC_SOURCE',source_sha256:`sha256:${id}-source`}],
      relations:[{source_program:name,target_name:'REQUESTIO',relation_type:'CALLS',status:'source_observed',evidence_id:`ev_guide_${id}`}],
      metadata:[{label:localized('共享上下文','共享上下文','Shared context'),value:localized('申請識別碼與狀態。','申请标识码与状态。','Request identifier and status.')}],
      boundaries:[localized('未提供產生式I/O實作。','未提供生成式I/O实现。','Generated I/O implementation is not provided.')],trace:[],
    })),
  };
}
module.exports={frameworkDemoFixture};
