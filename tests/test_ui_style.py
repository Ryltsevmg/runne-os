import importlib.util
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

FILES=Path(__file__).parents[1]/'packages/runneos-theme/files'
spec=importlib.util.spec_from_file_location('ui_style',FILES/'ui_style.py')
style=importlib.util.module_from_spec(spec);spec.loader.exec_module(style)


class SharedStyleTests(unittest.TestCase):
    def setUp(self):self.tokens=json.loads((FILES/'tokens.json').read_text())

    def test_controls_keep_cobalt_and_readable_foreground(self):
        for mode,colors in self.tokens['modes'].items():
            css=style.css(colors)
            self.assertIn('@define-color accent_bg_color #315CFF;',css)
            self.assertIn('@define-color accent_fg_color #FFFFFF;',css)
            self.assertIn('headerbar',css)

    def test_secondary_text_not_faded_below_contrast(self):
        self.assertIn('.dim-label,.muted,.subtitle {opacity:1;',style.css(self.tokens['modes']['carbon']))

    def test_template_uses_shared_role_values(self):
        colors=self.tokens['modes']['cloud']
        self.assertIn('color:'+colors['muted'],style.css(colors,'x {color:@MUTED@;}'))

    def test_malicious_colors_are_rejected(self):
        colors=dict(self.tokens['modes']['carbon'],text='url(file:///etc/passwd)')
        with self.assertRaises(ValueError):style.css(colors)

    def test_mode_follows_dms_and_has_safe_missing_fallback(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'XDG_CACHE_HOME':root}):
            self.assertEqual(style.current_mode(),'carbon')
            path=Path(root)/'DankMaterialShell/dms-colors.json';path.parent.mkdir();path.write_text('{"mode":"light"}')
            self.assertEqual(style.current_mode(),'cloud')

    def test_original_asset_provenance_and_hashes(self):
        provenance=json.loads((FILES/'brand/provenance.json').read_text())
        self.assertEqual(provenance['commit'],'b5bfb915c978f3b575f74df6ac6540055a0804aa')
        for row in provenance['exports']:
            self.assertEqual(hashlib.sha256((FILES/'brand'/row['file']).read_bytes()).hexdigest(),row['sha256'])

    def test_os_wordmarks_are_transparent_and_keep_approved_separator_colors(self):
        import gi
        gi.require_version('GdkPixbuf','2.0')
        from gi.repository import GdkPixbuf
        for mode,rgb in [('carbon',(124,138,255)),('cloud',(64,85,245))]:
            image=GdkPixbuf.Pixbuf.new_from_file(str(FILES/'brand'/f'wordmark-{mode}.png'))
            self.assertEqual((image.get_width(),image.get_height()),(1848,432))
            self.assertTrue(image.get_has_alpha())
            pixels=image.get_pixels();self.assertEqual(pixels[3],0)
            offset=234*image.get_rowstride()+1230*image.get_n_channels()
            self.assertEqual(tuple(pixels[offset:offset+4]),rgb+(255,))

    def test_gtk_css_parses_both_palettes(self):
        import gi
        gi.require_version('Gtk','4.0')
        from gi.repository import Gtk
        for colors in self.tokens['modes'].values():
            provider=Gtk.CssProvider();errors=[]
            provider.connect('parsing-error',lambda p,s,e:errors.append(str(e)))
            provider.load_from_string(style.css(colors))
            self.assertEqual(errors,[])

    def test_logo_natural_png_size_does_not_expand_sidebar(self):
        import gi
        gi.require_version('Gtk','4.0')
        from gi.repository import Gtk
        for width in (124,158):
            logo=style.wordmark(width);logo.set_filename(str(FILES/'brand/wordmark-carbon.png'))
            measure=logo.measure(Gtk.Orientation.HORIZONTAL,-1)
            self.assertEqual(measure[0],width)
            self.assertEqual(measure[1],width)
            self.assertEqual(logo.get_alternative_text(),'Runne.OS')
            self.assertEqual(logo.measure(Gtk.Orientation.VERTICAL,-1)[1],max(24,round(width*144/616)))


if __name__=='__main__':unittest.main()
