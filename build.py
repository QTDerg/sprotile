"""Build the installable Sprotile ZIP with Python 3.11 or later."""

from pathlib import Path
import re
import tempfile
import tomllib
import zipfile


def build(root, output_directory=None):
    manifest = root/'blender_manifest.toml'
    with manifest.open('rb') as stream:
        version = tomllib.load(stream)['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError(f'Expected a three-part extension version, got {version!r}')

    source = root/'sprotile'
    if not (source/'__init__.py').is_file():
        raise FileNotFoundError(source/'__init__.py')
    notes = root/'docs'/f'Sprotile-{version}-Notes.txt'
    members = [manifest, *sorted(source.glob('*.py')), root/'LICENSE', notes]
    destination = root/'dist' if output_directory is None else output_directory
    destination.mkdir(exist_ok=True)
    archive = destination/f'Sprotile-{version}.zip'

    with tempfile.TemporaryDirectory(prefix='.sprotile-build-', dir=destination) as stage:
        candidate = Path(stage)/archive.name
        with zipfile.ZipFile(candidate, 'x') as output:
            for path in members:
                member = zipfile.ZipInfo(path.name, (1980, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                # Storing the small source files keeps the ZIP independent of zlib versions.
                output.writestr(member, path.read_bytes())
        candidate.replace(archive)
    return archive


if __name__ == '__main__':
    print(build(Path(__file__).resolve().parent))
