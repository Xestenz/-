"""Atomic, repeatable export of completed waybills."""
import os
import uuid
from pathlib import Path

import fitz

MARKER = 'Waybill completed PDF'


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
