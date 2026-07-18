#!/usr/bin/env python3
"""Convert FictionBook 2.0/2.1 (.fb2) and DjVu (.djvu/.djv) files to EPUB 2.

The FB2 converter intentionally uses only Python's standard library. DjVu text
extraction requires the external DjVuLibre command-line tool `djvutxt`.
"""

from __future__ import annotations

import argparse
import base64
import html
import mimetypes
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET


FB2_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
NS = {"fb": FB2_NS, "l": XLINK_NS}
SUPPORTED_EXTENSIONS = {".fb2", ".djvu", ".djv"}


class ConversionError(Exception):
    """Raised when one FB2 file cannot be converted."""


@dataclass
class Chapter:
    title: str
    filename: str
    body_html: str


@dataclass
class ImageAsset:
    media_type: str
    data: bytes
    filename: str


@dataclass
class Book:
    title: str
    authors: List[str]
    language: str
    identifier: str
    annotation: str = ""
    chapters: List[Chapter] = field(default_factory=list)
    images: Dict[str, ImageAsset] = field(default_factory=dict)
    cover_href: Optional[str] = None


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find_text(parent: ET.Element, path: str, default: str = "") -> str:
    found = parent.find(path, NS)
    if found is None or found.text is None:
        return default
    return normalize_space(found.text)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slug_filename(value: str, fallback: str) -> str:
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
    value = "".join(translit.get(ch, translit.get(ch.lower(), ch)) for ch in value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return value.lower() or fallback


def collect_text(element: ET.Element) -> str:
    parts: List[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(collect_text(child))
        if child.tail:
            parts.append(child.tail)
    return normalize_space("".join(parts))


def parse_author(author: ET.Element) -> str:
    first = find_text(author, "fb:first-name")
    middle = find_text(author, "fb:middle-name")
    last = find_text(author, "fb:last-name")
    nickname = find_text(author, "fb:nickname")
    full = normalize_space(" ".join(part for part in [first, middle, last] if part))
    return full or nickname or "Unknown Author"


def parse_images(root: ET.Element) -> Dict[str, ImageAsset]:
    images: Dict[str, ImageAsset] = {}
    used_names: Dict[str, int] = {}
    for binary in root.findall("fb:binary", NS):
        image_id = binary.get("id")
        content_type = binary.get("content-type", "application/octet-stream")
        raw = normalize_space(binary.text or "")
        if not image_id or not raw:
            continue
        try:
            data = base64.b64decode(raw, validate=False)
        except ValueError:
            continue
        ext = mimetypes.guess_extension(content_type) or Path(image_id).suffix or ".bin"
        base = slug_filename(Path(image_id).stem, "image")
        count = used_names.get(base, 0) + 1
        used_names[base] = count
        name = f"{base}{'' if count == 1 else '-' + str(count)}{ext}"
        images[image_id] = ImageAsset(content_type, data, f"images/{name}")
    return images


def get_cover_href(description: ET.Element, images: Dict[str, ImageAsset]) -> Optional[str]:
    image = description.find("fb:title-info/fb:coverpage/fb:image", NS)
    if image is None:
        return None
    href = image.get(f"{{{XLINK_NS}}}href") or image.get("href")
    if not href:
        return None
    image_id = href.lstrip("#")
    asset = images.get(image_id)
    return asset.filename if asset else None


class Fb2HtmlRenderer:
    def __init__(self, images: Dict[str, ImageAsset]) -> None:
        self.images = images

    def render_children_inline(self, element: ET.Element) -> str:
        output = html.escape(element.text or "")
        for child in element:
            output += self.render_inline(child)
            output += html.escape(child.tail or "")
        return output

    def render_inline(self, element: ET.Element) -> str:
        tag = strip_ns(element.tag)
        content = self.render_children_inline(element)
        if tag == "emphasis":
            return f"<em>{content}</em>"
        if tag == "strong":
            return f"<strong>{content}</strong>"
        if tag == "strikethrough":
            return f"<s>{content}</s>"
        if tag == "sub":
            return f"<sub>{content}</sub>"
        if tag == "sup":
            return f"<sup>{content}</sup>"
        if tag == "code":
            return f"<code>{content}</code>"
        if tag == "a":
            href = element.get(f"{{{XLINK_NS}}}href") or element.get("href") or "#"
            return f'<a href="{html.escape(href, quote=True)}">{content}</a>'
        if tag == "image":
            href = element.get(f"{{{XLINK_NS}}}href") or element.get("href") or ""
            asset = self.images.get(href.lstrip("#"))
            if asset:
                src = html.escape(asset.filename.replace("images/", "../images/"), quote=True)
                return f'<img src="{src}" alt="" />'
            return ""
        if tag == "empty-line":
            return "<br />"
        return content

    def render_block(self, element: ET.Element) -> str:
        tag = strip_ns(element.tag)
        content = self.render_children_inline(element)
        if tag == "p":
            return f"<p>{content}</p>"
        if tag == "subtitle":
            return f"<h3>{content}</h3>"
        if tag == "text-author":
            return f'<p class="text-author">{content}</p>'
        if tag == "date":
            return f'<p class="date">{content}</p>'
        if tag == "epigraph":
            blocks = "".join(self.render_block(child) for child in element)
            return f"<blockquote>{blocks}</blockquote>"
        if tag == "poem":
            return self.render_poem(element)
        if tag == "cite":
            blocks = "".join(self.render_block(child) for child in element)
            return f"<blockquote>{blocks}</blockquote>"
        if tag == "empty-line":
            return '<p class="empty-line">&#160;</p>'
        if tag == "image":
            return f"<p>{self.render_inline(element)}</p>"
        if tag == "section":
            return self.render_section_content(element, include_title=True)
        if tag == "title":
            title = collect_text(element)
            return f"<h2>{html.escape(title)}</h2>" if title else ""
        return "".join(self.render_block(child) for child in element)

    def render_poem(self, poem: ET.Element) -> str:
        parts = ['<div class="poem">']
        for child in poem:
            tag = strip_ns(child.tag)
            if tag == "title":
                title = collect_text(child)
                if title:
                    parts.append(f"<h3>{html.escape(title)}</h3>")
            elif tag == "stanza":
                parts.append('<div class="stanza">')
                for line in child:
                    if strip_ns(line.tag) == "v":
                        parts.append(f"<p>{self.render_children_inline(line)}</p>")
                    else:
                        parts.append(self.render_block(line))
                parts.append("</div>")
            else:
                parts.append(self.render_block(child))
        parts.append("</div>")
        return "".join(parts)

    def render_section_content(self, section: ET.Element, include_title: bool) -> str:
        parts: List[str] = []
        title = section.find("fb:title", NS)
        if include_title and title is not None:
            title_text = collect_text(title)
            if title_text:
                parts.append(f"<h2>{html.escape(title_text)}</h2>")
        for child in section:
            if child is title:
                continue
            parts.append(self.render_block(child))
        return "".join(parts)


def parse_fb2(path: Path) -> Book:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ConversionError(f"Cannot parse FB2 XML: {exc}") from exc

    root = tree.getroot()
    description = root.find("fb:description", NS)
    title_info = root.find("fb:description/fb:title-info", NS)
    if description is None or title_info is None:
        raise ConversionError("FB2 file does not contain description/title-info metadata.")

    title = find_text(title_info, "fb:book-title", path.stem)
    authors = [parse_author(author) for author in title_info.findall("fb:author", NS)]
    language = find_text(title_info, "fb:lang", "und")
    document_id = find_text(description, "fb:document-info/fb:id")
    identifier = document_id or f"urn:uuid:{uuid.uuid4()}"
    images = parse_images(root)
    cover_href = get_cover_href(description, images)
    annotation_node = title_info.find("fb:annotation", NS)
    annotation = ""
    if annotation_node is not None:
        annotation = Fb2HtmlRenderer(images).render_block(annotation_node)

    renderer = Fb2HtmlRenderer(images)
    chapters: List[Chapter] = []
    body = root.find("fb:body", NS)
    if body is not None:
        sections = body.findall("fb:section", NS)
        if sections:
            for index, section in enumerate(sections, start=1):
                section_title = collect_text(section.find("fb:title", NS)) if section.find("fb:title", NS) is not None else ""
                chapter_title = section_title or f"Chapter {index}"
                filename = f"text/chapter-{index:03d}.xhtml"
                body_html = renderer.render_section_content(section, include_title=True)
                chapters.append(Chapter(chapter_title, filename, body_html))
        else:
            body_html = "".join(renderer.render_block(child) for child in body)
            chapters.append(Chapter(title, "text/chapter-001.xhtml", body_html))

    if not chapters:
        chapters.append(Chapter(title, "text/chapter-001.xhtml", "<p></p>"))

    return Book(title, authors or ["Unknown Author"], language, identifier, annotation, chapters, images, cover_href)


def decode_command_output(data: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def text_to_html(text: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return "<p></p>"
    return "\n".join(f"<p>{html.escape(paragraph).replace(chr(10), '<br />')}</p>" for paragraph in paragraphs)


def parse_djvu(path: Path) -> Book:
    djvutxt = shutil.which("djvutxt")
    if not djvutxt:
        raise ConversionError(
            "DjVu conversion requires DjVuLibre. Install it first, for example: brew install djvulibre"
        )

    try:
        result = subprocess.run([djvutxt, str(path)], check=False, capture_output=True)
    except OSError as exc:
        raise ConversionError(f"Cannot run djvutxt: {exc}") from exc

    if result.returncode != 0:
        message = decode_command_output(result.stderr).strip() or "djvutxt failed"
        raise ConversionError(message)

    text = decode_command_output(result.stdout).strip()
    if not text:
        raise ConversionError("DjVu file does not contain an extractable text layer.")

    pages = [page.strip() for page in text.split("\f") if page.strip()]
    chapters: List[Chapter] = []
    if pages:
        for index, page in enumerate(pages, start=1):
            chapters.append(Chapter(f"Page {index}", f"text/page-{index:03d}.xhtml", text_to_html(page)))
    else:
        chapters.append(Chapter(path.stem, "text/chapter-001.xhtml", text_to_html(text)))

    return Book(
        title=path.stem,
        authors=["Unknown Author"],
        language="und",
        identifier=f"urn:uuid:{uuid.uuid4()}",
        chapters=chapters,
    )


def xhtml_page(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css" />
</head>
<body>
{body}
</body>
</html>
"""


def container_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def content_opf(book: Book) -> str:
    creators = "\n".join(f"    <dc:creator>{html.escape(author)}</dc:creator>" for author in book.authors)
    image_items = "\n".join(
        f'    <item id="img-{index}" href="{html.escape(asset.filename, quote=True)}" media-type="{html.escape(asset.media_type, quote=True)}" />'
        for index, asset in enumerate(book.images.values(), start=1)
    )
    chapter_items = "\n".join(
        f'    <item id="chapter-{index}" href="{html.escape(chapter.filename, quote=True)}" media-type="application/xhtml+xml" />'
        for index, chapter in enumerate(book.chapters, start=1)
    )
    spine = "\n".join(f'    <itemref idref="chapter-{index}" />' for index, _ in enumerate(book.chapters, start=1))
    cover_meta = ""
    if book.cover_href:
        cover_id = next(
            (f"img-{index}" for index, asset in enumerate(book.images.values(), start=1) if asset.filename == book.cover_href),
            "",
        )
        if cover_id:
            cover_meta = f'\n    <meta name="cover" content="{cover_id}" />'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="book-id" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{html.escape(book.title)}</dc:title>
{creators}
    <dc:language>{html.escape(book.language)}</dc:language>
    <dc:identifier id="book-id">{html.escape(book.identifier)}</dc:identifier>{cover_meta}
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />
    <item id="css" href="styles/style.css" media-type="text/css" />
{chapter_items}
{image_items}
  </manifest>
  <spine toc="ncx">
{spine}
  </spine>
</package>
"""


def toc_ncx(book: Book) -> str:
    nav_points = "\n".join(
        f"""    <navPoint id="navPoint-{index}" playOrder="{index}">
      <navLabel><text>{html.escape(chapter.title)}</text></navLabel>
      <content src="{html.escape(chapter.filename, quote=True)}" />
    </navPoint>"""
        for index, chapter in enumerate(book.chapters, start=1)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{html.escape(book.identifier, quote=True)}" />
    <meta name="dtb:depth" content="1" />
    <meta name="dtb:totalPageCount" content="0" />
    <meta name="dtb:maxPageNumber" content="0" />
  </head>
  <docTitle><text>{html.escape(book.title)}</text></docTitle>
  <navMap>
{nav_points}
  </navMap>
</ncx>
"""


def stylesheet() -> str:
    return """body {
  font-family: serif;
  line-height: 1.45;
  margin: 5%;
}
h1, h2, h3 {
  line-height: 1.2;
  text-align: center;
}
p {
  margin: 0 0 0.85em;
  text-indent: 1.4em;
}
blockquote {
  margin: 1em 2em;
  font-style: italic;
}
img {
  display: block;
  height: auto;
  margin: 1em auto;
  max-width: 100%;
}
.text-author, .date {
  text-align: right;
}
.empty-line {
  text-indent: 0;
}
.poem p {
  text-indent: 0;
  margin-bottom: 0.2em;
}
"""


def write_epub(book: Book, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        epub.writestr("META-INF/container.xml", container_xml(), compress_type=zipfile.ZIP_DEFLATED)
        epub.writestr("OEBPS/content.opf", content_opf(book), compress_type=zipfile.ZIP_DEFLATED)
        epub.writestr("OEBPS/toc.ncx", toc_ncx(book), compress_type=zipfile.ZIP_DEFLATED)
        epub.writestr("OEBPS/styles/style.css", stylesheet(), compress_type=zipfile.ZIP_DEFLATED)
        for chapter in book.chapters:
            epub.writestr(
                f"OEBPS/{chapter.filename}",
                xhtml_page(chapter.title, chapter.body_html),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        for asset in book.images.values():
            epub.writestr(f"OEBPS/{asset.filename}", asset.data, compress_type=zipfile.ZIP_DEFLATED)


def convert_file(input_path: Path, output_path: Optional[Path] = None) -> Path:
    if not input_path.exists():
        raise ConversionError(f"Input file not found: {input_path}")
    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ConversionError(f"Input file should have one of these extensions ({supported}): {input_path}")
    output = output_path or input_path.with_suffix(".epub")
    if suffix == ".fb2":
        book = parse_fb2(input_path)
    else:
        book = parse_djvu(input_path)
    write_epub(book, output)
    return output


def find_book_files(directory: Path) -> List[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)


def output_for_directory_item(input_file: Path, input_dir: Path, output_dir: Optional[Path]) -> Path:
    if output_dir is None:
        return input_file.with_suffix(".epub")
    relative = input_file.relative_to(input_dir).with_suffix(".epub")
    return output_dir / relative


def convert_directory(input_dir: Path, output_dir: Optional[Path] = None) -> List[Path]:
    if not input_dir.exists():
        raise ConversionError(f"Input directory not found: {input_dir}")
    if not input_dir.is_dir():
        raise ConversionError(f"Input path is not a directory: {input_dir}")
    if output_dir is not None and output_dir.exists() and not output_dir.is_dir():
        raise ConversionError(f"Output path should be a directory for batch conversion: {output_dir}")

    input_files = find_book_files(input_dir)
    if not input_files:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ConversionError(f"No supported files ({supported}) found in directory: {input_dir}")

    created: List[Path] = []
    failures: List[str] = []
    for input_file in input_files:
        output_file = output_for_directory_item(input_file, input_dir, output_dir)
        try:
            created.append(convert_file(input_file, output_file))
        except ConversionError as exc:
            failures.append(f"{input_file}: {exc}")

    if failures:
        message = "Some files could not be converted:\n" + "\n".join(f"  - {failure}" for failure in failures)
        raise ConversionError(message)
    return created


def convert_path(input_path: Path, output_path: Optional[Path] = None) -> List[Path]:
    if input_path.is_dir():
        return convert_directory(input_path, output_path)
    return [convert_file(input_path, output_path)]


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert FB2 and DjVu books to EPUB.")
    parser.add_argument("input", type=Path, help="Path to .fb2/.djvu/.djv file or directory with books")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to output .epub file, or output directory when input is a directory",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        outputs = convert_path(args.input, args.output)
    except ConversionError as exc:
        print(f"Error: {exc}")
        return 1
    for output in outputs:
        print(f"Created {output}")
    if len(outputs) > 1:
        print(f"Converted {len(outputs)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
