"""Простой xlsx: одна таблица, без сторонних пакетов.

Excel — это zip с XML. Нам нужна ровно одна страница с шапкой и строками,
поэтому свой сборщик короче и спокойнее, чем тащить openpyxl ради отчёта.
"""

from __future__ import annotations

import zipfile
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
OD = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def book(sheet: str, headers: list[str], rows: list[list]) -> bytes:
    """Книга с одной страницей. `headers` — первая строка, дальше `rows`."""

    title = _sheet_name(sheet)
    body = _sheet(headers, rows)
    out = BytesIO()

    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', _TYPES)
        archive.writestr('_rels/.rels', _ROOT_RELS)
        archive.writestr('xl/workbook.xml', _workbook(title))
        archive.writestr('xl/_rels/workbook.xml.rels', _BOOK_RELS)
        archive.writestr('xl/styles.xml', _STYLES)
        archive.writestr('xl/worksheets/sheet1.xml', body)

    return out.getvalue()


def _sheet(headers: list[str], rows: list[list]) -> str:
    width = len(headers)
    lines = [
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<worksheet xmlns="{NS}">',
        '<sheetViews><sheetView workbookViewId="0">',
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>',
        '</sheetView></sheetViews>',
        _cols(headers, rows),
        '<sheetData>',
        _row(1, headers, header=True),
    ]

    for index, values in enumerate(rows, start=2):
        padded = list(values) + [None] * (width - len(values))
        lines.append(_row(index, padded[:width]))

    lines.append('</sheetData></worksheet>')
    return '\n'.join(lines)


def _row(number: int, values: list, header: bool = False) -> str:
    cells = []

    for index, value in enumerate(values):
        ref = f'{_col(index)}{number}'
        xml = _cell(ref, value, header)

        if xml:
            cells.append(xml)

    return f'<row r="{number}">{"".join(cells)}</row>'


def _cell(ref: str, value, header: bool) -> str:
    style = ' s="1"' if header else ''

    if value is None or value == '':
        return f'<c r="{ref}"{style}"/>' if header else ''

    if isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, bool):
        text = 'да' if value else 'нет'
        return (
            f'<c r="{ref}"{style} t="inlineStr"><is><t>{_text(text)}</t></is></c>'
        )

    if isinstance(value, int):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'

    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e12:
            return f'<c r="{ref}"{style}><v>{int(value)}</v></c>'

        return f'<c r="{ref}"{style}><v>{value}</v></c>'

    return f'<c r="{ref}"{style} t="inlineStr"><is><t>{_text(value)}</t></is></c>'


def _cols(headers: list[str], rows: list[list]) -> str:
    sample = rows[:80]
    parts = []

    for index, header in enumerate(headers):
        widest = len(str(header))

        for row in sample:
            if index < len(row) and row[index] is not None:
                widest = max(widest, len(str(row[index])))

        width = min(48, max(10, widest + 2))
        parts.append(
            f'<col min="{index + 1}" max="{index + 1}" width="{width}" customWidth="1"/>'
        )

    return f'<cols>{"".join(parts)}</cols>'


def _col(index: int) -> str:
    name = ''
    index += 1

    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name

    return name


def _sheet_name(value: str) -> str:
    cleaned = ''.join(ch for ch in value if ch not in r'[]:*?/\\')[:31]
    return cleaned or 'Отчёт'


def _text(value) -> str:
    text = str(value)
    text = ''.join(ch for ch in text if ord(ch) >= 32 or ch in '\t\n')
    return escape(text, {'"': '&quot;'})


def _workbook(title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{NS}" xmlns:r="{OD}">'
        '<sheets>'
        f'<sheet name="{escape(title)}" sheetId="1" r:id="rId1"/>'
        '</sheets></workbook>'
    )


_TYPES = f'''\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
'''

_ROOT_RELS = f'''\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{OD}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
'''

_BOOK_RELS = f'''\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{OD}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{OD}/styles" Target="styles.xml"/>
</Relationships>
'''

_STYLES = f'''\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS}">
  <fonts count="2">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf/></cellStyleXfs>
  <cellXfs count="2">
    <xf xfId="0"/>
    <xf xfId="0" fontId="1" applyFont="1"/>
  </cellXfs>
</styleSheet>
'''
