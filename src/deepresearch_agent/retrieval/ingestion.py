"""Strict private-corpus ingestion: a failed document must not disappear silently."""
from pathlib import Path
import json


def _read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from PyPDF2 import PdfReader
        with path.open("rb") as stream:
            # Unlike the legacy reader, page extraction errors propagate.
            return "\n\n".join(page.extract_text() or "" for page in PdfReader(stream).pages)
    if suffix == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(path)
        blocks = []
        for element in document.element.body:
            if element.tag.endswith("}p"):
                blocks.append(Paragraph(element, document).text)
            elif element.tag.endswith("}tbl"):
                blocks.extend("\t".join(cell.text for cell in row.cells) for row in Table(element, document).rows)
        return "\n".join(blocks)
    if suffix == ".doc":
        from deepresearch_agent.pipelines.ingestion.file_reader import FileReader
        text = FileReader(str(path.parent))._read_doc(str(path))
        if text.startswith("[无法") or text.startswith("[警告"):
            raise ValueError("请将旧版 .doc 转换为 .docx 后重试")
        return text
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Strict decoding: never embed replacement characters from a damaged file.
        import chardet
        encoding = chardet.detect(raw).get("encoding") or "gb18030"
        text = raw.decode(encoding)
    if suffix == ".json":
        json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        import yaml
        yaml.safe_load(text)
    return text


def read_corpus(directory) -> list[tuple[str, str]]:
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError("文档目录不存在")
    supported = {".txt", ".md", ".pdf", ".doc", ".docx", ".csv", ".json", ".yaml", ".yml"}
    expected = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() in supported}
    if not expected:
        raise ValueError("文档目录没有支持的文件")
    documents, failed = [], []
    for relative in sorted(expected):
        try:
            text = _read_document(directory / relative)
            if not text.strip():
                raise ValueError("未提取到文字（扫描文档需先 OCR）")
            documents.append((relative, text))
        except Exception:
            failed.append(relative)
    if failed:
        raise ValueError("下列文档无法提取文字，未发布索引：" + ", ".join(failed))
    return sorted(documents)
