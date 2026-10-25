"""提取文字型 PDF 的逐页内容；扫描件需先经 OCR 后再上传。"""
from pathlib import Path


def parse_pdf(path, max_pages=100):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf，请先重新同步项目依赖") from exc

    reader = PdfReader(Path(path))
    if reader.is_encrypted:
        raise ValueError("PDF 已加密，无法读取；请上传未加密版本")
    if len(reader.pages) > max_pages:
        raise ValueError(f"教材页数超过上限（{max_pages} 页）")

    pages = []
    text_count = 0
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        text_count += len(text)
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        pages.append({
            "slide_number": number,
            "title": first_line[:160] or f"第 {number} 页",
            "body_text": text,
            "speaker_notes": "",
        })
    if text_count < 20:
        raise ValueError("未提取到足够文字。这可能是扫描版 PDF，请先进行 OCR 后再上传")
    return pages
