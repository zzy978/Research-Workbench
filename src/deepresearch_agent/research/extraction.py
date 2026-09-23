"""Single-cell model contract; identity and evidence metadata belong to the host."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .failures import CellValidationError


class ExtractedCitation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    evidence_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)


class ExtractedContent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['supported', 'inference', 'conflict', 'not_found', 'not_applicable']
    value: Any
    reason: str
    citations: list[ExtractedCitation]


def validation_error(message, path, expected, actual):
    return CellValidationError(message, issues=[{'path': path, 'expected': expected, 'actual': actual}])


def parse_content(payload):
    try:
        return ExtractedContent.model_validate(payload).model_dump()
    except ValidationError as exc:
        issues = []
        schema = ExtractedContent.model_json_schema()
        for error in exc.errors(include_context=False):
            path = error['loc']
            expected = schema
            for part in path:
                if isinstance(part, int):
                    expected = expected.get('items', {})
                else:
                    expected = expected.get('properties', {}).get(part, {})
                if '$ref' in expected:
                    expected = schema['$defs'][expected['$ref'].rsplit('/', 1)[-1]]
            if error['type'] == 'extra_forbidden':
                expected = '不返回此字段；仅返回 Schema 规定的内容字段'
            issues.append({'path': '.'.join(map(str, path)) or '$',
                           'expected': expected or error['msg'],
                           'actual': {'missing': True} if error['type'] == 'missing' else error['input']})
        raise CellValidationError('抽取结果不符合单元格内容结构', issues=issues) from exc


def require_available_body(field, results):
    if (field.get('required_access') == 'full_text'
            and any(r.metadata.extra.get('full_text_failure') for r in results)
            and not any(r.metadata.extra.get('access') == 'full_text' for r in results)):
        raise validation_error('已定位相关来源，但正文获取失败；不能据此认定论文未报告该字段',
                               'sources.access', '至少一个已获取的 full_text 来源',
                               [r.metadata.extra.get('access', 'snippet') for r in results])


def validate_evidence(cell, field, results):
    require_available_body(field, results)
    sources = {r.result_id: r for r in results}
    for index, citation in enumerate(cell['citations']):
        path = f'citations.{index}'
        source = sources.get(citation['evidence_id'])
        if source is None:
            raise validation_error('引用了未提供的证据 ID', path+'.evidence_id', list(sources), citation['evidence_id'])
        locator = citation['locator'].strip()
        if not locator or locator not in str(source.evidence):
            raise validation_error('证据定位不在实际读取的来源中', path+'.locator',
                {'evidence_id': source.result_id, 'rule': '逐字摘自本次所给该来源的非空连续短原文'},
                citation['locator'])
        access = source.metadata.extra.get('access', 'snippet')
        if field.get('required_access') == 'full_text' and access != 'full_text':
            raise validation_error('已取得相关片段，但缺少本字段要求的正文证据',
                                   path+'.access', 'full_text', access)
        citation.update(locator=locator, content_hash=source.metadata.content_hash, access=access)
    if cell['status'] in {'supported', 'inference', 'conflict'}:
        if cell['value'] is None or cell['value'] == '' or not cell['citations']:
            raise validation_error('结论需要实际取值和证据引用', 'conclusion',
                '非空 value 和至少一条有效引用', {'value': cell['value'], 'citations': cell['citations']})
    if cell['status'] in {'not_found', 'not_applicable'} and cell['value'] is not None:
        raise validation_error('未找到或不适用的字段不能填写肯定结果', 'value', None, cell['value'])
    if cell['status'] == 'conflict':
        count = len({sources[c['evidence_id']].metadata.source_id for c in cell['citations']})
        if count < 2:
            raise validation_error('冲突需要至少两个独立来源', 'citations.independent_sources', '>=2', count)
    return cell


def bind_content(payload, item, field, results):
    content = parse_content(payload)
    return validate_evidence({**content, 'item_id': item['id'], 'field_id': field['id']}, field, results)


def validate_bound_cell(cells, item, field, results):
    """Also guard custom model adapters; never silently relabel their output."""
    require_available_body(field, results)
    if not isinstance(cells, list):
        raise validation_error('抽取结果必须完整返回当前字段，不能遗漏或重复', 'cells', '单元素列表', cells)
    if len(cells) != 1:
        raise validation_error('抽取结果必须完整返回当前字段，不能遗漏或重复', 'cells.length', 1, len(cells))
    if not isinstance(cells[0], dict):
        raise validation_error('抽取结果必须是单元格对象', 'cells.0', 'object', cells[0])
    cell = cells[0]
    for key, expected in [('item_id', item['id']), ('field_id', field['id'])]:
        if cell.get(key) != expected:
            raise validation_error('抽取结果试图改变已批准的对象或字段', key, expected,
                                   cell.get(key, {'missing': True}))
    content = {k: v for k, v in cell.items() if k not in {'item_id', 'field_id'}}
    if isinstance(content.get('citations'), list):
        content['citations'] = [{k: v for k, v in c.items() if k not in {'content_hash', 'access'}}
                                if isinstance(c, dict) else c for c in content['citations']]
    return bind_content(content, item, field, results)
