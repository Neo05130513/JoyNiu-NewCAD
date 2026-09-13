#!/usr/bin/env python3
"""Build a new local LibreDWG tools tag from the existing pinned builder stage.

python deploy/build_currentcad_tools.py --tag joyniu-currentcad-tools:DATE-ARCH \
    --source-archive /private/cache/libredwg-0.14.tar.xz

The optional archive must match Dockerfile.api's exact release SHA-256. Only
that archive and the numeric adapter C source enter a fresh private context.
No existing image tag, application environment or running service is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def prepare_context(root: Path, archive: Path | None = None, platform: str | None = None, jobs: int = 1) -> Path:
    text = (root / 'deploy/Dockerfile.api').read_text()
    marker = '\nFROM ${PYTHON_IMAGE} AS api\n'
    if text.count(marker) != 1:
        raise ValueError('Existing DWG builder boundary changed; review before building')
    builder = text.split(marker)[0] + '\n'
    if jobs not in (1, 2):
        raise ValueError('Tools build supports one or two compiler jobs')
    builder = builder.replace('make -j2', f'make -j{jobs}')
    if platform:
        if platform not in ('linux/amd64', 'linux/arm64'):
            raise ValueError('Unsupported tools platform')
        architecture = platform.split('/')[1]
        builder = builder.replace('FROM ${PYTHON_IMAGE} AS dwg-builder',
            f'FROM --platform={platform} ${{PYTHON_IMAGE}} AS dwg-builder\n'
            f'RUN test "$(dpkg --print-architecture)" = "{architecture}"')
    version = re.search(r'^ARG LIBREDWG_VERSION=(\S+)$', builder, re.M)
    checksum = re.search(r'^ARG LIBREDWG_SHA256=([0-9a-f]{64})$', builder, re.M)
    if not version or version[1] != '0.14' or not checksum:
        raise ValueError('Expected pinned LibreDWG 0.14 builder')
    if archive is not None:
        if not archive.is_file() or archive.is_symlink():
            raise ValueError('Source archive must be a regular file')
        if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum[1]:
            raise ValueError('Source archive does not match pinned release SHA-256')
        pattern = r'RUN curl --fail[\s\S]*? -o source\.tar\.xz \\\n    && printf'
        builder, replaced = re.subn(pattern, 'COPY source.tar.xz ./source.tar.xz\nRUN printf', builder)
        if replaced != 1:
            raise ValueError('Existing download command changed; review cache substitution')
    folder = Path(tempfile.mkdtemp(prefix='joyniu-currentcad-tools-'))
    target = folder / 'apps/api/app'
    target.mkdir(parents=True)
    shutil.copy2(root / 'apps/api/app/native_drawing_libredwg.c', target)
    if archive is not None:
        shutil.copyfile(archive, folder / 'source.tar.xz')
    (folder / 'Dockerfile').write_text(builder)
    (folder / 'build-input.json').write_text(json.dumps({
        'version': version[1], 'sourceSha256': checksum[1], 'verifiedCachedArchive': archive is not None,
        'adapterSha256': hashlib.sha256((target / 'native_drawing_libredwg.c').read_bytes()).hexdigest(),
    }, indent=2) + '\n')
    return folder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True, help='A new, explicitly named local tools image tag')
    parser.add_argument('--source-archive', type=Path)
    parser.add_argument('--platform', choices=('linux/arm64', 'linux/amd64'))
    parser.add_argument('--jobs', type=int, choices=(1, 2), default=1,
                        help='Compiler jobs; default one avoids high peak memory under emulation')
    parser.add_argument('--python-image', help='Optional exact platform image digest for classic builders')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--verify-existing', action='store_true', help='Verify/export an existing full builder image without rebuilding it')
    parser.add_argument('--export-dir', type=Path, help='New directory for a small /out-only transfer archive and evidence')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*', args.tag):
        parser.error('Use an explicit repository:tag, not latest or a digest')
    if args.tag.endswith(':latest'):
        parser.error('Use a dated candidate tag')
    if args.export_dir and args.export_dir.exists():
        parser.error('Tools export directory must not exist')
    if not args.prepare_only:
        subprocess.run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'], check=True)
        exists = subprocess.run(['docker', 'image', 'inspect', args.tag], capture_output=True)
        if exists.returncode == 0 and not args.verify_existing:
            parser.error('Refusing to overwrite an existing image tag')
        if exists.returncode != 0 and args.verify_existing:
            parser.error('Requested existing tools image is not present')
    folder = prepare_context(ROOT, args.source_archive, args.platform, args.jobs)
    print(json.dumps({'context': str(folder), 'tag': args.tag, 'prepared': True}), flush=True)
    if args.prepare_only:
        return 0
    command = ['docker', 'build', '--tag', args.tag]
    if args.platform:
        command += ['--platform', args.platform]
    if args.python_image:
        command += ['--build-arg', 'PYTHON_IMAGE=' + args.python_image]
    if not args.verify_existing:
        subprocess.run([*command, str(folder)], check=True)
    platform = subprocess.run(['docker', 'image', 'inspect', args.tag, '--format', '{{.Os}}/{{.Architecture}}'],
                              check=True, capture_output=True, text=True).stdout.strip()
    if args.platform and platform != args.platform:
        raise RuntimeError('Built image platform differs from requested tools architecture')
    verification = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--entrypoint', 'sh', args.tag, '-ec',
        'for t in dwgread dwg2dxf dxf2dwg joyniu-native-dwg-adapter; do /out/$t --version; ldd /out/$t; done'],
        check=True, text=True, capture_output=True)
    (folder / 'toolchain-verification.txt').write_text(verification.stdout + verification.stderr)
    if 'not found' in verification.stdout or 'libredwg.so' in verification.stdout:
        raise RuntimeError('Tools must not require a shared LibreDWG runtime')
    if args.export_dir:
        args.export_dir.mkdir(parents=True, mode=0o700)
        out = args.export_dir / 'out'
        out.mkdir()
        container = subprocess.run(['docker', 'create', '--network', 'none', '--entrypoint', '/out/dwgread',
                                    args.tag, '--version'], check=True, capture_output=True, text=True).stdout.strip()
        try:
            # Verify the actual builder's inputs, not merely the current checkout.
            subprocess.run(['docker', 'cp', f'{container}:/tmp/native_drawing_libredwg.c',
                            str(folder / 'actual-adapter.c')], check=True)
            subprocess.run(['docker', 'cp', f'{container}:/tmp/libredwg/source.tar.xz',
                            str(folder / 'actual-source.tar.xz')], check=True)
            expected = json.loads((folder / 'build-input.json').read_text())
            if hashlib.sha256((folder / 'actual-adapter.c').read_bytes()).hexdigest() != expected['adapterSha256']:
                raise RuntimeError('Builder numeric adapter source differs from this release')
            if hashlib.sha256((folder / 'actual-source.tar.xz').read_bytes()).hexdigest() != expected['sourceSha256']:
                raise RuntimeError('Builder source archive differs from pinned LibreDWG release')
            for name in ('dwgread', 'dwg2dxf', 'dxf2dwg', 'joyniu-native-dwg-adapter'):
                subprocess.run(['docker', 'cp', f'{container}:/out/{name}', str(out / name)], check=True)
        finally:
            subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
        evidence = json.loads((folder / 'build-input.json').read_text())
        evidence.update({'image': args.tag, 'platform': platform, 'files': {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in out.iterdir()}})
        (args.export_dir / 'toolchain.json').write_text(json.dumps(evidence, indent=2) + '\n')
        (args.export_dir / 'toolchain-verification.txt').write_text(verification.stdout + verification.stderr)
        (args.export_dir / 'Dockerfile').write_text('FROM scratch\nCOPY out/ /out/\n')
        archive = args.export_dir / 'currentcad-tools.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for name in ('out', 'Dockerfile', 'toolchain.json', 'toolchain-verification.txt'):
                bundle.add(args.export_dir / name, arcname=name)
        print(json.dumps({'transferArchive': str(archive), 'platform': platform,
                          'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))
    print(json.dumps({'tag': args.tag, 'verified': True, 'evidence': str(folder)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
