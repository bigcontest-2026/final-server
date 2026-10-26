"""Local snapshot exports. No model calls, remote conversion or mutable files.

HWPX packaging follows Hancom's published OWPML structure:
https://tech.hancom.com/hwpxformat/
PDF embeds an unmodified OFL NanumGothic TrueType font and a ToUnicode map.
The small PDF serializer deliberately supports only text, headings and rules;
it does not interpret authored markup as executable content or external links.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import struct
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED
import zlib


ASSETS = Path(__file__).with_name("export_assets")
MIME_TYPES = {"md": "text/markdown; charset=utf-8", "pdf": "application/pdf", "hwpx": "application/hwp+zip"}
NS = {"hh": "http://www.hancom.co.kr/hwpml/2011/head", "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
      "hs": "http://www.hancom.co.kr/hwpml/2011/section", "hc": "http://www.hancom.co.kr/hwpml/2011/core",
      "opf": "http://www.idpf.org/2007/opf/"}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


class PdfUnsupportedGlyph(ValueError):
    """Do not silently produce empty squares for unsupported author text."""


def _plain(text):
    """Readable legacy Markdown, preserving URLs, values and authored prose."""
    text = re.sub(r"\[([^\]]+)\]\(<?([^)>]+)>?\)", r"\1 (\2)", str(text))
    # Saved Markdown escapes author-entered markup. Preserve those literals
    # while removing only generated Markdown emphasis/code delimiters.
    literals = []
    def protect(match):
        literals.append(match.group(1))
        return f"\x00{len(literals) - 1}\x00"
    text = re.sub(r"\\([\\`*_{}\[\]()<>#!|])", protect, text)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"\x00(\d+)\x00", lambda match: literals[int(match.group(1))], text)


def report_document(report):
    """Never regenerate a historical report from its current workspace."""
    if isinstance(report.get("document"), dict):
        return report["document"]
    sections = []
    current = {"id": "saved-0", "title": "", "paragraphs": []}
    title = report.get("title") or "저장 보고서"
    for line in report.get("markdown", "").splitlines():
        if line.startswith("# "):
            title = _plain(line[2:])
        elif re.match(r"^#{2,6} ", line):
            if current["title"] or current["paragraphs"]:
                sections.append(current)
            current = {"id": f"saved-{len(sections)}", "title": _plain(re.sub(r"^#+ ", "", line)), "paragraphs": []}
        elif line.strip():
            current["paragraphs"].append(_plain(line))
    if current["title"] or current["paragraphs"]:
        sections.append(current)
    return {"version": "saved-snapshot", "title": title, "subtitle": "", "metadata": [], "sections": sections}


def _blocks(document):
    yield "title", document.get("title", "저장 보고서")
    if document.get("subtitle"):
        yield "subtitle", document["subtitle"]
    for item in document.get("metadata", []):
        yield "metadata", f"{item['label']}: {item['value']}"
    for section in document.get("sections", []):
        if section.get("title"):
            yield "heading", section["title"]
        for paragraph in section.get("paragraphs", []):
            yield "body", str(paragraph)


@lru_cache(maxsize=1)
def _font():
    """Read just the standard TrueType tables needed for glyph metrics."""
    data = (ASSETS / "NanumGothic-Regular.ttf").read_bytes()
    u16 = lambda offset: struct.unpack_from(">H", data, offset)[0]
    s16 = lambda offset: struct.unpack_from(">h", data, offset)[0]
    tables = {}
    for index in range(u16(4)):
        tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + index * 16)
        tables[tag.decode()] = (offset, length)
    head, hhea, hmtx, cmap = (tables[tag][0] for tag in ("head", "hhea", "hmtx", "cmap"))
    units = u16(head + 18)
    metric_count = u16(hhea + 34)
    widths = [u16(hmtx + i * 4) for i in range(metric_count)]
    mapping = {}
    for index in range(u16(cmap + 2)):
        platform, encoding, relative = struct.unpack_from(">HHI", data, cmap + 4 + index * 8)
        offset = cmap + relative
        if u16(offset) != 4 or platform not in (0, 3):
            continue
        count = u16(offset + 6) // 2
        end = offset + 14
        start = end + count * 2 + 2
        delta = start + count * 2
        ranges = delta + count * 2
        for segment in range(count):
            first, last = u16(start + segment * 2), u16(end + segment * 2)
            change, distance = s16(delta + segment * 2), u16(ranges + segment * 2)
            for code in range(first, min(last, 65534) + 1):
                if distance:
                    address = ranges + segment * 2 + distance + (code - first) * 2
                    glyph = u16(address) if address + 2 <= len(data) else 0
                    glyph = (glyph + change) & 65535 if glyph else 0
                else:
                    glyph = (code + change) & 65535
                mapping[code] = glyph
    return data, mapping, widths, units


def _pdf(document):
    data, mapping, widths, units = _font()
    blocks = list(_blocks(document))
    if any(not mapping.get(ord(char)) for _, text in blocks for char in str(text) if char not in "\r\n\t"):
        raise PdfUnsupportedGlyph("pdf_unsupported_glyph")
    # A unique CID per Unicode character preserves extraction even if several
    # unsupported characters map to the font's missing-glyph shape.
    characters = sorted(set("".join(text for _, text in blocks) + "HERMES 검토용 보고서0123456789 /"))
    if len(characters) > 65534:
        raise ValueError("export_document_too_complex")
    cids = {char: index + 1 for index, char in enumerate(characters)}
    glyphs = [0] + [mapping.get(ord(char), 0) for char in characters]
    advances = [round(widths[min(gid, len(widths) - 1)] * 1000 / units) for gid in glyphs]
    encode = lambda text: "".join(f"{cids[char]:04X}" for char in text)
    def width(text, size):
        return sum(advances[cids[char]] for char in text) * size / 1000
    def wrap(text, size, limit):
        for paragraph in text.replace("\r", "").split("\n"):
            line, used = "", 0
            for char in paragraph.expandtabs(4):
                if char not in cids:
                    continue
                advance = advances[cids[char]] * size / 1000
                if line and used + advance > limit:
                    yield line
                    line, used = "", 0
                line += char
                used += advance
            yield line
    pages, commands, y = [], [], 776.0
    def draw(text, size, x, baseline, color="0.14 0.18 0.23"):
        commands.append(f"BT /F0 {size} Tf {color} rg 1 0 0 1 {x:.2f} {baseline:.2f} Tm <{encode(text)}> Tj ET")
    def finish():
        draw("HERMES 검토용 보고서", 8, 54, 808, "0.40 0.45 0.50")
        draw(str(len(pages) + 1), 9, 535, 33, "0.40 0.45 0.50")
        commands.append("0.84 0.87 0.90 RG 0.5 w 54 795 m 541 795 l S")
        pages.append("\n".join(commands).encode("ascii"))
        commands.clear()
    for kind, text in blocks:
        size = {"title": 20, "subtitle": 11, "metadata": 9.5, "heading": 13.5, "body": 10.5}[kind]
        leading = size * 1.65
        before = 15 if kind == "heading" else 0
        if y - before - leading * (2 if kind == "heading" else 1) < 62:
            finish()
            y = 776
        y -= before
        for line in wrap(str(text), size, 487):
            if y - leading < 62:
                finish()
                y = 776
            draw(line, size, 54, y, "0.10 0.28 0.36" if kind in ("title", "heading") else "0.14 0.18 0.23")
            y -= leading
        y -= 7 if kind != "metadata" else 2
    if commands:
        finish()
    objects = []
    def add(value):
        objects.append(value.encode("ascii") if isinstance(value, str) else value)
        return len(objects)
    def stream(value, extra=""):
        compressed = zlib.compress(value)
        return add(f"<< /Length {len(compressed)} /Filter /FlateDecode {extra} >>\nstream\n".encode() + compressed + b"\nendstream")
    catalog = add("")
    tree = add("")
    font_file = stream(data, f"/Length1 {len(data)}")
    descriptor = add(f"<< /Type /FontDescriptor /FontName /NanumGothic /Flags 4 /FontBBox [-1000 -1000 2000 2000] /ItalicAngle 0 /Ascent 950 /Descent -250 /CapHeight 750 /StemV 80 /FontFile2 {font_file} 0 R >>")
    gid_map = stream(b"".join(struct.pack(">H", gid) for gid in glyphs))
    cidfont = add(f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NanumGothic /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /FontDescriptor {descriptor} 0 R /CIDToGIDMap {gid_map} 0 R /DW 1000 /W [0 [{' '.join(map(str, advances))}]] >>")
    cmap = ["/CIDInit /ProcSet findresource begin", "12 dict begin begincmap", "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def", "/CMapName /HermesUnicode def /CMapType 2 def", "1 begincodespacerange <0000> <FFFF> endcodespacerange"]
    for start in range(0, len(characters), 100):
        group = characters[start:start + 100]
        cmap.append(f"{len(group)} beginbfchar")
        cmap.extend(f"<{cids[char]:04X}> <{char.encode('utf-16-be').hex().upper()}>" for char in group)
        cmap.append("endbfchar")
    cmap.extend(["endcmap CMapName currentdict /CMap defineresource pop end end"])
    unicode_map = stream("\n".join(cmap).encode("ascii"))
    typefont = add(f"<< /Type /Font /Subtype /Type0 /BaseFont /NanumGothic /Encoding /Identity-H /DescendantFonts [{cidfont} 0 R] /ToUnicode {unicode_map} 0 R >>")
    page_ids = []
    for page in pages:
        content = stream(page)
        page_ids.append(add(f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font << /F0 {typefont} 0 R >> >> /Contents {content} 0 R >>"))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R /Lang (ko-KR) >>".encode()
    objects[tree - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>".encode()
    result = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def _element(parent, prefix, name, attrs=None, text=None):
    node = ET.SubElement(parent, f"{{{NS[prefix]}}}{name}", {key: str(value) for key, value in (attrs or {}).items()})
    if text is not None:
        # XML 1.0 controls cannot appear in an editable document.
        node.text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text))
    return node


def _hwpx_header():
    head = ET.Element(f"{{{NS['hh']}}}head", {"version": "1.2", "secCnt": "1"})
    _element(head, "hh", "beginNum", {"page": 1, "footnote": 1, "endnote": 1, "pic": 1, "tbl": 1, "equation": 1})
    refs = _element(head, "hh", "refList")
    faces = _element(refs, "hh", "fontfaces", {"itemCnt": 7})
    languages = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
    for language in languages:
        face = _element(faces, "hh", "fontface", {"lang": language, "fontCnt": 1})
        font = _element(face, "hh", "font", {"id": 0, "face": "나눔고딕", "type": "TTF", "isEmbedded": 0})
        _element(font, "hh", "typeInfo", {"familyType": "FCAT_GOTHIC", "weight": 5, "proportion": 0, "contrast": 0, "strokeVariation": 1, "armStyle": 1, "letterform": 1, "midline": 1, "xHeight": 1})
    fills = _element(refs, "hh", "borderFills", {"itemCnt": 1})
    fill = _element(fills, "hh", "borderFill", {"id": 1, "threeD": 0, "shadow": 0, "centerLine": "NONE", "breakCellSeparateLine": 0})
    for name in ("slash", "backSlash"):
        _element(fill, "hh", name, {"type": "NONE", "Crooked": 0, "isCounter": 0})
    for name in ("leftBorder", "rightBorder", "topBorder", "bottomBorder", "diagonal"):
        _element(fill, "hh", name, {"type": "NONE", "width": "0.1 mm", "color": "#000000"})
    props = _element(refs, "hh", "charProperties", {"itemCnt": 3})
    for index, size in enumerate((1050, 2000, 1400)):
        prop = _element(props, "hh", "charPr", {"id": index, "height": size, "textColor": "#1F2937", "shadeColor": "none", "useFontSpace": 0, "useKerning": 0, "symMark": "NONE", "borderFillIDRef": 1})
        for name, value in (("fontRef", 0), ("ratio", 100), ("spacing", 0), ("relSz", 100), ("offset", 0)):
            _element(prop, "hh", name, {language.lower(): value for language in languages})
        if index:
            _element(prop, "hh", "bold")
        _element(prop, "hh", "underline", {"type": "NONE", "shape": "SOLID", "color": "#000000"})
        _element(prop, "hh", "strikeout", {"shape": "NONE", "color": "#000000"})
        _element(prop, "hh", "outline", {"type": "NONE"})
        _element(prop, "hh", "shadow", {"type": "NONE", "color": "#808080", "offsetX": 10, "offsetY": 10})
    tabs = _element(refs, "hh", "tabProperties", {"itemCnt": 1})
    _element(tabs, "hh", "tabPr", {"id": 0, "autoTabLeft": 0, "autoTabRight": 0})
    numberings = _element(refs, "hh", "numberings", {"itemCnt": 1})
    numbering = _element(numberings, "hh", "numbering", {"id": 1, "start": 1})
    for level in range(1, 8):
        _element(numbering, "hh", "paraHead", {"start": 1, "level": level, "align": "LEFT", "useInstWidth": 1,
                 "autoIndent": 1, "widthAdjust": 0, "textOffsetType": "PERCENT", "textOffset": 50,
                 "numFormat": "DIGIT", "charPrIDRef": 0, "checkable": 0}, text=f"^{level}.")
    paras = _element(refs, "hh", "paraProperties", {"itemCnt": 2})
    for index in (0, 1):
        para = _element(paras, "hh", "paraPr", {"id": index, "tabPrIDRef": 0, "condense": 0, "fontLineHeight": 0, "snapToGrid": 0, "suppressLineNumbers": 0, "checked": 0})
        _element(para, "hh", "align", {"horizontal": "LEFT", "vertical": "BASELINE"})
        _element(para, "hh", "heading", {"type": "NONE", "idRef": 0, "level": 0})
        _element(para, "hh", "breakSetting", {"breakLatinWord": "BREAK_WORD", "breakNonLatinWord": "BREAK_WORD", "widowOrphan": 1, "keepWithNext": index, "keepLines": 0, "pageBreakBefore": 0, "lineWrap": "BREAK"})
        _element(para, "hh", "autoSpacing", {"eAsianEng": 0, "eAsianNum": 0})
        margin = _element(para, "hh", "margin")
        for name, value in (("intent", 0), ("left", 0), ("right", 0), ("prev", 800 if index else 0), ("next", 500)):
            _element(margin, "hc", name, {"value": value, "unit": "HWPUNIT"})
        _element(para, "hh", "lineSpacing", {"type": "PERCENT", "value": 165, "unit": "HWPUNIT"})
        _element(para, "hh", "border", {"borderFillIDRef": 1, "offsetLeft": 0, "offsetRight": 0, "offsetTop": 0, "offsetBottom": 0, "connect": 0, "ignoreMargin": 0})
    styles = _element(refs, "hh", "styles", {"itemCnt": 1})
    _element(styles, "hh", "style", {"id": 0, "type": "PARA", "name": "바탕글", "engName": "Normal", "paraPrIDRef": 0, "charPrIDRef": 0, "nextStyleIDRef": 0, "langID": 1042, "lockForm": 0})
    compatible = _element(head, "hh", "compatibleDocument", {"targetProgram": "HWP201X"})
    _element(compatible, "hh", "layoutCompatibility")
    option = _element(head, "hh", "docOption")
    _element(option, "hh", "linkinfo", {"path": "", "pageInherit": 0, "footnoteInherit": 0})
    # The 2011 format spells this element "trackchageConfig"; preserve the
    # spelling used by Hancom-saved documents and the official 2011 model.
    _element(head, "hh", "trackchageConfig", {"flags": 56})
    return head


def _hwpx(document):
    section = ET.Element(f"{{{NS['hs']}}}sec")
    for index, (kind, text) in enumerate(_blocks(document)):
        paragraph = _element(section, "hp", "p", {"id": index, "paraPrIDRef": 1 if kind in ("title", "heading") else 0, "styleIDRef": 0, "pageBreak": 0, "columnBreak": 0, "merged": 0})
        run = _element(paragraph, "hp", "run", {"charPrIDRef": 1 if kind == "title" else 2 if kind == "heading" else 0})
        if index == 0:
            props = _element(run, "hp", "secPr", {"id": "", "textDirection": "HORIZONTAL", "spaceColumns": 1134, "tabStop": 8000, "tabStopVal": 4000, "tabStopUnit": "HWPUNIT", "outlineShapeIDRef": 1, "memoShapeIDRef": 0, "textVerticalWidthHead": 0, "masterPageCnt": 0})
            _element(props, "hp", "grid", {"lineGrid": 0, "charGrid": 0, "wonggojiFormat": 0})
            _element(props, "hp", "startNum", {"pageStartsOn": "BOTH", "page": 1, "pic": 0, "tbl": 0, "equation": 0})
            _element(props, "hp", "visibility", {"hideFirstHeader": 0, "hideFirstFooter": 0, "hideFirstMasterPage": 0, "border": "SHOW_ALL", "fill": "SHOW_ALL", "hideFirstPageNum": 0, "hideFirstEmptyLine": 0, "showLineNumber": 0})
            _element(props, "hp", "lineNumberShape", {"restartType": 0, "countBy": 0, "distance": 0, "startNumber": 0})
            page = _element(props, "hp", "pagePr", {"landscape": "WIDELY", "width": 59528, "height": 84186, "gutterType": "LEFT_ONLY"})
            _element(page, "hp", "margin", {"header": 2835, "footer": 2835, "gutter": 0, "left": 5669, "right": 5669, "top": 5669, "bottom": 5669})
            for name in ("footNotePr", "endNotePr"):
                note = _element(props, "hp", name)
                _element(note, "hp", "autoNumFormat", {"type": "DIGIT", "userChar": "", "prefixChar": "", "suffixChar": ")", "supscript": 0})
                _element(note, "hp", "noteLine", {"length": -1, "type": "SOLID", "width": "0.12 mm", "color": "#000000"})
                _element(note, "hp", "noteSpacing", {"betweenNotes": 283, "belowLine": 567, "aboveLine": 850})
                _element(note, "hp", "numbering", {"type": "CONTINUOUS", "newNum": 1})
                _element(note, "hp", "placement", {"place": "EACH_COLUMN" if name == "footNotePr" else "END_OF_DOCUMENT", "beneathText": 0})
            for page_type in ("BOTH", "EVEN", "ODD"):
                border = _element(props, "hp", "pageBorderFill", {"type": page_type, "borderFillIDRef": 1,
                                  "textBorder": "PAPER", "headerInside": 0, "footerInside": 0, "fillArea": "PAPER"})
                _element(border, "hp", "offset", {"left": 1417, "right": 1417, "top": 1417, "bottom": 1417})
            control = _element(run, "hp", "ctrl")
            _element(control, "hp", "colPr", {"id": "", "type": "NEWSPAPER", "layout": "LEFT", "colCount": 1, "sameSz": 1, "sameGap": 0})
        text_node = _element(run, "hp", "t")
        parts = str(text).split("\n")
        text_node.text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", parts[0])
        for part in parts[1:]:
            br = _element(text_node, "hp", "lineBreak")
            br.tail = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", part)
    package = ET.Element(f"{{{NS['opf']}}}package", {"version": "", "unique-identifier": "", "id": ""})
    metadata = _element(package, "opf", "metadata")
    _element(metadata, "opf", "title", text=document.get("title", "저장 보고서"))
    _element(metadata, "opf", "language", text="ko")
    manifest = _element(package, "opf", "manifest")
    for name, path in (("header", "Contents/header.xml"), ("section0", "Contents/section0.xml"), ("settings", "settings.xml")):
        _element(manifest, "opf", "item", {"id": name, "href": path, "media-type": "application/xml"})
    spine = _element(package, "opf", "spine")
    _element(spine, "opf", "itemref", {"idref": "header", "linear": "yes"})
    _element(spine, "opf", "itemref", {"idref": "section0", "linear": "yes"})
    xml = lambda root: ET.tostring(root, encoding="utf-8", xml_declaration=True)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"application/hwp+zip", compress_type=ZIP_STORED)
        archive.writestr("version.xml", '<?xml version="1.0" encoding="UTF-8"?><hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication="WORDPROCESSOR" major="5" minor="1" micro="0" buildNumber="1" os="1" xmlVersion="1.2" application="HERMES" appVersion="2.0"/>')
        archive.writestr("Contents/header.xml", xml(_hwpx_header()))
        archive.writestr("Contents/section0.xml", xml(section))
        archive.writestr("Contents/content.hpf", xml(package))
        archive.writestr("settings.xml", '<?xml version="1.0" encoding="UTF-8"?><ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app"><ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/></ha:HWPApplicationSetting>')
        archive.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?><ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container"><ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/></ocf:rootfiles></ocf:container>')
        archive.writestr("META-INF/manifest.xml", '<?xml version="1.0" encoding="UTF-8"?><odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>')
        archive.writestr("Preview/PrvText.txt", "\n".join(text for _, text in _blocks(document)).encode("utf-8"))
    return buffer.getvalue()


def export_report(report, format):
    if format not in MIME_TYPES:
        raise ValueError("unsupported_export_format")
    if format == "md":
        return report.get("markdown", "").encode("utf-8"), MIME_TYPES[format]
    document = report_document(report)
    return (_pdf(document) if format == "pdf" else _hwpx(document)), MIME_TYPES[format]
