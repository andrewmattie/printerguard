"""Run local release checks without HA access, provider requests or deployment."""
from pathlib import Path
import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, environment):
    subprocess.run(arguments, cwd=ROOT, env=environment, check=True)


def check_public_links():
    if (ROOT / '.git').exists():
        output = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
        files = {ROOT / path for path in output.decode().split('\0') if path}
    else:
        files = {path for path in ROOT.rglob('*') if path.is_file()}
    for document in files:
        if document.suffix != '.md':
            continue
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', document.read_text()):
            reference = urlsplit(target)
            if reference.scheme or reference.netloc or not reference.path:
                continue
            resolved = (document.parent / unquote(reference.path)).resolve()
            if resolved not in files and not any(resolved in path.parents for path in files):
                raise SystemExit(f'Public link is missing from release files: {document.relative_to(ROOT)} -> {target}')


def check_hassfest(core):
    core = core.resolve()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=core).decode().strip()
    if revision != '357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b':
        raise SystemExit('hassfest requires the pinned Home Assistant Core 2026.10.4 checkout.')
    if importlib.metadata.version('homeassistant') != '2026.10.4':
        raise SystemExit('Install requirements-hassfest.txt for the supported HA baseline.')
    site_packages = importlib.metadata.distribution('homeassistant').locate_file('')
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
    subprocess.run([sys.executable, '-B', '-m', 'script.hassfest', '--core-path', str(site_packages),
                    '--integration-path', str(ROOT / 'custom_components/p1s_guard')],
                   cwd=core, env=environment, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hassfest-core', type=Path, help='Core 2026.10.4 source checkout for hassfest')
    arguments = parser.parse_args()
    if os.name != 'posix' or sys.version_info < (3, 14, 2):
        raise SystemExit('Checks require a POSIX host and Python 3.14.2 or newer.')
    import yaml

    sys.path.insert(0, str(ROOT / 'custom_components'))
    from p1s_guard.config import parse_config

    manifest = json.loads((ROOT / 'custom_components/p1s_guard/manifest.json').read_text())
    if manifest['domain'] != 'p1s_guard' or not manifest['version']:
        raise SystemExit('Invalid integration manifest identity or version.')
    example = yaml.safe_load((ROOT / 'examples/configuration.yaml').read_text())
    parse_config(example['p1s_guard'])
    check_public_links()
    with tempfile.TemporaryDirectory(prefix='printguard-check-') as cache:
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': cache}
        run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], environment)
        run([sys.executable, '-m', 'compileall', '-q', 'custom_components', 'tests', 'scripts'], environment)
        if (ROOT / '.git').exists():
            run(['git', 'diff', '--check'], environment)
            run(['git', 'diff', '--cached', '--check'], environment)
        else:
            print('Git whitespace check skipped: this is not a Git checkout.')
    print('Unit, compilation, manifest JSON, example configuration and public link checks passed.')
    if arguments.hassfest_core:
        check_hassfest(arguments.hassfest_core)
    else:
        print('hassfest skipped; pass --hassfest-core with the pinned Core checkout to run it.')
    print('Home Assistant lifecycle and supervised live checks are separate.')


if __name__ == '__main__':
    main()
