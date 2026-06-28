"""
Creates a test EPUB file with multiple characters and dialogue for testing
the multi-voice speaker attribution feature.

Characters:
  Sarah   — female, protagonist
  James   — male, detective
  Victor  — male, antagonist
  Elena   — female, scientist

Run:
    python create_test_epub.py
Output:
    test_multivoice.epub
"""

import zipfile
import os

OUT = "test_multivoice.epub"

# ── Chapter content ────────────────────────────────────────────────────────────

CHAPTERS = [
    {
        "id":    "chapter1",
        "title": "Chapter One: The Letter",
        "body":  """
<p>The morning fog clung to the streets of Ashford when Sarah first noticed the envelope.
It was sitting on her kitchen table, though she was certain she had locked the door the night before.</p>

<p>She picked it up carefully, turning it over in her hands. There was no stamp, no return address.
Just her name written in a sharp, angular hand she did not recognise.</p>

<p>She tore it open and read the single line inside: <em>Meet me at the lighthouse. Come alone. Midnight.</em></p>

<p>"This is either the most exciting thing that's ever happened to me," she said aloud to her empty apartment,
"or the last."</p>

<p>She folded the note and slipped it into her coat pocket. Outside, the fog was beginning to lift.</p>

<p>Her phone buzzed. It was James.</p>

<p>"Sarah, I need you to come to the station," he said, his voice tight. "We found something in the harbour.
You're not going to like it."</p>

<p>"Can it wait until after lunch?" she asked.</p>

<p>"No," James said flatly. "It really cannot."</p>

<p>She grabbed her keys and headed for the door.</p>
""",
    },
    {
        "id":    "chapter2",
        "title": "Chapter Two: The Evidence",
        "body":  """
<p>The Ashford police station smelled of cold coffee and old paper. James met Sarah at the front desk,
his face drawn and pale beneath the fluorescent lights.</p>

<p>"Thank you for coming quickly," he said.</p>

<p>"You said it was urgent," Sarah replied. "What did you find?"</p>

<p>He led her through a narrow corridor to a room at the back. On the table lay a briefcase, its brass
locks corroded with seawater.</p>

<p>"We pulled this from the harbour this morning," James said. "Diver found it wedged under the old pier.
Inside we found documents, a passport, and this."</p>

<p>He slid a photograph across the table. Sarah leaned in and felt the blood leave her face.</p>

<p>"That's impossible," she whispered.</p>

<p>"I know," James said quietly. "I thought so too."</p>

<p>"Where did he go?" Sarah asked, straightening up. "After the harbour — where did Victor go?"</p>

<p>"That's what we don't know," James admitted. "He vanished three years ago. Everyone assumed he was dead."</p>

<p>"He's not dead," Sarah said. She pulled the letter from her pocket and placed it on the table.
"He left me this this morning."</p>

<p>James stared at the note for a long moment. Then he looked up at her with the expression of a man
who has just realised his evening is entirely ruined.</p>

<p>"You are absolutely not going to that lighthouse alone," he said.</p>

<p>"I wasn't planning to," Sarah said. "But I am going."</p>
""",
    },
    {
        "id":    "chapter3",
        "title": "Chapter Three: The Lighthouse",
        "body":  """
<p>Midnight found them on the cliff path, the beam of the lighthouse sweeping slow arcs across the water below.
James walked two steps behind Sarah, one hand resting on his coat pocket.</p>

<p>"I still think this is a terrible idea," he muttered.</p>

<p>"You've said that four times," Sarah replied.</p>

<p>"Because it is still true."</p>

<p>The lighthouse door was unlocked. They pushed inside and climbed the iron stairs to the lamp room.
A figure stood with his back to them, watching the sea.</p>

<p>"I expected you sooner," Victor said without turning around. His voice was lower than Sarah remembered,
roughed by years she had not witnessed.</p>

<p>"We took the long route," Sarah said. "Habit."</p>

<p>Victor turned. He looked older, harder, but the eyes were the same — pale grey and absolutely certain.</p>

<p>"You should not have brought the detective," he said.</p>

<p>"He invited himself," Sarah replied.</p>

<p>"I am standing right here," James said.</p>

<p>Victor studied him for a moment, then seemed to decide he was not worth the concern. He moved to the
small table in the centre of the room and opened a leather satchel.</p>

<p>"Three years ago I discovered something that certain people would prefer remain hidden," he said.
"I faked my death because it was the only way to keep working. What I found changes everything you
think you know about this town."</p>

<p>"Then tell us," Sarah said.</p>

<p>Victor hesitated. For the first time since they had entered, he looked uncertain.</p>

<p>"There is someone else," he said at last. "Someone who helped me. Without her, none of this
would have been possible."</p>

<p>A door behind the lamp opened and a woman stepped through. She was small and precise-looking,
with dark eyes that assessed the room in a single sweep.</p>

<p>"Hello, Sarah," Elena said. "I was wondering when you would finally arrive."</p>

<p>"Elena," Sarah said slowly. "You've been alive this whole time."</p>

<p>"Very much so," Elena said. She set a hard drive on the table beside Victor's satchel. "And I have
everything. Every document, every transaction, every name. All of it."</p>

<p>"Is it enough?" James asked.</p>

<p>"It is more than enough," Elena said. "The question is whether you are ready to use it."</p>

<p>Sarah looked at James. James looked at Sarah. Outside, the lighthouse beam swept across the dark water,
patient and indifferent.</p>

<p>"We're ready," Sarah said.</p>
""",
    },
]

# ── EPUB boilerplate ───────────────────────────────────────────────────────────

MIMETYPE = "application/epub+zip"

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

def make_opf(chapters):
    items = "\n".join(
        f'    <item id="{ch["id"]}" href="chapters/{ch["id"]}.html" media-type="application/xhtml+xml"/>'
        for ch in chapters
    )
    itemrefs = "\n".join(
        f'    <itemref idref="{ch["id"]}"/>'
        for ch in chapters
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>The Ashford Lighthouse — Multi-Voice Test</dc:title>
    <dc:creator>ScrollTone Test</dc:creator>
    <dc:identifier id="bookid">test-multivoice-001</dc:identifier>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
{items}
  </manifest>
  <spine toc="ncx">
{itemrefs}
  </spine>
</package>"""

def make_ncx(chapters):
    nav_points = "\n".join(
        f"""  <navPoint id="nav{i+1}" playOrder="{i+1}">
    <navLabel><text>{ch["title"]}</text></navLabel>
    <content src="chapters/{ch["id"]}.html"/>
  </navPoint>"""
        for i, ch in enumerate(chapters)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="test-multivoice-001"/></head>
  <docTitle><text>The Ashford Lighthouse</text></docTitle>
  <navMap>
{nav_points}
  </navMap>
</ncx>"""

def make_chapter_html(title, body):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>"""

# ── Build the EPUB ─────────────────────────────────────────────────────────────

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    # mimetype must be first and uncompressed
    zf.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE,
                compress_type=zipfile.ZIP_STORED)

    zf.writestr("META-INF/container.xml", CONTAINER_XML)
    zf.writestr("OEBPS/content.opf",      make_opf(CHAPTERS))
    zf.writestr("OEBPS/toc.ncx",          make_ncx(CHAPTERS))

    for ch in CHAPTERS:
        path = f"OEBPS/chapters/{ch['id']}.html"
        zf.writestr(path, make_chapter_html(ch["title"], ch["body"]))

print(f"Created: {OUT}")
print(f"  {len(CHAPTERS)} chapters")
print( "  Characters: Sarah (F), James (M), Victor (M), Elena (F)")
print( "  Upload to ScrollTone with Multi-voice enabled to test.")
