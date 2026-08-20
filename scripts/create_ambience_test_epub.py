"""
Creates a small test EPUB for the multi-voice + ambience pipeline: two
distinct characters (Mara, Tobias) having a conversation during a clear,
explicitly-described rainstorm.

Run:
    python scripts/create_ambience_test_epub.py
Output:
    test_ambience.epub
"""

import zipfile

OUT = "test_ambience.epub"

CHAPTER_TITLE = "Chapter One: The Storm"
CHAPTER_BODY = """
<p>Rain hammered the tin roof of the cabin, a steady drumming that had not let up in an hour.
Water sheeted down the single window, blurring the dark shapes of the pines outside.</p>

<p>Mara pressed her palm against the cold glass. "It's not stopping," she said. "Listen to it."</p>

<p>Tobias looked up from the fire he was building in the small iron stove. "Rivers like this
don't stop for anyone," he said. "We ride it out till morning."</p>

<p>"You said that two hours ago," Mara replied, turning from the window. Thunder rolled somewhere
over the ridge, and the downpour outside seemed to answer it, hissing harder against the roof.</p>

<p>"And I'll say it again in two more," Tobias said. He struck a match and the stove caught,
casting a thin orange light across the room. "The storm will pass. It always does."</p>

<p>Mara sat down across from him, listening to the rain beat against the walls. "I hope you're
right," she said quietly.</p>

<p>"I usually am," Tobias said, and for the first time that night, he smiled.</p>
"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>The Storm — Ambience Test</dc:title>
    <dc:creator>ScrollTone Test</dc:creator>
    <dc:identifier id="bookid">test-ambience-001</dc:identifier>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="chapter1" href="chapters/chapter1.html" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="chapter1"/>
  </spine>
</package>"""

NCX = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="test-ambience-001"/></head>
  <docTitle><text>The Storm</text></docTitle>
  <navMap>
    <navPoint id="nav1" playOrder="1">
      <navLabel><text>{CHAPTER_TITLE}</text></navLabel>
      <content src="chapters/chapter1.html"/>
    </navPoint>
  </navMap>
</ncx>"""

CHAPTER_HTML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{CHAPTER_TITLE}</title></head>
<body>
<h1>{CHAPTER_TITLE}</h1>
{CHAPTER_BODY}
</body>
</html>"""

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    zf.writestr("META-INF/container.xml", CONTAINER_XML)
    zf.writestr("OEBPS/content.opf", OPF)
    zf.writestr("OEBPS/toc.ncx", NCX)
    zf.writestr("OEBPS/chapters/chapter1.html", CHAPTER_HTML)

print(f"Created: {OUT}")
print("  Characters: Mara (F), Tobias (M)")
print("  Ambient cue: rain (explicit, sustained)")
