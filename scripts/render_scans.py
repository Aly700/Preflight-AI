"""Render fictional Markdown-lite documents as deterministic 150 dpi letter scans."""

import argparse
import csv
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import random
import re
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
RENDERER_VERSION = 'scans-2026-11-09.2'
PAGE_SIZE = (1275, 1650)
LEFT, RIGHT, TOP, BOTTOM = 140, 1135, 240, 1420
FONT_FILES = {
    'courier': 'courierprime/CourierPrime-Regular.ttf',
    'bold': 'courierprime/CourierPrime-Bold.ttf',
    'serif': 'librebaskerville/LibreBaskerville[wght].ttf',
    'hand': 'caveat/Caveat[wght].ttf',
}
KINDS = {'memo', 'policy', 'glossary', 'retrospective', 'handover', 'coordination', 'form'}
LETTER_KINDS = {'memo', 'policy', 'glossary', 'retrospective'}
RECORD_ID = re.compile(r'\b(?:DEMO-[A-Z]+|ISSUE|TASK|EVT|TEAM|PERSON|SHIFT|DOC)-[\w-]+', re.IGNORECASE)
CLERICAL_NOTES = ('seen', 'file', 'copy to desk lead', 'ok')
HEADERS = {
    'doc_id',
    'kind',
    'title',
    'date',
    'station',
    'program',
    'author',
    'team',
    'related_ids',
    'authored_by',
    'spec_version',
}
Pixels = NDArray[np.uint8]
Fonts = dict[str, ImageFont.FreeTypeFont]


@dataclass(frozen=True)
class Document:
    fields: dict[str, str]
    body: str
    source_sha256: str

    @property
    def doc_id(self) -> str:
        return self.fields['doc_id']


@dataclass(frozen=True)
class Block:
    kind: str
    text: str = ''
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Box:
    text: str
    x: int
    y: int
    width: int
    height: int
    style: str
    font_size: int = 0
    angle: int = 0


@dataclass(frozen=True)
class Stamp:
    text: str
    date: str
    x: int
    y: int
    angle: float


@dataclass
class Page:
    boxes: list[Box] = field(default_factory=list)
    rules: list[tuple[int, int, int, int]] = field(default_factory=list)
    stamps: list[Stamp] = field(default_factory=list)


def author_identity(document: Document, synthetic: Path) -> tuple[str, str]:
    """Resolve display names from runtime-mounted CSVs, never image contents."""
    with (synthetic / 'people.csv').open(encoding='utf-8', newline='') as handle:
        people = {row['person_id']: row for row in csv.DictReader(handle)}
    with (synthetic / 'teams.csv').open(encoding='utf-8', newline='') as handle:
        teams = {row['team_id']: row['team_name'] for row in csv.DictReader(handle)}
    person = people.get(document.fields['author'])
    if person is None or person['team_id'] not in teams:
        raise ValueError('Author or team missing from synthetic CSVs')
    if document.fields.get('team', person['team_id']) != person['team_id']:
        raise ValueError('Document author/team mismatch')
    return person['display_name'], teams[person['team_id']]


def verify_fonts(directory: Path = ROOT / 'assets/fonts') -> dict[str, Path]:
    """Read the vendored checksum manifest, then verify every font before use."""
    readme = (directory / 'README.md').read_text(encoding='utf-8')
    checksums = dict(re.findall(r'\| `([^`]+\.ttf)` \|[^\n]*?\| `([0-9a-f]{64})` \|', readme))
    result = {}
    for family, name in FONT_FILES.items():
        if name not in checksums:
            raise ValueError(f'Font checksum missing from assets/fonts/README.md: {name}')
        path = directory / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksums[name]:
            raise ValueError(f'Font SHA-256 mismatch: {name}')
        result[family] = path
    return result


def load_fonts(directory: Path = ROOT / 'assets/fonts') -> Fonts:
    paths = verify_fonts(directory)
    styles = {
        'serif': ('serif', 24),
        'type': ('courier', 27),
        'label': ('bold', 26),
        'hand': ('hand', 34),
        'heading': ('serif', 31),
        'type_heading': ('bold', 31),
        'letterhead': ('serif', 30),
        'small': ('courier', 21),
        'stamp': ('bold', 29),
    }
    # BASIC avoids platform-dependent optional shaping engines for this Latin corpus.
    return {
        style: ImageFont.truetype(str(paths[family]), size, layout_engine=ImageFont.Layout.BASIC)
        for style, (family, size) in styles.items()
    }


def read_document(path: Path) -> Document:
    if path.is_symlink():
        raise ValueError('Document must not be a symbolic link')
    raw = path.read_bytes()
    lines = raw.decode('utf-8').splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError('Document requires delimited front matter')
    end = lines.index('---', 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(':')
        if not separator or key not in HEADERS or key in fields:
            raise ValueError('Front matter requires unique supported key: value lines')
        fields[key] = value.strip()
    if any(not fields.get(key) for key in ('doc_id', 'kind', 'title', 'date', 'author')):
        raise ValueError('Document requires doc_id, kind, title, date and author')
    if not re.fullmatch(r'DOC-[0-9]{4}', fields['doc_id']) or fields['doc_id'] != path.stem:
        raise ValueError('Invalid document ID or filename mismatch')
    if fields['kind'] not in KINDS:
        raise ValueError('Unsupported document kind')
    return Document(fields, '\n'.join(lines[end + 1 :]).strip(), hashlib.sha256(raw).hexdigest())


def inline_text(text: str) -> str:
    """Remove paired emphasis only; preserve underscores inside status names."""
    text = re.sub(r'\*\*(.+?)\*\*|__(.+?)__', lambda m: m[1] or m[2], text)
    return re.sub(r'(?<!\w)[*_]([^*_]+)[*_](?!\w)', r'\1', text).strip()


def parse_body(body: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []
    table: list[tuple[str, ...]] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Block('paragraph', inline_text(' '.join(paragraph))))
            paragraph.clear()
        if table:
            if any(len(row) != len(table[0]) for row in table):
                raise ValueError('Table rows must have the same number of cells')
            blocks.append(Block('table', rows=tuple(table)))
            table.clear()

    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith('|') and line.endswith('|'):
            if paragraph:
                flush()
            cells = tuple(inline_text(cell) for cell in line[1:-1].split('|'))
            if not all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells):
                table.append(cells)
        else:
            if table:
                flush()
            heading = re.match(r'^#{1,6}\s+(.+)', line)
            listing = re.match(r'^(?:[-*+] |\d+[.)] )(.+)', line)
            form = re.match(r'^[^:]{1,70}:\s*', line)
            if not line or heading or listing or form:
                flush()
                if heading:
                    blocks.append(Block('heading', inline_text(heading[1])))
                elif listing:
                    blocks.append(Block('list', inline_text(listing[1])))
                elif form:
                    blocks.append(Block('field', inline_text(line)))
            else:
                paragraph.append(line)
    flush()
    return blocks


def clean_body_text(body: str) -> str:
    """Reference text: visible body content in row order, without Markdown syntax."""
    return '\n'.join(
        ' '.join(cell for cell in row if cell) if block.kind == 'table' else block.text
        for block in parse_body(body)
        for row in (block.rows if block.kind == 'table' else ((),))
    )


def wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Measure glyph advances, splitting even a single oversized token if needed."""
    lines: list[str] = []
    current = ''
    for word in text.split():
        proposed = f'{current} {word}' if current else word
        if font.getlength(proposed) <= width:
            current = proposed
            continue
        if current:
            lines.append(current)
            current = ''
        for char in word:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ''
            current += char
    if current:
        lines.append(current)
    return lines or ['']


def layout_document(document: Document, fonts: Fonts) -> list[Page]:
    """Paginate measured text; form/table rows may continue across page boundaries."""
    pages = [Page()]
    y = TOP
    width = RIGHT - LEFT
    kind = document.fields['kind']
    body_style = 'serif' if kind in LETTER_KINDS else 'type'
    heading_style = 'heading' if kind in LETTER_KINDS else 'type_heading'

    def room(height: int) -> None:
        nonlocal y
        if y + height > BOTTOM:
            pages.append(Page())
            y = TOP

    def line(text: str, style: str, x: int, available: int, height: int = 38) -> None:
        nonlocal y
        room(height)
        pages[-1].boxes.append(Box(text, x, y, available, height, style))
        y += height

    def cells(values: list[tuple[str, str, int]]) -> None:
        nonlocal y
        wrapped = [wrap_text(value, fonts[style], cell_width - 20) for value, style, cell_width in values]
        for index in range(max(map(len, wrapped))):
            height = 47
            room(height)
            x = LEFT
            for (_, style, cell_width), lines in zip(values, wrapped, strict=True):
                value = lines[index] if index < len(lines) else ''
                pages[-1].boxes.append(Box(value, x + 10, y + 3, cell_width - 20, height - 8, style))
                pages[-1].rules.extend(
                    [(x, y, x + cell_width, y), (x, y, x, y + height), (x, y + height, x + cell_width, y + height)]
                )
                x += cell_width
            pages[-1].rules.append((x, y, x, y + height))
            y += height

    # Preserve the paper's own header, including prefixes such as POLICY or
    # Subject. Only synthesize a title when the opening header does not have it.
    body_lines = document.body.splitlines()
    title = document.fields['title'].casefold()
    title_present = False
    for index, raw in enumerate(body_lines):
        if not raw.strip():
            break
        if inline_text(raw).casefold().endswith(title):
            title_present = True
            if not raw.startswith('#') and not raw.startswith('Subject:'):
                body_lines[index] = '# ' + raw
            break
    blocks = parse_body('\n'.join(body_lines))
    if not title_present:
        blocks.insert(0, Block('heading', document.fields['title']))
    for block in blocks:
        if block.kind == 'table':
            column_count = len(block.rows[0])
            if column_count > 6:
                raise ValueError('Simple tables support at most six columns')
            for index, row in enumerate(block.rows):
                style = 'label' if index == 0 else body_style
                cells([(value, style, width // column_count) for value in row])
        elif block.kind == 'field' and kind == 'form':
            label, _, value = block.text.partition(':')
            # Preserve supplied values. Only explicitly blank/underscore-only fields stay blank.
            if re.fullmatch(r'[_\s]*', value):
                value = ''
            value_style = 'type' if RECORD_ID.search(value) else 'hand'
            cells([(label + ':', 'label', 300), (value.strip(), value_style, width - 300)])
        else:
            style = heading_style if block.kind == 'heading' else body_style
            indent = 26 if block.kind == 'list' else 0
            height = 46 if block.kind == 'heading' else 37
            lines = wrap_text(block.text, fonts[style], width - indent)
            if block.kind == 'heading':
                room(min((len(lines) + 1) * height, BOTTOM - TOP))
            for index, value in enumerate(lines):
                line(value, style, LEFT + indent, width - indent, height)
                if block.kind == 'list' and index == 0:
                    pages[-1].rules.append((LEFT + 2, y - height + 13, LEFT + 12, y - height + 13))
        y += 13 if block.kind != 'field' else 0
    return pages


@dataclass(frozen=True)
class ScanParameters:
    seed: int
    grayscale: bool
    rotation: float
    offset: tuple[int, int]
    scale: float
    shadow: float
    blur: float
    noise: float
    quality: int
    paper: tuple[int, int, int]
    density: float
    bleed: float
    artifact: str
    stamp: str
    handwriting: bool
    margin_note: bool


def scan_parameters(seed: int) -> ScanParameters:
    rng = random.Random(seed)
    return ScanParameters(
        seed=seed,
        grayscale=rng.random() < 1 / 3,
        rotation=rng.uniform(0.4, 1.2) * rng.choice((-1, 1)),
        offset=(rng.randint(-8, 8), rng.randint(-8, 8)),
        scale=rng.uniform(0.972, 0.98),
        shadow=rng.uniform(0.3, 0.48),
        blur=rng.uniform(0.20, 0.34),
        noise=rng.uniform(2.6, 3.8),
        quality=rng.randint(60, 78),
        paper=(rng.randint(251, 254), rng.randint(247, 250), rng.randint(237, 243)),
        density=rng.uniform(1.03, 1.10),
        bleed=rng.uniform(0.16, 0.24),
        artifact=rng.choice(('', '', '', 'streak', 'fold', 'holes', 'staple')),
        stamp=rng.choice(('', '', '', 'RECEIVED', 'FILED', 'COPY')),
        handwriting=True,
        margin_note=rng.random() < 0.4,
    )


def pixels(values: NDArray[Any]) -> Pixels:
    return np.clip(np.rint(values), 0, 255).astype(np.uint8)


def uneven_ink(source: Pixels, params: ScanParameters) -> Pixels:
    """Word-scale density patches: 0.78-1.24 gain, retaining white paper."""
    height, width = source.shape[:2]
    rng = np.random.default_rng(params.seed)
    field = rng.integers(0, 256, (max(4, height // 32), max(4, width // 75)), dtype=np.uint8)
    local = np.asarray(Image.fromarray(field).resize((width, height), Image.Resampling.BICUBIC)) / 255
    y = np.linspace(0, 5 * np.pi, height)[:, None]
    density = np.clip(params.density + 0.38 * (local - 0.5) + 0.04 * np.sin(y), 0.78, 1.24)
    return pixels(255 - (255 - source.astype(np.float32)) * density[:, :, None])


def paper_tone(source: Pixels, params: ScanParameters) -> Pixels:
    """Warm paper with low-frequency illumination and a gentle edge vignette."""
    height, width = source.shape[:2]
    rng = np.random.default_rng(params.seed)
    low = rng.integers(0, 7, (24, 18), dtype=np.uint8)
    illumination = np.asarray(Image.fromarray(low).resize((width, height), Image.Resampling.BICUBIC))
    y, x = np.ogrid[-1 : 1 : complex(height), -1 : 1 : complex(width)]
    vignette = 1.4 * (x * x + y * y)
    paper = np.array(params.paper) - illumination[:, :, None] - vignette[:, :, None]
    return pixels(source.astype(np.float32) * paper / 255)


def paper_grain(source: Pixels, params: ScanParameters) -> Pixels:
    """Fine correlated fibers, with absolute noise capped at four intensity levels."""
    rng = np.random.default_rng(params.seed + 1)
    noise = rng.normal(0, params.noise, source.shape[:2])
    fibers = (2 * noise + np.roll(noise, 1, axis=1)) / 3
    return pixels(source.astype(np.float32) + np.clip(fibers, -4, 4)[:, :, None])


def toner_dust(source: Pixels, params: ScanParameters) -> Pixels:
    rng = np.random.default_rng(params.seed + 2)
    height, width = source.shape[:2]
    mask = rng.random((height, width)) < 0.00065
    values = source.astype(np.float32)
    values[mask] -= rng.uniform(35, 100, (int(mask.sum()), 1))
    for _ in range(min(12, max(1, height * width // 180000))):
        y, x = int(rng.integers(0, height)), int(rng.integers(0, width))
        values[y : y + 2, x : x + 3] -= rng.uniform(45, 95)
    return pixels(values)


def scanner_marks(source: Pixels, params: ScanParameters) -> Pixels:
    height, width = source.shape[:2]
    rng = random.Random(params.seed + 3)
    values = source.astype(np.float32)
    y, x = np.ogrid[:height, :width]
    if params.artifact == 'streak':
        center = int(rng.uniform(0.2, 0.8) * height)
        values[center : center + rng.randint(1, 2)] -= rng.uniform(5, 10)
    elif params.artifact == 'fold':
        center = rng.uniform(0.4, 0.65) * width
        values -= (7 * np.exp(-(((x - center - y * 0.012) / 9) ** 2)))[:, :, None]
    elif params.artifact == 'holes':
        for center in (0.25 * height, 0.75 * height):
            distance = np.sqrt((x - width * 0.025) ** 2 + (y - center) ** 2)
            values -= (36 * np.exp(-(((distance - 10) / 3) ** 2)) + 18 * (distance < 10))[:, :, None]
    elif params.artifact == 'staple':
        distance = np.abs(y - height * 0.035 - 0.3 * (x - width * 0.03))
        mark = (distance < 2) & (x > width * 0.025) & (x < width * 0.055)
        values -= mark[:, :, None] * 65
    return pixels(values)


def soften_ink(source: Pixels, params: ScanParameters) -> Pixels:
    original = Image.fromarray(source)
    bled = Image.blend(original, original.filter(ImageFilter.MinFilter(3)), params.bleed)
    return np.asarray(bled.filter(ImageFilter.GaussianBlur(params.blur))).copy()


def page_transform(image: Image.Image, params: ScanParameters) -> Image.Image:
    """Shrink around the canvas center, then apply the scanner's rigid motion."""
    width, height = image.size
    scaled = (round(width * params.scale), round(height * params.scale))
    layer = Image.new(image.mode, image.size, 0)
    layer.paste(image.resize(scaled, Image.Resampling.BICUBIC), ((width - scaled[0]) // 2, (height - scaled[1]) // 2))
    offset = (round(params.offset[0] * width / PAGE_SIZE[0]), round(params.offset[1] * height / PAGE_SIZE[1]))
    return layer.rotate(params.rotation, resample=Image.Resampling.BICUBIC, translate=offset, fillcolor=0)


def scan_geometry(source: Pixels, params: ScanParameters) -> Pixels:
    """A slightly uneven paper silhouette above a dark bed and a soft edge shadow."""
    height, width = source.shape[:2]
    rng = random.Random(params.seed + 5)
    edge = max(0.3, 1.8 * width / PAGE_SIZE[0])
    points = []
    for x in np.linspace(0, width - 1, 40):
        points.append((float(x), rng.uniform(0, edge)))
    for y in np.linspace(0, height - 1, 50):
        points.append((width - 1 - rng.uniform(0, edge), float(y)))
    for x in np.linspace(width - 1, 0, 40):
        points.append((float(x), height - 1 - rng.uniform(0, edge)))
    for y in np.linspace(height - 1, 0, 50):
        points.append((rng.uniform(0, edge), float(y)))
    mask = Image.new('L', (width, height), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    sheet = Image.fromarray(source).convert('RGBA')
    sheet.putalpha(mask)
    sheet = page_transform(sheet, params)
    shadow = Image.new('L', (width, height), 0)
    shadow.paste(
        sheet.getchannel('A'), (max(1, round(5 * width / PAGE_SIZE[0])), max(1, round(7 * height / PAGE_SIZE[1])))
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(0.7, 4 * width / PAGE_SIZE[0])))
    shadow = shadow.point([round(value * params.shadow) for value in range(256)])
    bed = Image.new('RGBA', (width, height), (87, 88, 90, 255))
    bed.paste((18, 18, 19, 255), (0, 0), shadow)
    bed.alpha_composite(sheet)
    return np.asarray(bed.convert('RGB')).copy()


def scan_color(source: Pixels, params: ScanParameters) -> Pixels:
    if params.grayscale:
        gray = np.asarray(Image.fromarray(source).convert('L')).astype(np.float32)
        # Photocopy curve: slightly crushed blacks while keeping light paper.
        copied = pixels(np.where(gray < 128, (gray - 8) * 1.04, 128 + (gray - 128) * 1.015))
        return np.repeat(copied[:, :, None], 3, axis=2)
    return source.copy()


# Pure RGB uint8 -> RGB uint8 stages. Every RNG is local to the page seed;
# JPEG compression is the final bounded stage in render_document.
SCAN_EFFECTS = (uneven_ink, paper_tone, paper_grain, toner_dust, scanner_marks, soften_ink, scan_geometry, scan_color)


def prepare_page(
    document: Document,
    page: Page,
    number: int,
    total: int,
    fonts: Fonts,
    params: ScanParameters,
    identity: tuple[str, str],
) -> Page:
    """One drawing plan for typed reference text, annotations and actual pixels."""
    name, team = identity
    letter = document.fields['kind'] in LETTER_KINDS
    heading = 'letterhead' if letter else 'type_heading'
    scope = document.fields.get('station') or document.fields.get('program', '')
    header = []
    for text, y, style in [('Preflight Demo Program', 80, heading), (scope, 127, 'small')]:
        if text:
            x = round((PAGE_SIZE[0] - fonts[style].getlength(text)) / 2) if letter else LEFT
            header.append(Box(text, x, y, RIGHT - LEFT, 38, style))
    header.append(Box(f'{document.doc_id}    {document.fields["date"]}', LEFT, 187, RIGHT - LEFT, 30, 'small'))
    result = Page(boxes=header, rules=[(LEFT, 166, RIGHT, 166), *page.rules])
    rng = random.Random(params.seed + 6)
    for box in page.boxes:
        if box.style == 'hand':
            size = round(fonts['hand'].size) + rng.randint(-2, 2)
            while (
                size > fonts['hand'].size - 2 and fonts['hand'].font_variant(size=size).getlength(box.text) > box.width
            ):
                size -= 1
            box = replace(box, y=box.y + rng.randint(-2, 3), font_size=size)
        result.boxes.append(box)
    if params.handwriting:
        signature = name.replace('Demo Worker ', 'D. Worker ', 1)
        result.boxes.append(Box(signature, LEFT + 8, 1470, 440, 43, 'hand', 36))
        result.rules.append((LEFT, 1515, LEFT + 340, 1515))
    result.boxes.append(Box(f'{name}, {team}', LEFT, 1530, RIGHT - LEFT, 28, 'small'))
    result.boxes.append(Box(f'Page {number} of {total}', LEFT, 1588, RIGHT - LEFT, 28, 'small'))
    if params.margin_note:
        result.boxes.append(Box(rng.choice(CLERICAL_NOTES), 48, 560, 350, 45, 'hand', 32, 90))
    if params.stamp and number == 1:
        y = 1460
        if document.fields['kind'] == 'form':
            hand_cells = [box for box in page.boxes if box.style == 'hand']
            if hand_cells:
                blank = [box for box in hand_cells if not box.text]
                target = (blank or hand_cells)[-1]
                y = target.y + target.height - 8
        result.stamps.append(Stamp(params.stamp, document.fields['date'], RIGHT - 275, y, rng.uniform(-12, -6)))
    return result


def stamp_image(stamp: Stamp, fonts: Fonts) -> Image.Image:
    image = Image.new('RGBA', (270, 94))
    draw = ImageDraw.Draw(image)
    ink = (143, 32, 38, 120)
    draw.rectangle((5, 5, 264, 88), outline=ink, width=3)
    draw.text((135, 14), stamp.text, font=fonts['stamp'], fill=ink, anchor='mt')
    draw.text((135, 54), stamp.date, font=fonts['small'], fill=ink, anchor='mt')
    return image.rotate(stamp.angle, resample=Image.Resampling.BICUBIC, expand=True)


def draw_page(page: Page, fonts: Fonts) -> Pixels:
    image = Image.new('RGB', PAGE_SIZE, 'white')
    draw = ImageDraw.Draw(image)
    for rule in page.rules:
        draw.line(rule, fill=(100, 100, 100), width=1)
    for box in page.boxes:
        font = fonts[box.style].font_variant(size=box.font_size) if box.font_size else fonts[box.style]
        color = (28, 42, 64) if box.style == 'hand' else (22, 22, 22)
        if box.angle:
            note = Image.new('RGBA', (box.width, box.height))
            ImageDraw.Draw(note).text((0, 0), box.text, font=font, fill=(*color, 215), anchor='lt')
            note = note.rotate(box.angle, expand=True)
            image.paste(note, (box.x, box.y), note)
        else:
            draw.text((box.x, box.y), box.text, font=font, fill=color, anchor='lt')
    for stamp in page.stamps:
        overlay = stamp_image(stamp, fonts)
        image.paste(overlay, (stamp.x, stamp.y), overlay)
    return np.asarray(image).copy()


def scan_region(bounds: tuple[int, int, int, int], params: ScanParameters) -> list[int]:
    """Map an annotation rectangle through the same shrink/rotation as its pixels."""
    width, height = PAGE_SIZE
    scaled = (round(width * params.scale), round(height * params.scale))
    left, top = (width - scaled[0]) // 2, (height - scaled[1]) // 2
    angle = math.radians(params.rotation)
    points = []
    for x, y in ((bounds[0], bounds[1]), (bounds[2], bounds[1]), (bounds[0], bounds[3]), (bounds[2], bounds[3])):
        dx, dy = x * scaled[0] / width + left - width / 2, y * scaled[1] / height + top - height / 2
        points.append(
            (
                width / 2 + dx * math.cos(angle) + dy * math.sin(angle) + params.offset[0],
                height / 2 - dx * math.sin(angle) + dy * math.cos(angle) + params.offset[1],
            )
        )
    return [
        max(0, math.floor(min(p[0] for p in points)) - 2),
        max(0, math.floor(min(p[1] for p in points)) - 2),
        min(width, math.ceil(max(p[0] for p in points)) + 2),
        min(height, math.ceil(max(p[1] for p in points)) + 2),
    ]


def excluded_regions(page: Page, fonts: Fonts, params: ScanParameters) -> list[list[int]]:
    regions = []
    for box in page.boxes:
        if box.style == 'hand' and box.text:
            width, height = (box.height, box.width) if box.angle else (box.width, box.height)
            regions.append(scan_region((box.x, box.y, box.x + width, box.y + height), params))
    for stamp in page.stamps:
        width, height = stamp_image(stamp, fonts).size
        regions.append(scan_region((stamp.x, stamp.y, stamp.x + width, stamp.y + height), params))
    return regions


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def render_document(
    path: Path,
    output: Path,
    spec_seed: int,
    fonts: Fonts | None = None,
    *,
    synthetic: Path = ROOT / 'data/synthetic',
) -> dict[str, Any]:
    document = read_document(path)
    fonts = load_fonts() if fonts is None else fonts
    seed = spec_seed + int(document.doc_id[4:])
    base = scan_parameters(seed)
    identity = author_identity(document, synthetic)
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        'doc_id': document.doc_id,
        'kind': document.fields['kind'],
        'renderer_version': RENDERER_VERSION,
        'seed': seed,
        'spec_seed': spec_seed,
        'source_sha256': document.source_sha256,
        'author_display_name': identity[0],
        'team_display_name': identity[1],
        'pages': [],
    }
    pages = layout_document(document, fonts)
    for number, page in enumerate(pages, start=1):
        params = replace(
            scan_parameters(seed + (number - 1) * 1000003),
            grayscale=base.grayscale,
            handwriting=base.handwriting,
            stamp=base.stamp,
        )
        page = prepare_page(document, page, number, len(pages), fonts, params, identity)
        array = draw_page(page, fonts)
        for effect in SCAN_EFFECTS:
            array = effect(array, params)
        filename = f'{document.doc_id}-p{number}.jpg'
        image = Image.fromarray(array)
        if params.grayscale:
            image = image.convert('L')
        image.save(output / filename, 'JPEG', quality=params.quality, dpi=(150, 150), optimize=False, progressive=False)
        manifest['pages'].append(
            {
                'page_number': number,
                'file': filename,
                'sha256': hashlib.sha256((output / filename).read_bytes()).hexdigest(),
                'printed_reference': '\n'.join(box.text for box in page.boxes if box.style != 'hand' and box.text),
                'excluded_regions': excluded_regions(page, fonts, params),
                'scan_parameters': asdict(params),
            }
        )
    # An earlier, longer rendering must not leave extra pages for later OCR.
    expected = {page['file'] for page in manifest['pages']}
    for old in output.glob(f'{document.doc_id}-p*.jpg'):
        if re.fullmatch(r'DOC-[0-9]{4}-p[1-9][0-9]*\.jpg', old.name) and old.name not in expected:
            old.unlink()
    write_json(output / f'{document.doc_id}.json', manifest)
    return manifest


def selected_ids(only: str | None) -> set[str] | None:
    if only is None:
        return None
    ids = {item.strip() for item in only.split(',')}
    if not ids or any(not re.fullmatch(r'DOC-[0-9]{4}', ident) for ident in ids):
        raise ValueError('--only requires comma-separated document IDs such as DOC-0001')
    return ids


def select_documents(documents: Path, only: str | None) -> list[Path]:
    ids = selected_ids(only)
    paths = sorted(documents.glob('DOC-*.md'))
    if ids is not None:
        if ids - {path.stem for path in paths}:
            raise ValueError('Selected document is missing')
        paths = [path for path in paths if path.stem in ids]
    if not paths:
        raise ValueError('No document files found')
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=Path, default=ROOT / 'data/documents')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/scans')
    parser.add_argument(
        '--synthetic', type=Path, default=ROOT / 'data/synthetic', help='Runtime people.csv and teams.csv'
    )
    parser.add_argument('--only', help='Comma-separated document IDs')
    args = parser.parse_args()
    try:
        spec = json.loads((args.documents / 'spec.json').read_text(encoding='utf-8'))
        if type(spec['seed']) is not int or spec['seed'] < 0:
            raise ValueError('Specification seed must be a nonnegative integer')
        paths = select_documents(args.documents, args.only)
        fonts = load_fonts()
        for path in paths:
            manifest = render_document(path, args.output, spec['seed'], fonts, synthetic=args.synthetic)
            print(f'{manifest["doc_id"]}: rendered {len(manifest["pages"])} page(s), seed {manifest["seed"]}')
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        print(f'Render failed: {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
