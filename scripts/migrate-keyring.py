#!/usr/bin/python3
"""Copy the unlocked KWallet Secret Service collection to GNOME Keyring.

Run as the desktop user after GNOME Keyring owns org.freedesktop.secrets and
KWallet is available as org.kde.secretservicecompat. Secret values stay in
memory; desktop prompts handle passwords. Originals are never deleted.
"""
import ctypes
import resource
import subprocess
import threading

import gi
gi.require_version('Secret', '1')
from gi.repository import Gio, GLib, Secret


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def service(name='org.freedesktop.secrets'):
    return Secret.Service.open_sync(Secret.Service.__gtype__, name,
        Secret.ServiceFlags.OPEN_SESSION | Secret.ServiceFlags.LOAD_COLLECTIONS, None)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def call(path, interface, method, params=None):
        return connection.call_sync('org.kde.secretservicecompat', path, interface,
            method, params, None, Gio.DBusCallFlags.NONE, 30000, None).unpack()
    def properties(path, interface):
        return call(path, 'org.freedesktop.DBus.Properties', 'GetAll',
                    GLib.Variant('(s)', (interface,)))[0]
    def prompt(path):
        if path == '/':return
        loop = GLib.MainLoop()
        result = {'dismissed':True}
        def completed(connection, sender, object_path, interface, signal, params):
            result['dismissed'] = params.unpack()[0]
            loop.quit()
        subscription = connection.signal_subscribe('org.kde.secretservicecompat',
            'org.freedesktop.Secret.Prompt','Completed',path,None,
            Gio.DBusSignalFlags.NONE,completed)
        timeout = GLib.timeout_add_seconds(300, lambda: (loop.quit(), False)[1])
        try:
            call(path,'org.freedesktop.Secret.Prompt','Prompt',GLib.Variant('(s)',('',)))
            loop.run()
            if result['dismissed']:raise RuntimeError('Old wallet unlock cancelled')
        finally:
            connection.signal_unsubscribe(subscription)
            if GLib.MainContext.default().find_source_by_id(timeout):GLib.source_remove(timeout)
    owner = connection.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus',
        'org.freedesktop.DBus','GetNameOwner',GLib.Variant('(s)',('org.kde.secretservicecompat',)),
        None,Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
    pid = connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
        'org.freedesktop.DBus', 'GetConnectionUnixProcessID',
        GLib.Variant('(s)', (owner,)), None,
        Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    from pathlib import Path
    if Path(f'/proc/{pid}/exe').resolve().name != 'ksecretd':
        raise RuntimeError('Expected KWallet as source; no changes made')
    rows = []
    _, session = call('/org/freedesktop/secrets','org.freedesktop.Secret.Service',
        'OpenSession',GLib.Variant('(sv)',('plain',GLib.Variant('s',''))))
    try:
        for path in properties('/org/freedesktop/secrets','org.freedesktop.Secret.Service')['Collections']:
            collection = properties(path,'org.freedesktop.Secret.Collection')
            if collection['Locked']:
                _, pending = call('/org/freedesktop/secrets','org.freedesktop.Secret.Service',
                    'Unlock',GLib.Variant('(ao)',([path],)))
                prompt(pending)
                collection = properties(path,'org.freedesktop.Secret.Collection')
                if collection['Locked']:raise RuntimeError('Old wallet remains locked')
            for item_path in collection['Items']:
                item = properties(item_path,'org.freedesktop.Secret.Item')
                _, _, raw, content_type = call(item_path,'org.freedesktop.Secret.Item',
                    'GetSecret',GLib.Variant('(o)',(session,)))[0]
                rows.append((item['Attributes'],item['Label'],bytes(raw),content_type))
    finally:
        call(session,'org.freedesktop.Secret.Session','Close')
    print(f'Source items loaded in memory: {len(rows)}', flush=True)
    try:
        target = service()
        if target.get_name_owner() == owner:
            raise RuntimeError('Secret Service owner did not change')
        cancel = Gio.Cancellable()
        timer = threading.Timer(300, cancel.cancel)
        timer.start()
        try:
            print('Enter your Linux login password in the new keyring dialog; do not use an empty password.', flush=True)
            destination = next((c for c in target.get_collections()
                                if c.get_object_path().endswith(('/Login', '/login'))), None)
            if destination is None:
                destination = Secret.Collection.create_sync(target, 'login', 'default',
                    Secret.CollectionCreateFlags.NONE, cancel)
            elif destination.get_locked():
                target.unlock_sync([destination], cancel)
            if destination is None:
                raise RuntimeError('Keyring creation cancelled')
            def target_call(path, interface, method, params=None):
                return connection.call_sync(target.get_name_owner(),path,interface,method,
                    params,None,Gio.DBusCallFlags.NONE,30000,cancel).unpack()
            _, target_session = target_call('/org/freedesktop/secrets','org.freedesktop.Secret.Service',
                'OpenSession',GLib.Variant('(sv)',('plain',GLib.Variant('s',''))))
            for attributes, label, raw, content_type in rows:
                props = {'org.freedesktop.Secret.Item.Label':GLib.Variant('s',label),
                         'org.freedesktop.Secret.Item.Attributes':GLib.Variant('a{ss}',attributes)}
                path, pending = target_call(destination.get_object_path(),'org.freedesktop.Secret.Collection',
                    'CreateItem',GLib.Variant('(a{sv}(oayays)b)',
                        (props,(target_session,[],list(raw),content_type),True)))
                if pending != '/':raise RuntimeError('Unexpected item prompt')
                # Replace may keep attributes added by a previous libsecret
                # client (notably xdg:schema). Restore the source set exactly.
                target_call(path,'org.freedesktop.DBus.Properties','Set',
                    GLib.Variant('(ssv)',('org.freedesktop.Secret.Item','Attributes',
                                         GLib.Variant('a{ss}',attributes))))
                stored = target_call(path,'org.freedesktop.DBus.Properties','GetAll',
                    GLib.Variant('(s)',('org.freedesktop.Secret.Item',)))[0]
                _, _, copied, copied_type = target_call(path,'org.freedesktop.Secret.Item','GetSecret',
                    GLib.Variant('(o)',(target_session,)))[0]
                actual_attributes = dict(stored['Attributes'])
                # GNOME adds a schema marker when the source did not have one;
                # every original lookup attribute must still match exactly.
                if 'xdg:schema' not in attributes:actual_attributes.pop('xdg:schema',None)
                checks = {'attributes':actual_attributes == attributes,
                          'value':bytes(copied) == raw,
                          'content_type':copied_type == content_type or
                              (content_type == 'application/octet-stream' and copied_type == 'text/plain')}
                if content_type != copied_type and checks['content_type']:
                    print('GNOME normalized MIME to text/plain; bytes verified unchanged.',flush=True)
                if not all(checks.values()):
                    print('Verification fields:', checks, flush=True)
                    if not checks['content_type']:
                        print('MIME normalization:',repr(content_type),repr(copied_type),flush=True)
                    raise RuntimeError('Copy verification failed')
            target_call(target_session,'org.freedesktop.Secret.Session','Close')
            current = target_call(destination.get_object_path(),'org.freedesktop.DBus.Properties',
                'GetAll',GLib.Variant('(s)',('org.freedesktop.Secret.Collection',)))[0]
            if len(current['Items']) != len(rows):
                raise RuntimeError('Destination count mismatch')
            print(f'PASS: {len(rows)} secrets copied and verified; old wallet retained.', flush=True)
        finally:
            timer.cancel()
    except Exception as error:
        print('Failure type:', type(error).__name__, flush=True)
        print('Migration incomplete; old wallet retained via KDE compatibility service.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
