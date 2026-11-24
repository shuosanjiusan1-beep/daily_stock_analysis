"""Public data download and fail-closed daily-bar validation; no credentials."""
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import struct
import tarfile
from urllib.request import urlopen

RELEASE_API = 'https://api.github.com/repos/chenditc/investment_data/releases/latest'


def read_json(url):
    with urlopen(url, timeout=60) as response:
        return json.load(response)


def download(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    release = read_json(RELEASE_API)
    assets = {a['name']: a['browser_download_url'] for a in release['assets']}
    manifest = read_json(assets['qlib_bin.manifest.json'])
    archive = destination / 'download.tar.gz'
    digest = hashlib.sha256()
    with urlopen(assets['qlib_bin.tar.gz'], timeout=120) as response, archive.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != manifest['archive_sha256'].removeprefix('sha256:'):
        raise ValueError('Data checksum mismatch')
    if archive.stat().st_size != manifest['archive_size_bytes']:
        raise ValueError('Data size mismatch')
    with tarfile.open(archive) as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith('/') or '..' in parts or member.issym() or member.islnk():
                raise ValueError('Unsafe archive member')
            if not member.isfile():
                continue
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, target.open('wb') as output:
                while chunk := incoming.read(1024 * 1024):
                    output.write(chunk)
    archive.unlink()
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return destination / 'qlib_bin'


def value_at(provider, code, field, index):
    path = Path(provider) / 'features' / code.lower() / f'{field}.day.bin'
    if not path.is_file():
        return float('nan')
    with path.open('rb') as source:
        header = source.read(4)
        if len(header) != 4:
            return float('nan')
        start = struct.unpack('<f', header)[0]
        if not math.isfinite(start) or start != int(start) or index < start:
            return float('nan')
        source.seek(4 * (1 + index - int(start)))
        raw = source.read(4)
    return struct.unpack('<f', raw)[0] if len(raw) == 4 else float('nan')


def check(provider, expected):
    provider = Path(provider)
    dates = (provider / 'calendars/day.txt').read_text().splitlines()
    if dates != sorted(set(dates)) or expected not in dates:
        raise ValueError(f'Data does not cover required session {expected}')
    index = dates.index(expected)
    codes = set()
    for line in (provider / 'instruments/csi300.txt').read_text().splitlines():
        code, start, end = line.split()
        if start <= expected <= end:
            codes.add(code)
    if not 280 <= len(codes) <= 320:
        raise ValueError('Unexpected CSI300 constituent count')
    missing = []
    for code in sorted(codes | {'SH000300'}):
        values = [value_at(provider, code, field, index) for field in ('open', 'close', 'high', 'low', 'volume')]
        if not all(math.isfinite(v) and v > 0 for v in values):
            missing.append(code)
    # Suspensions may exist, but never silently accept a missing benchmark or widespread gaps.
    if 'SH000300' in missing or len(missing) / len(codes) > .05:
        raise ValueError(f'Insufficient valid daily bars: {len(missing)} missing')
    return {'required_session': expected, 'calendar_last': dates[-1], 'constituents': len(codes),
            'missing_or_suspended': missing, 'valid_ratio': 1 - len(missing) / (len(codes) + 1)}
