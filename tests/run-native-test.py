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
parser.add_argument('--backend', choices=('x11', 'wayland'), default='x11')
parser.add_argument('--screen-reader', action='store_true')
args = parser.parse_args()
repo = Path.cwd()

with tempfile.TemporaryDirectory(prefix='cinnamon-menu-test-') as directory:
    root = Path(directory)
    env = dict(os.environ, GSETTINGS_BACKEND='keyfile', LIBGL_ALWAYS_SOFTWARE='1',
               XDG_CURRENT_DESKTOP='X-Cinnamon', XDG_SESSION_TYPE=args.backend,
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
        command = ['cinnamon', '--replace'] if args.backend == 'x11' else ['cinnamon', '--wayland', '--nested']
        cinnamon = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        helpers = []
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

            is_wayland = evaluate('imports.gi.Meta.is_wayland_compositor()')
            assert is_wayland == (args.backend == 'wayland'), (args.backend, is_wayland)
            print(f'Confirmed Cinnamon compositor backend: {args.backend}', flush=True)
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

            if args.screen_reader:
                if args.backend == 'wayland':
                    for name in ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY'):
                        value = evaluate(f"imports.gi.GLib.getenv('{name}')")
                        if value:
                            env[name] = value

                def start(command):
                    process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                    helpers.append(process)
                    return process

                start(['pulseaudio', '-n', '--daemonize=no', '--exit-idle-time=-1',
                       '--load=module-native-protocol-unix',
                       '--load=module-null-sink sink_name=cinnamon_test'])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if subprocess.run(['pactl', 'info'], env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL).returncode == 0:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError('Isolated PulseAudio server did not start')

                speech_config = root / 'speech-dispatcher'
                speech_config.mkdir()
                (speech_config / 'speechd.conf').write_text(
                    'AddModule "espeak-ng" "sd_espeak-ng" "espeak-ng.conf"\n'
                    'DefaultModule espeak-ng\nAudioOutputMethod "pulse"\nLogLevel 4\n')
                (speech_config / 'modules').symlink_to('/etc/speech-dispatcher/modules')
                env['SPEECHD_ADDRESS'] = f'unix_socket:{root / "speech.sock"}'
                start(['speech-dispatcher', '-s', '-C', str(speech_config),
                       '-S', str(root / 'speech.sock'), '-t', '0'])
                deadline = time.monotonic() + 10
                while not (root / 'speech.sock').exists() and time.monotonic() < deadline:
                    time.sleep(0.25)
                assert (root / 'speech.sock').exists(), 'Speech Dispatcher did not start'

                orca_log = Path('/tmp/cinnamon-menu-orca.log')
                orca_log.unlink(missing_ok=True)
                orca = start(['orca', '--debug-file', str(orca_log), '--disable=braille', '--enable=speech'])
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if orca.poll() is not None:
                        raise RuntimeError(f'Orca exited with {orca.returncode}')
                    output = orca_log.read_text() if orca_log.exists() else ''
                    if 'Starting Atspi main event loop' in output:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError('Orca did not finish starting')
                assert 'SPEECH: Not available' not in output, output[-4000:]

                for enabled, label in ((True, 'Result with details'), (False, 'Result without details')):
                    offset = orca_log.stat().st_size
                    result = evaluate('''(() => {
                        const menu = imports.ui.appletManager.definitions.find(
                            item => item.uuid === 'menu@cinnamon.org').applet;
                        menu.menu.close(false);
                        menu.searchEntry.set_text('');
                        menu.showDescription = ENABLED;
                        imports.ui.searchProviderManager.launch_all = (pattern, callback) => {
                            callback({}, [{id: 'orca-test', label: LABEL, description: 'Project directory'}]);
                        };
                        menu.menu.open(false);
                        menu.searchEntry.set_text('cinnamon-live-orca-regression');
                        const button = menu._searchProviderButtons[0];
                        return {name: button.actor.get_accessible().accessible_name,
                                description: button.descriptionLabel && button.descriptionLabel.get_text()};
                    })()'''.replace('ENABLED', json.dumps(enabled)).replace('LABEL', json.dumps(label)))
                    deadline = time.monotonic() + 15
                    speech = []
                    previous_speech = []
                    speech_changed_at = time.monotonic()
                    while time.monotonic() < deadline:
                        with orca_log.open() as stream:
                            stream.seek(offset)
                            output = stream.read()
                        speech = [line for line in output.splitlines() if 'SPEECH OUTPUT:' in line]
                        if speech != previous_speech:
                            speech_changed_at = time.monotonic()
                            previous_speech = speech
                        if any(label in line for line in speech) and time.monotonic() - speech_changed_at >= 1:
                            break
                        time.sleep(0.25)
                    else:
                        raise RuntimeError(f'Orca speech did not settle for {label}: {speech}')
                    assert any(label in line for line in speech), output[-8000:]
                    includes_description = any('Project directory' in line for line in speech)
                    assert includes_description == (enabled and not args.expect_failure), speech
                    print(json.dumps({'live_orca': label, 'result': result, 'speech': speech}), flush=True)
        except BaseException:
            log.flush()
            print(Path(log.name).read_text()[-16000:], flush=True)
            orca_log = Path('/tmp/cinnamon-menu-orca.log')
            if args.screen_reader and orca_log.exists():
                print(orca_log.read_text()[-12000:], flush=True)
            raise
        finally:
            for process in reversed(helpers):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            cinnamon.terminate()
            try:
                cinnamon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cinnamon.kill()
                cinnamon.wait()
