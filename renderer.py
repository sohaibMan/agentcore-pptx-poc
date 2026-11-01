"""Trusted renderer executed inside AgentCore, not on the web server.

python-pptx is preinstalled in the managed Code Interpreter environment.
"""
from io import BytesIO


def render(deck):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    prs.core_properties.title = deck['title']
    navy, teal, white, ink = '162A35', '007F78', 'FFFFFF', '243640'

    def text(slide, value, x, y, w, h, size, color, bold=False):
        box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = 0
        for i, line in enumerate(value.split('\n')):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = line
            p.font.name, p.font.size = 'Arial', Pt(size)
            p.font.bold, p.font.color.rgb = bold, RGBColor.from_string(color)
        return box

    cover = prs.slides.add_slide(prs.slide_layouts[6])
    cover.background.fill.solid()
    cover.background.fill.fore_color.rgb = RGBColor.from_string(navy)
    text(cover, 'PRESENTATION', .8, .65, 10, .4, 14, '66D8C6', True)
    text(cover, deck['title'], .8, 1.7, 11.5, 2.4, 40, white, True)
    text(cover, deck.get('subtitle', ''), .8, 4.6, 11, 1.3, 20, white)
    for number, item in enumerate(deck['slides'], 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(white)
        text(slide, item['title'], .7, .55, 11.9, 1.25, 32, navy, True)
        points = item['bullets']
        # Up to four editable cards; fixed bounds keep model output on canvas.
        for i, point in enumerate(points):
            x, y = .7 + (i % 2) * 6.1, 2.05 + (i // 2) * 2.15
            card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                         Inches(x), Inches(y), Inches(5.8), Inches(1.85))
            card.fill.solid()
            card.fill.fore_color.rgb = RGBColor.from_string('EDF5F4')
            card.line.fill.background()
            text(slide, f'{i + 1:02}', x + .25, y + .2, .6, .45, 20, teal, True)
            text(slide, point, x + 1, y + .2, 4.5, 1.5, 18, ink)
        text(slide, f'{number:02} / {len(deck["slides"]):02}', 11.5, 7, 1.1, .25, 10, ink)
        slide.notes_slide.notes_text_frame.text = item.get('notes', '')
    output = BytesIO()
    prs.save(output)
    return output.getvalue()
