"""Shared Runne GTK styling for our applications, not a global GTK override."""
import json
import os
from pathlib import Path
import re

HERE=Path(__file__).resolve().parent
ROLES={'background','surface','raised','highest','text','muted','border','accent',
       'accentText','link','focus','selected','error','warning','success'}


def validate_colors(colors):
    if set(colors)!=ROLES or not all(isinstance(c,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',c) for c in colors.values()):
        raise ValueError('Invalid Runne palette')


def current_mode():
    root=Path(os.environ.get('XDG_CACHE_HOME',str(Path.home()/'.cache')))
    try:
        data=json.loads((root/'DankMaterialShell/dms-colors.json').read_text())
        return 'cloud' if data.get('mode')=='light' else 'carbon'
    except (OSError,ValueError,TypeError):return 'carbon'


def css(colors,extra=''):
    validate_colors(colors)
    mapping={'BG':'background','SURFACE':'surface','RAISED':'raised','TEXT':'text','MUTED':'muted',
             'ACCENT':'link','ACCENTBG':'accent','BORDER':'border','FOCUS':'focus'}
    for token,role in mapping.items():extra=extra.replace('@'+token+'@',colors[role])
    return f'''
@define-color accent_bg_color {colors['accent']};
@define-color accent_fg_color {colors['accentText']};
@define-color accent_color {colors['link']};
window {{background:{colors['background']};color:{colors['text']};font-family:Inter,"Noto Sans",sans-serif;}}
headerbar {{background:{colors['surface']};box-shadow:none;}}
.dim-label,.muted,.subtitle {{opacity:1;color:{colors['muted']};}}
.title-1 {{font-size:26px;font-weight:600;letter-spacing:-0.5px;}}
.title-2 {{font-size:20px;font-weight:600;}}
.title-3 {{font-size:16px;font-weight:600;}}
button {{border-radius:8px;}}
button.pill {{border-radius:8px;}}
button.suggested-action {{background:{colors['accent']};color:{colors['accentText']};border-radius:6px;}}
entry,searchentry {{background:{colors['highest']};color:{colors['text']};border:1px solid {colors['border']};border-radius:8px;min-height:34px;}}
label.warning {{color:{colors['warning']};}}
label.error {{color:{colors['error']};}}
button:focus-visible,entry:focus-visible,searchentry:focus-visible {{outline:2px solid {colors['focus']};outline-offset:2px;}}
.account {{padding:20px;border-radius:12px;background:{colors['surface']};border:1px solid {colors['border']};}}
.metric {{font-family:"JetBrains Mono",monospace;font-size:26px;font-weight:600;}}
.app-card .muted {{font-weight:400;}}
progressbar progress {{background:{colors['accent']};}}
progressbar trough {{background:{colors['highest']};min-height:5px;border-radius:3px;}}
.boxed-list {{background:{colors['surface']};border:1px solid {colors['border']};border-radius:12px;box-shadow:none;}}
'''+extra


_WORDMARK_TYPE=None


def wordmark(width=156):
    global _WORDMARK_TYPE
    from gi.repository import Gtk
    if _WORDMARK_TYPE is None:
        try:
            class SizedWordmark(Gtk.Picture):
                __gtype_name__='RunneWordmarkPicture'
                def do_measure(self,orientation,for_size):
                    width=getattr(self,'brand_width',156)
                    size=width if orientation==Gtk.Orientation.HORIZONTAL else max(24,round(width*144/616))
                    return size,size,-1,-1
            _WORDMARK_TYPE=SizedWordmark
        except RuntimeError:
            # This module can be imported more than once in one process (for
            # example the test suite loads it per test file). The GType name is
            # global, so the second registration fails — reuse the type that is
            # already registered instead of crashing.
            from gi.repository import GObject
            _WORDMARK_TYPE=GObject.type_from_name('RunneWordmarkPicture').pytype
    picture=_WORDMARK_TYPE(content_fit=Gtk.ContentFit.CONTAIN,can_shrink=True)
    picture.brand_width=width
    picture.set_halign(Gtk.Align.START)
    picture.set_alternative_text('Runne.OS')
    return picture


class Theme:
    def __init__(self,window,extra_css='',wordmarks=(),on_change=None):
        from gi.repository import Gtk,Adw,GLib,Gdk
        self.Gtk=Gtk;self.Adw=Adw;self.GLib=GLib;self.Gdk=Gdk
        self.extra=extra_css;self.wordmarks=list(wordmarks);self.signature=None;self.provider=None
        self.on_change=on_change
        self.refresh()
        self.timer=GLib.timeout_add_seconds(2,self.refresh)
        window.connect('unrealize',self.stop)

    def refresh(self):
        try:
            data=json.loads((HERE/'tokens.json').read_text());mode=current_mode();colors=data['modes'][mode]
            validate_colors(colors);signature=json.dumps((mode,colors),sort_keys=True)
            if signature==self.signature:return True
            provider=self.Gtk.CssProvider();provider.load_from_string(css(colors,self.extra))
            display=self.Gdk.Display.get_default()
            if self.provider:self.Gtk.StyleContext.remove_provider_for_display(display,self.provider)
            self.provider=provider;self.Gtk.StyleContext.add_provider_for_display(display,provider,600)
            self.Adw.StyleManager.get_default().set_color_scheme(self.Adw.ColorScheme.FORCE_DARK if mode=='carbon' else self.Adw.ColorScheme.FORCE_LIGHT)
            for picture in self.wordmarks:picture.set_filename(str(HERE/'brand'/f'wordmark-{mode}.png'))
            self.signature=signature
            if self.on_change:self.on_change(mode)
        except (OSError,ValueError,KeyError,TypeError):pass
        return True

    def stop(self,*_):
        if getattr(self,'timer',None):self.GLib.source_remove(self.timer);self.timer=None
