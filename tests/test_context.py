import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'context', Path(__file__).parents[1] / 'packages/runneos-tools/files/context.py')
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


class ContextTests(unittest.TestCase):
    def test_missing_name_is_not_reported_as_server_down(self):
        # MagicDNS is maintained by Tailscale and NetworkManager can drop it.
        # A name that does not resolve here says nothing about the server, so it
        # must never be reported as "does not answer".
        import socket
        with patch.object(context, 'run', return_value=(0, '')), \
             patch.object(socket, 'getaddrinfo', side_effect=socket.gaierror(-2, 'Name or service not known')), \
             patch.object(Path, 'read_text', return_value='[runneos]\nServer = https://example.invalid/runneos/$arch\n'):
            section = context.repository_section()
        self.assertIsNone(section['reachable'])
        self.assertIn('не разрешается', section['detail'])

    def test_render_includes_repository_state(self):
        data = {
            'generated': 'now',
            'system': {'host': 'h', 'kernel': 'k', 'uptime': '1 ч', 'disk_free': '1 GiB'},
            'session': {'type': 'wayland', 'desktop': 'niri', 'niri': '1', 'dms': '2',
                        'services': {'niri.service': 'active'}},
            'packages': {'installed': 10, 'pending': 0, 'pending_list': []},
            'repository': {'configured': True, 'server': 'https://s/runneos/x86_64', 'reachable': True},
            'snapshots': {'available': True, 'last': '95'},
            'knowledge': {'found': True, 'statuses': {'проверено': 8}, 'entry': '/tmp/START.md'},
            'health': {'available': True, 'ok': 14, 'attention': [], 'unknown': ['Кнопки мыши']},
        }
        text = context.render(data)
        self.assertIn('отвечает', text)
        self.assertIn('проверено 8', text)

    def test_unknown_health_status_is_not_attention(self):
        data = {
            'generated': 'now',
            'system': {'host': 'h', 'kernel': 'k', 'uptime': '1 ч', 'disk_free': '1 GiB'},
            'session': {'type': 'w', 'desktop': 'n', 'niri': '1', 'dms': '2', 'services': {}},
            'packages': {'installed': 0, 'pending': 0, 'pending_list': []},
            'repository': {'configured': False, 'detail': 'нет'},
            'snapshots': {'available': False, 'detail': 'нужен root'},
            'knowledge': {'found': False, 'detail': 'нет индекса'},
            'health': {'available': True, 'ok': 14, 'attention': [], 'unknown': ['Кнопки мыши']},
        }
        self.assertNotIn('внимание', context.render(data))


if __name__ == '__main__':
    unittest.main()
