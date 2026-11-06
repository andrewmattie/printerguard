"""Run local release checks without HA access, provider requests or deployment."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, environment):
    subprocess.run(arguments, cwd=ROOT, env=environment, check=True)


def main():
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
    with tempfile.TemporaryDirectory(prefix='printguard-check-') as cache:
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': cache}
        run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], environment)
        run([sys.executable, '-m', 'compileall', '-q', 'custom_components', 'tests', 'scripts'], environment)
        if (ROOT / '.git').exists():
            run(['git', 'diff', '--check'], environment)
            run(['git', 'diff', '--cached', '--check'], environment)
        else:
            print('Git whitespace check skipped: this is not a Git checkout.')
    print('Unit, compilation, manifest JSON and example configuration checks passed.')
    print('Home Assistant lifecycle, hassfest and supervised live checks are separate.')


if __name__ == '__main__':
    main()
