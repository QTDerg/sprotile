"""Load the Sprotile package for a test, under a module name of the test's choosing.

    import sprotile_loader
    addon = sprotile_loader.load('sprotile_my_test')

The add-on is a package of modules, but the suites were written against one file: they read `addon.<name>` for
any function or constant, and patch names with `unittest.mock.patch.object(addon, ...)` or `addon.<name> = ...`.
`load` returns an AddonView that keeps that working:
- reading `addon.x` finds x in the package or any of its modules;
- setting `addon.x` sets x in every module that holds that name, so a patched helper is seen wherever the add-on
  uses it, and restoring it restores it everywhere.
"""
import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / 'sprotile'


class AddonView:
    """The whole add-on as one namespace (see the module docstring)."""

    __slots__ = ('_package',)

    def __init__(self, package):
        object.__setattr__(self, '_package', package)

    def _modules(self):
        package = object.__getattribute__(self, '_package')
        prefix = package.__name__ + '.'
        return [package] + [module for name, module in sorted(sys.modules.items())
                            if name.startswith(prefix) and module is not None]

    @property
    def __dict__(self):
        # unittest.mock reads target.__dict__[name] to decide how to restore a patch.
        merged = {}
        for module in reversed(self._modules()):
            merged.update(vars(module))
        return merged

    def __getattr__(self, name):
        for module in self._modules():
            namespace = vars(module)
            if name in namespace:
                return namespace[name]
        raise AttributeError(f'Sprotile has no {name!r}')

    def __setattr__(self, name, value):
        holders = [module for module in self._modules() if name in vars(module)]
        for module in holders or [object.__getattribute__(self, '_package')]:
            setattr(module, name, value)

    def __delattr__(self, name):
        for module in self._modules():
            if name in vars(module):
                delattr(module, name)

    def __repr__(self):
        return f'<Sprotile add-on {object.__getattribute__(self, "_package").__name__}>'


def source_files(package_dir=PACKAGE):
    """The add-on's own files in a fixed order: every module of the package."""
    return sorted(Path(package_dir).glob('*.py'))


def source_hash(package_dir=PACKAGE):
    """One SHA-256 over every module's name and bytes: what validation.json and the release check record."""
    digest = hashlib.sha256()
    for path in source_files(package_dir):
        digest.update(path.name.encode('utf-8') + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def release_inputs_hash(root=None):
    """One SHA-256 over everything else a validated release depends on: the tests and their fixtures, the packaging
    script, the manifest, the licence and every release-notes file. validation.json records it beside source_sha256, and package_release.py refuses to
    build if it changed, so a test or the notes can't be edited after the suite ran without running it again."""
    root = Path(root) if root is not None else Path(PACKAGE).parent
    fixtures = sorted(path for path in (root/'tests'/'fixtures').rglob('*') if path.is_file())
    files = sorted((root/'tests').glob('*.py')) + fixtures \
        + [root/'build.py', root/'package_release.py', root/'blender_manifest.toml', root/'LICENSE'] \
        + [root/'upstream.json', root/'upstream'/'sprotile.py'] \
        + sorted((root/'backups').glob('Sprotile-*.zip')) \
        + sorted((root/'docs').glob('Sprotile-*-Notes.txt'))
    digest = hashlib.sha256()
    for path in files:
        if path.exists():
            digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def load_package(name, package_dir=PACKAGE):
    """Import the package under `name` and return the real package module."""
    spec = importlib.util.spec_from_file_location(name, Path(package_dir) / '__init__.py',
                                                  submodule_search_locations=[str(package_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load(name, package_dir=PACKAGE):
    """Import the package under `name` and return an AddonView of it."""
    return AddonView(load_package(name, package_dir))
