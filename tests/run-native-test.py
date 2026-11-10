#!/usr/bin/python3
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

parser = argparse.ArgumentParser()
parser.add_argument('--expect-failure', action='store_true')
args = parser.parse_args()
repo = Path.cwd()

with tempfile.TemporaryDirectory(prefix='cinnamon-menu-test-') as directory:
    root = Path(directory)
    env = dict(os.environ, GSETTINGS_BACKEND='keyfile', LIBGL_ALWAYS_SOFTWARE='1',
               XDG_CURRENT_DESKTOP='X-Cinnamon', XDG_SESSION_TYPE='x11',
               CINNAMON_SOFTWARE_RENDERING='1')
    for name in ('DATA', 'CONFIG', 'CACHE', 'STATE'):
        env[f'XDG_{name}_HOME'] = str(root / name.lower())
    runtime = root / 'runtime'
    runtime.mkdir(mode=0o700)
    env['XDG_RUNTIME_DIR'] = str(runtime)
    subprocess.run(['gsettings', 'set', 'org.cinnamon', 'enabled-applets',
                    "['panel1:left:0:menu@cinnamon.org:0']"], env=env, check=True)
    subprocess.run(['gsettings', 'set', 'org.cinnamon', 'enabled-extensions', '[]'], env=env, check=True)

    with open('/tmp/cinnamon-menu-session.log', 'w') as log:
        cinnamon = subprocess.Popen(['cinnamon', '--replace'], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            def evaluate(code):
                reply = connection.call_sync('org.Cinnamon', '/org/Cinnamon', 'org.Cinnamon',
                    'Eval', GLib.Variant('(s)', (code,)), GLib.VariantType.new('(bs)'),
                    Gio.DBusCallFlags.NONE, 5000, None).unpack()
                if not reply[0]:
                    raise RuntimeError(f'Cinnamon Eval failed: {reply[1]}')
                return json.loads(reply[1]) if reply[1] else None

            deadline = time.monotonic() + 75
            last_error = None
            while time.monotonic() < deadline:
                if cinnamon.poll() is not None:
                    raise RuntimeError(f'Cinnamon exited with {cinnamon.returncode}')
                try:
                    ready = evaluate("imports.ui.appletManager.appletsLoaded && "
                        "imports.ui.appletManager.definitions.some(d => d.uuid === 'menu@cinnamon.org' && d.applet)")
                    if ready:
                        break
                except (GLib.Error, RuntimeError) as error:
                    last_error = str(error)
                time.sleep(0.25)
            else:
                raise RuntimeError(f'Menu did not finish loading; last error: {last_error}')

            script = repo / 'tests/interactive/menu-search-provider-descriptions.js'
            results = evaluate(script.read_text())
            print(json.dumps(results, indent=2), flush=True)
            failures = [case['name'] for case in results if not case['passed']]
            if args.expect_failure:
                assert failures == ['description enabled', 'plain text description'], failures
                print('Confirmed missing description rows in the unmodified menu', flush=True)
            else:
                assert not failures, failures
                assert len(results) == 7
                print('All seven native menu checks passed', flush=True)
        except BaseException:
            log.flush()
            print(Path(log.name).read_text()[-16000:], flush=True)
            raise
        finally:
            cinnamon.terminate()
            try:
                cinnamon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cinnamon.kill()
                cinnamon.wait()
