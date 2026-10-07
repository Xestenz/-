"""Atomic, repeatable export of completed waybills."""
import os
import uuid
import hashlib
from pathlib import Path

import fitz

MARKER = 'Waybill completed PDF'


def preserve_source(path, cache_folder):
    """Keep immutable working input before the shared-folder file is renamed."""
    source = Path(path)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    directory = Path(cache_folder)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + source.suffix.lower())
    if not target.exists():
        temporary = directory / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target)


def is_completed(path):
    if Path(path).suffix.lower() != '.pdf':
        return False
    try:
        with fitz.open(path) as doc:
            return doc.metadata.get('subject') == MARKER
    except Exception:
        return False


def marked_pdf(data):
    with fitz.open(stream=data, filetype='pdf') as doc:
        doc.set_metadata({**doc.metadata, 'subject': MARKER})
        return doc.tobytes()


def archive_pdf(data, filename, job_id, primary, mirror=''):
    """Job suffix avoids overwriting other sheets for the same client/date."""
    if not primary:
        raise ValueError('Не задана основная папка OUTPUT_FOLDER')
    name = Path(filename).stem + ' [' + job_id + '].pdf'
    destinations = []
    for folder in (primary, mirror):
        if not folder:
            continue
        directory = Path(folder)
        directory.mkdir(parents=True, exist_ok=True)
        if any(os.path.samefile(directory, previous.parent) for previous in destinations):
            continue
        target = directory / name
        temporary = directory / ('.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        destinations.append(target)
    return [str(p) for p in destinations]


def replace_scanned_file(data, filename, job_id, original, cached, mirror=''):
    """Remove the unchanged inbox original only after all outputs are saved."""
    source = Path(original)
    cache = Path(cached)
    if not cache.is_file() or os.path.abspath(source) == os.path.abspath(cache):
        raise ValueError('Нет отдельной рабочей копии исходного скана')
    expected = hashlib.sha256(cache.read_bytes()).digest()
    if source.exists() and hashlib.sha256(source.read_bytes()).digest() != expected:
        raise ValueError('Исходный файл изменён после загрузки; замена отменена')
    paths = archive_pdf(data, filename, job_id, str(source.parent), mirror)
    if source.exists():
        if any(os.path.samefile(source, path) for path in paths):
            raise ValueError('Путь готового файла совпадает с исходником')
        if hashlib.sha256(source.read_bytes()).digest() != expected:
            raise ValueError('Исходник изменился во время сохранения; он оставлен без изменений')
        source.unlink()
    return paths
