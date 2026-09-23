"""Host-owned identity and query boundaries; model IDs are never search terms."""
from copy import deepcopy
import re

from .schemas import ResearchSpec
from deepresearch_agent.retrieval.web_utils import normalize_url


def needs_discovery(spec):
    if not spec.get('items'):
        return True
    evidence_scope = ' '.join([spec.get('title', ''), spec.get('source_policy', ''),
                              *(f.get('evidence_requirement', '') for f in spec.get('fields', []))])
    academic = bool(re.search(r'论文|文献|arxiv|\bpapers?\b', evidence_scope, re.I))
    return any((academic or re.search(r'待核实|待确认|具体实例|待定|\bTBD\b',
                                     item['name'] + ' ' + item.get('version', ''), re.I))
               and '身份来源：' not in item.get('rationale', '') for item in spec['items'])


def apply_candidates(spec, sources, selections):
    """Select observed titles, never model-invented names, URLs or versions."""
    revised = deepcopy(spec)
    groups = {item['id']: [] for item in spec['items']}
    if not groups:
        groups = {'discovered': []}
    if not isinstance(selections, list):
        raise ValueError('候选筛选必须返回列表')
    seen = set()
    for entry in selections:
        key, index = entry.get('item_id'), entry.get('source_index')
        if key not in groups or type(index) is not int or not 0 <= index < len(sources):
            raise ValueError('候选引用了未检索到的来源或未知分类')
        source = sources[index]
        url = normalize_url(source['url'])
        title = str(source.get('title') or '').strip()
        if not title or not url:
            raise ValueError('候选缺少真实标题或来源')
        if (key, url) in seen:
            continue
        seen.add((key, url))
        groups[key].append(source)
    originals = {item['id']: item for item in spec['items']}
    items, mapping = [], {}
    for key, candidates in groups.items():
        mapping[key] = []
        if not candidates:
            if key in originals:
                items.append(originals[key])  # Keep unresolved categories visible, never silently drop scope.
                mapping[key].append(key)
            continue
        for index, source in enumerate(candidates[:3]):
            item_id = key if index == 0 else f'{key[:60]}_paper{index + 1}'
            mapping[key].append(item_id)
            version = re.search(r'v\d+(?=$|[/?#])', source['url'])
            items.append({'id': item_id, 'name': source['title'],
                'version': version.group() if version else '版本待核实',
                'rationale': originals.get(key, {}).get('rationale', '') + '\n身份来源：' + source['url']})
    revised['items'] = items
    if not spec['items'] and items:
        mapping.update({f'q{i + 1}': [item['id'] for item in items] for i in range(len(spec['questions']))})
    for field in revised['fields']:
        field['applies_to'] = [new for old in field.get('applies_to', []) for new in mapping.get(old, [old])]
    return ResearchSpec.model_validate(revised).model_dump(mode='json')


def search_query(spec, item, fields, proposed='', *, retry=False):
    """Reconstruct a short, domain-anchored query rather than forwarding task instructions."""
    selected = [f for f in fields if f['id'] in proposed or f['label'] in proposed] or fields[:2]
    # Model rewrite can add exact titles/English terminology, but cannot erase scope anchors.
    rewrite = proposed if retry else ''
    for entry in [*spec.get('items', []), *spec.get('fields', [])]:
        rewrite = re.sub(r'(?<![\w-])' + re.escape(entry['id']) + r'(?![\w-])', '', rewrite)
    rewrite = re.sub(r'\b(?:item|fields?|source_index)\b', '', rewrite, flags=re.I)
    parts = [spec.get('title', '')[:100], item['name'][:180],
             ' '.join(f['label'] for f in selected), rewrite[:180]]
    return ' '.join(' '.join(parts).split())[:480]
