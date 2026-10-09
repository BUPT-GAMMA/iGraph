"""Generate the bundled novel graph with a server-configured provider; never bundle credentials or source text."""
import argparse
import shutil
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph_engine.construction.agent import TabularConstructionSubAgent, _parse_typed_semantic_response, _parse_typed_entities
from graph_engine.construction.llm import ToolCallingLLM
from graph_data_service.construction import to_graph_data
from graph_data_service.dataset_registry import DatasetMeta
import numpy as np

parser = argparse.ArgumentParser(description="使用完整中文小说生成可部署人物关系预置图（支持断点复用）。")
parser.add_argument("source", type=Path)
parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1]/"graph_engine/resources/tianlongbabu")
parser.add_argument("--work-dir", type=Path, default=Path("output/novel-preset-build"))
args = parser.parse_args()
from dotenv import load_dotenv
load_dotenv()
root = args.work_dir.resolve()
root.mkdir(parents=True, exist_ok=True)
source = args.source.resolve()
cache = root / 'chunks'
cache.mkdir(exist_ok=True)
class CachedLLM(ToolCallingLLM):
    def __init__(self):
        super().__init__(model='deepseek-v4-pro', timeout=600)
        self.cache_hits = 0
        self.guard = threading.Lock()
    def complete_text(self, messages, *, timeout=None, max_tokens=8192):
        key = hashlib.sha256(json.dumps([self.model, messages, max_tokens], ensure_ascii=False).encode()).hexdigest()
        path = cache / (key + '.json')
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            parsed = _parse_typed_semantic_response(cached.get('content', ''), ['人物'])
            if parsed is None or parsed:
                with self.guard:
                    self.cache_hits += 1
                return cached
        response = super().complete_text(messages, timeout=timeout, max_tokens=max_tokens)
        text = response.get('content', '')
        parsed = _parse_typed_semantic_response(text, ['人物'])
        if parsed == []:
            return response
        chunk = messages[-1]['content'].split('<<<TEXT\n', 1)[1].rsplit('\nTEXT>>>', 1)[0]
        _parse_typed_entities(text, ['人物'], chunk)
        path.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
        return response
    def audit_metadata(self):
        return {**super().audit_metadata(), 'checkpoint_cache_hits': self.cache_hits}

llm = CachedLLM()
result = TabularConstructionSubAgent(llm_factory=lambda config: llm).run(source, {
    'txt_mode': 'entities', 'mode': 'llm', 'construction_model': 'deepseek-v4-pro',
    'entity_types': ['人物'], 'text_chunk_tokens': 12000, 'text_overlap_tokens': 300,
    'text_parallelism': 3, 'llm_timeout_seconds': 600,
    'experience_enabled': False, 'text_refine_enabled': False,
    'domain': '《天龙八部》人物关系图。抽取原文明示姓名的人物和人物之间的亲属、师徒、结义、朋友、敌对、恋爱等关系；保留无关系的具名人物，不把门派、地点、武功、泛指称谓作为人物，不补充原文以外知识。',
}, report_path=root/'report.json', progress_path=root/'progress.json')
graph = result.graph
(root/'graph.json').write_text(json.dumps(graph, ensure_ascii=False), encoding='utf-8')
asset = root/'tianlongbabu-characters.npz'
data=to_graph_data({'dataset_id':'ds_tianlongbabu_v1','revision':1,'graph':graph})
plain=root/'graph.npz'
data.save(str(plain))
with np.load(plain, allow_pickle=True) as doc:
    np.savez_compressed(asset, **{key:doc[key] for key in doc.files})
from datetime import datetime, timezone
now=datetime.now(timezone.utc).isoformat(timespec='seconds')
meta=DatasetMeta(dataset_id='ds_tianlongbabu_v1',name='天龙八部人物关系（预置图）',
 description='完整《天龙八部》文本经 deepseek-v4-pro 分块抽取的人物关系图；人物和关系保留来源分块，支持邻居、路径、中心性与社区分析。模型抽取关系需结合原文复核。',
 source_type='novel_preset',node_count=data.num_nodes,edge_count=data.num_edges,is_directed=True,
 created_at=now,updated_at=now,versions=['v001'],edge_entry_count=data.num_edge_entries,
 unique_edge_count=data.num_unique_edges,num_unique_directed_edges=data.num_unique_directed_edges,
 num_unique_undirected_edges=data.num_unique_undirected_edges,semantic_type=data.semantic_type,
 storage_type=data.storage_type,is_symmetric=data.is_symmetric,has_self_loops=data.has_self_loops,
 has_duplicate_entries=data.has_duplicate_entries)
manifest={'version':1,'filename':asset.name,'dataset':meta.to_dict(),
 'sources':{'天龙八部.txt':{'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'bytes':source.stat().st_size}},
 'sha256':hashlib.sha256(asset.read_bytes()).hexdigest(),
 'construction':{'model':result.report.get('model'),'semantic_extraction':result.report.get('semantic_extraction'),
                 'llm_api':result.report.get('llm_api')}}
(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
args.output.mkdir(parents=True, exist_ok=True)
shutil.copyfile(asset, args.output/asset.name)
shutil.copyfile(root/'manifest.json', args.output/'manifest.json')
print(json.dumps({'status':'ready','nodes':data.num_nodes,'edges':data.num_edges,'model':llm.model,'cache_hits':llm.cache_hits},ensure_ascii=False),flush=True)
