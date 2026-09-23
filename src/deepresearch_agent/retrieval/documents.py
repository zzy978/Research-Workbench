"""Paper identity, body checks and source-preserving section passages."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .web_utils import content_hash, normalize_url


FIELD_TERMS = {
    'method': ('method', 'architecture', 'model', '方法', '架构', '机制'),
    'training': ('training', 'pretrain', 'fine-tun', 'implementation', '训练', '适配'),
    'dataset': ('dataset', 'data', 'split', 'benchmark', '数据', '划分'),
    'eval': ('experiment', 'evaluation', 'baseline', 'protocol', '评测', '实验', '基线'),
    'metric': ('result', 'metric', 'mse', 'mae', 'crps', 'table', '指标', '结果'),
}
HEADING = re.compile(r'(?m)^(?:#{1,6}[ \t]+|(?:\d+|[A-Z])(?:\.\d+)*\.?[ \t]+)([^\n]{2,150})[ \t]*$')
PAPER_HOSTS = {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org', 'openreview.net', 'aclanthology.org',
               'proceedings.mlr.press', 'proceedings.neurips.cc', 'dl.acm.org', 'ieeexplore.ieee.org'}


def is_paper_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    return host in PAPER_HOSTS or host.endswith('.arxiv.org') or parts.path.lower().endswith('.pdf')


def is_landing_page(url):
    parts = urlsplit(url)
    return ('arxiv.org' in (parts.hostname or '') and parts.path.startswith('/abs/')) or (
        parts.hostname == 'openreview.net' and parts.path == '/forum') or '/doi/abs/' in parts.path


def document_urls(url, text=''):
    """Only derive known same-host document routes, retaining explicit paper versions."""
    parts = urlsplit(normalize_url(url))
    if parts.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
        match = re.match(r'^/(?:abs|html|pdf)/(.+?)(?:\.pdf)?$', parts.path)
        if match:
            paper = match[1]
            return [f'https://{parts.hostname}/html/{paper}', f'https://{parts.hostname}/pdf/{paper}']
    if parts.hostname == 'openreview.net' and parts.path == '/forum':
        paper = parse_qs(parts.query).get('id', [''])[0]
        if re.fullmatch(r'[\w-]+', paper):
            return [f'https://openreview.net/pdf?id={paper}']
    # Publisher PDF routes differ; follow actual links instead of guessing an endpoint.
    pdfs = []
    for target in re.findall(r'\]\(([^\s)]+)', text):
        candidate = urljoin(url, target)
        path = urlsplit(candidate).path.lower()
        if urlsplit(candidate).scheme in {'http', 'https'} and (path.endswith('.pdf') or '/pdf/' in path):
            pdfs.append(normalize_url(candidate))
    return list(dict.fromkeys(pdfs[:2] + [normalize_url(url)]))


def has_paper_body(text):
    headings = [m.group(1).lower() for m in HEADING.finditer(text)]
    body_sections = [h for h in headings if re.search(
        r'method|model|architect|experiment|result|data|evaluation|conclusion|discussion|方法|实验|结果|数据|结论', h)]
    return len(text) >= 1000 and len(body_sections) >= 2


def field_terms(fields):
    labels = ' '.join(f.get('id', '') + ' ' + f.get('label', '') + ' ' + f.get('description', '') for f in fields).lower()
    terms = set(re.findall(r'[a-z][a-z-]{3,}', labels))
    for key, synonyms in FIELD_TERMS.items():
        if key in labels or any(word in labels for word in synonyms):
            terms.update(synonyms)
    return terms


def passage_score(section, text, fields):
    terms = field_terms(fields)
    return sum(5 * (term in section.lower()) + min(2, text.lower().count(term)) for term in terms)


def section_passages(text, fields):
    headings = [h for h in HEADING.finditer(text)
                if not re.search(r'Published as|arXiv:|^[\d. ]+$', h.group(1))
                and len(re.findall(r'\b\d+\.\d+\b', h.group(1))) < 3]
    spans = [('正文', 0, headings[0].start() if headings else len(text))]
    spans.extend((h.group(1).strip(), h.start(), headings[i + 1].start() if i + 1 < len(headings) else len(text))
                 for i, h in enumerate(headings))
    passages = []
    for heading, start, end in spans:
        for offset in range(start, end, 3200):
            chunk = text[offset:min(end, offset + 3500)].strip()
            if chunk:
                passages.append({'section': heading, 'text': chunk, 'offset': offset})
    # Select per field so a long introduction cannot hide experiments/results at the end.
    selected = {}
    for field in fields or [{}]:
        ranked = sorted(passages, key=lambda p: passage_score(p['section'], p['text'], [field]), reverse=True)
        for passage in ranked[:2]:
            selected[passage['offset']] = passage
    return sorted(selected.values(), key=lambda p: p['offset'])


def select_field_passages(results, fields):
    ordinary = [r for r in results if not r.metadata.extra.get('section')]
    passages = [r for r in results if r.metadata.extra.get('section')]
    ranked = sorted(passages, key=lambda r: passage_score(r.metadata.extra['section'], str(r.evidence), fields), reverse=True)
    return ranked[:4] + ordinary[:3]


def document_results(source, url, text, fields, artifact=None):
    output = []
    for passage in section_passages(text, fields):
        result = source.model_copy(deep=True)
        result.evidence = passage['text']
        result.metadata.source_id = result.metadata.url = url
        result.metadata.domain = urlsplit(url).hostname
        result.metadata.content_hash = content_hash(result.evidence)
        result.metadata.extra.update(access='full_text', body_available=True, content_truncated=False,
            section=passage['section'], source_offset=passage['offset'], document_hash=content_hash(text),
            landing_url=source.metadata.source_id, document_excerpt=True)
        # IDs must distinguish passages before the ledger assigns run-scoped IDs.
        result.result_id = 'doc_' + content_hash(url + '\n' + result.evidence)[:24]
        if artifact:
            result.metadata.extra.update(artifact)
        output.append(result)
    return output
