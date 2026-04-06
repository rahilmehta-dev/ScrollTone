"""
EPUB parsing utilities.

Responsibilities:
- Cover image extraction (4-strategy fallback)
- Chapter text + title extraction
- Book metadata (title, author)
"""
import re


def _find_epub_cover(book):
    """Return (cover_bytes, mime_type) using 4 fallback strategies.

    1. EPUB2 OPF  : <meta name="cover" content="item-id"/>
    2. EPUB3 OPF  : manifest item with properties="cover-image"
    3. Name/ID    : any image whose file name or id contains "cover"
    4. First image: first image found anywhere in the book
    """
    import ebooklib

    # Strategy 1: EPUB2 OPF meta name="cover"
    cover_id = None
    try:
        meta = book.get_metadata('OPF', 'cover')
        if meta:
            cover_id = (meta[0][1] or {}).get('content', '')
    except Exception:
        pass

    if cover_id:
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_IMAGE and item.get_id() == cover_id:
                data = item.get_content()
                if data:
                    return data, item.media_type or "image/jpeg"

    # Strategy 2: EPUB3 manifest properties="cover-image"
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            props = getattr(item, 'properties', '') or ''
            if isinstance(props, (list, tuple)):
                props = ' '.join(props)
            if 'cover-image' in props:
                data = item.get_content()
                if data:
                    return data, item.media_type or "image/jpeg"

    # Strategy 3: "cover" in file name or item id
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            name = (getattr(item, 'file_name', '') or item.get_name() or '').lower()
            iid  = (item.get_id() or '').lower()
            if 'cover' in name or 'cover' in iid:
                data = item.get_content()
                if data:
                    return data, item.media_type or "image/jpeg"

    # Strategy 4: first image in the book
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            data = item.get_content()
            if data:
                return data, item.media_type or "image/jpeg"

    return None, "image/jpeg"


def _build_toc_map(toc_items) -> dict:
    """Walk the EPUB TOC tree and return {basename → title}."""
    result = {}
    for item in toc_items:
        if isinstance(item, tuple):
            section, children = item
            href  = getattr(section, 'href', '') or ''
            title = getattr(section, 'title', '') or ''
        else:
            href  = getattr(item, 'href', '') or ''
            title = getattr(item, 'title', '') or ''
            children = []
        base = href.split('#')[0].split('/')[-1]
        if base and title.strip() and base not in result:
            result[base] = title.strip()
        if children:
            result.update(_build_toc_map(children))
    return result


def extract_chapters(book, min_len: int) -> list:
    """Return list of (title, text) tuples for chapters meeting the min length.

    Title priority: EPUB TOC (NCX/NAV) → first H1/H2/H3 heading → "Section N".
    Bare-number headings like "1." are normalised to "Chapter 1".
    """
    import ebooklib
    from bs4 import BeautifulSoup

    toc_map = _build_toc_map(book.toc)

    chapters = []
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), "html.parser")
            for tag in soup(["script", "style", "head"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()
            if len(text) >= min_len:
                # 1. EPUB TOC
                item_base = (getattr(item, 'file_name', None) or item.get_name() or '').split('/')[-1]
                title = toc_map.get(item_base)

                # 2. First heading in the HTML
                if not title:
                    tag = soup.find(["h1", "h2", "h3"])
                    title = tag.get_text().strip() if tag else None

                # 3. Fallback
                if not title:
                    title = f"Section {len(chapters) + 1}"

                # "1." / "2" / "42." → "Chapter 1" / "Chapter 2" / "Chapter 42"
                if re.match(r'^\d+\.?$', title.strip()):
                    title = "Chapter " + title.strip().rstrip(".")

                chapters.append((title, text))
    return chapters


def get_book_metadata(book) -> dict:
    """Return {'title': str, 'author': str} from Dublin Core metadata."""
    meta_t = book.get_metadata('DC', 'title')
    meta_c = book.get_metadata('DC', 'creator')
    return {
        "title":  meta_t[0][0] if meta_t else "",
        "author": meta_c[0][0] if meta_c else "",
    }
