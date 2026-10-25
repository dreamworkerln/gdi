"""Inspect and fault-inject the actual one-file CI delivery in memory fixtures."""

import io
import zipfile

from gdi.ci_package import PACKAGE_PATTERN, build_package
from gdi.exchange import decode, digest, encode


def package_path(store, prefix):
    paths = [path for path in store.data if path.rpartition('/')[0] == prefix
             and PACKAGE_PATTERN.fullmatch(path.rpartition('/')[2])]
    if len(paths) != 1:
        raise AssertionError(f'expected one result package in {prefix}, found {paths}')
    return paths[0]


def stored_result(store, prefix):
    with zipfile.ZipFile(io.BytesIO(store.data[package_path(store, prefix)])) as archive:
        return decode(archive.read('result.json'))


def change_package(store, prefix, *, result_change=None, artifact_change=None):
    path = package_path(store, prefix)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(store.data[path])) as source, zipfile.ZipFile(output, 'w') as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == 'result.json' and result_change is not None:
                value = decode(data)
                result_change(value)
                data = encode(value)
            if artifact_change is not None:
                data = artifact_change(info.filename, data)
            target.writestr(info, data)
    data = output.getvalue()
    updated = prefix + '/result-' + digest(data) + '.zip'
    del store.data[path]
    store.data[updated] = data
    return updated


def publish_package(store, spool, prefix, req):
    path = build_package(spool, req, digest(encode(req)))
    store.upload(path, prefix + '/' + path.name)


def historical_delivery(store, prefix):
    """Turn a completed fixture into an existing pre-package result."""
    path = package_path(store, prefix)
    with zipfile.ZipFile(io.BytesIO(store.data.pop(path))) as archive:
        for info in archive.infolist():
            if info.filename != 'package.json':
                target = prefix + '/' + info.filename
                store.mkdir(target.rpartition('/')[0])
                store.data[target] = archive.read(info)
    data = store.data[prefix + '/build.log']
    if data:
        directory = prefix + '/log-chunks'
        store.mkdir(directory)
        for number, offset in enumerate(range(0, len(data), 256 * 1024), 1):
            chunk = data[offset:offset + 256 * 1024]
            store.data[directory + f'/{number:08d}-{digest(chunk)}.bin'] = chunk
