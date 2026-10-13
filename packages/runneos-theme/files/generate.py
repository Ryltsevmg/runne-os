#!/usr/bin/env python3
"""Generate preview assets from one data-only palette; never apply user settings."""
import argparse
import json
from pathlib import Path
import re


ROLES = {'background', 'surface', 'raised', 'highest', 'text', 'muted', 'border',
         'accent', 'accentText', 'link', 'focus', 'selected', 'error', 'warning', 'success'}


def luminance(color):
    rgb = [int(color[i:i+2], 16)/255 for i in (1, 3, 5)]
    linear = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in rgb]
    return sum(a*b for a, b in zip(linear, (.2126, .7152, .0722)))


def contrast(a, b):
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light+.05)/(dark+.05)


def validate(data):
    if data.get('schemaVersion') != 1 or set(data.get('modes', {})) != {'carbon', 'cloud'}:
        raise ValueError('Unsupported theme schema')
    for mode, colors in data['modes'].items():
        if set(colors) != ROLES or not all(re.fullmatch(r'#[0-9a-fA-F]{6}', c) for c in colors.values()):
            raise ValueError('Invalid color roles: '+mode)
        for foreground in ('text', 'muted', 'link', 'error', 'warning', 'success'):
            for background in ('background', 'surface', 'raised', 'highest', 'selected'):
                if contrast(colors[foreground], colors[background]) < 4.5:
                    raise ValueError(f'Insufficient text contrast: {mode} {foreground}/{background}')
        if contrast(colors['accentText'], colors['accent']) < 4.5:
            raise ValueError('Insufficient button contrast')
        if contrast(colors['focus'], colors['surface']) < 3:
            raise ValueError('Insufficient focus contrast')


def css(data):
    validate(data)
    result = []
    for mode, colors in data['modes'].items():
        result.append(f':root[data-theme="{mode}"] {{')
        for name, value in colors.items():
            name = re.sub(r'[A-Z]', lambda m: '-'+m[0].lower(), name)
            result.append(f'  --{name}: {value};')
        result.append('  color-scheme: '+('dark' if mode == 'carbon' else 'light')+';\n}')
    return '\n'.join(result)+'\n'


def dms(data):
    validate(data)
    result = {}
    for mode, c in data['modes'].items():
        result['dark' if mode == 'carbon' else 'light'] = {
            'name': 'RunneOS '+mode.title(), 'primary': c['link'], 'primaryText': c['background'],
            'primaryContainer': c['selected'], 'secondary': c['link'], 'surface': c['surface'],
            'surfaceText': c['text'], 'surfaceVariant': c['raised'], 'surfaceVariantText': c['muted'],
            'surfaceTint': c['link'], 'background': c['background'], 'backgroundText': c['text'],
            'outline': c['border'], 'surfaceContainer': c['surface'], 'surfaceContainerHigh': c['raised'],
            'surfaceContainerHighest': c['highest'], 'error': c['error'], 'warning': c['warning'],
            'info': c['link'], 'matugen_type': 'scheme-monochrome'
        }
        # DMS uses primaryText on primary backgrounds, unlike CSS accentText.
        result['dark' if mode == 'carbon' else 'light']['primaryText'] = '#080808' if mode == 'carbon' else '#FFFFFF'
    return result


def generate(source, output):
    data = json.loads(source.read_text())
    validate(data)
    output.mkdir(parents=True, exist_ok=True)
    (output/'tokens.css').write_text(css(data))
    (output/'dms-theme.json').write_text(json.dumps(dms(data), indent=2)+'\n')
    for mode, colors in data['modes'].items():
        # Palette-only fragment: deliberately no command, keybind or include.
        (output/f'ghostty-{mode}.conf').write_text(''.join(
            name+' = '+colors[role].lstrip('#')+'\n' for name, role in
            [('background', 'background'), ('foreground', 'text'), ('cursor-color', 'link'),
             ('selection-background', 'selected'), ('selection-foreground', 'text')]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tokens', type=Path, default=Path(__file__).with_name('tokens.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate(args.tokens, args.output)
