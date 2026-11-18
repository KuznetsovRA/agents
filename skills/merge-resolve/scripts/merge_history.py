"""merge_history.py — общие хелперы скриптов merge-resolve: стороны операции и история блобов.

Только чтение git: индекс и рабочую копию не трогает.
"""

import functools
import re
from collections import namedtuple
import subprocess

CODE_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.php', '.vue')
HEX40 = re.compile(r'\b[0-9a-f]{40}\b')
NULL_BLOB = '0' * 40
OBJECT_TYPES = {b'blob', b'tree', b'commit', b'tag'}


class BlobReader:
    """Читает `<rev>:<path>` и `:<stage>:<path>` одним процессом `git cat-file --batch`.

    Процесс на каждый `git show` стоит ~17 мс: на тысячах файлов это минуты.
    """

    def __init__(self):
        self._proc = subprocess.Popen(['git', 'cat-file', '--batch'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, spec):
        """Содержимое объекта или None, если его нет."""
        self._proc.stdin.write(spec.encode() + b'\n')
        self._proc.stdin.flush()
        # `<sha> <type> <size>` и тело, или `<spec> missing` без тела. Spec с
        # пробелом в пути тоже даёт три поля — отличаем по типу объекта.
        header = self._proc.stdout.readline().split()
        if len(header) != 3 or header[1] not in OBJECT_TYPES or not header[2].isdigit():
            return None
        data = self._proc.stdout.read(int(header[2]))
        self._proc.stdout.read(1)
        return data if header[1] == b'blob' else None

    def close(self):
        self._proc.stdin.close()
        self._proc.wait()


def changed_between(rev_a, rev_b):
    """Пути, которые отличаются между двумя ревизиями (rev_b=None — рабочая копия)."""
    args = ['diff', '--name-only', '--no-renames', rev_a] + ([rev_b] if rev_b else [])
    return set(git(*args).splitlines())


def git(*args):
    r = subprocess.run(('git',) + args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ''


def git_bytes(*args):
    r = subprocess.run(('git',) + args, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def theirs_ref():
    """Ref вливаемой стороны: MERGE_HEAD, CHERRY_PICK_HEAD, REBASE_HEAD, REVERT_HEAD.

    None — у операции нет ref'а (stash pop/apply): истории theirs нет.
    """
    for ref in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REBASE_HEAD', 'REVERT_HEAD'):
        if git('rev-parse', '--verify', '-q', ref).strip():
            return ref
    return None


def stage_blob(stage, path):
    """Хеш блоба стадии индекса (1 base, 2 ours, 3 theirs) или None."""
    return git('rev-parse', '--verify', '-q', f':{stage}:{path}').strip() or None


def ref_blob(ref, path):
    return git('rev-parse', '--verify', '-q', f'{ref}:{path}').strip() or None


@functools.lru_cache(maxsize=None)
def history_blobs(ref, path):
    """Все версии файла в истории ref, от новых к старым, без повторов.

    `-c --raw` показывает и merge-коммиты: версия, собранная резолвом
    конфликта в мерже, тоже часть истории стороны. `--full-history` не даёт
    git выкинуть боковую ветку, где файл совпал с одним из родителей мержа:
    версия с такой ветки иначе выпадает из истории. Лимита `-n` нет: в полной
    истории сотни пустых merge-коммитов, и лимит съедают они, а не правки файла.
    """
    out = git('log', '--full-history', '-c', '--raw', '--no-abbrev', '--no-renames',
              '--format=', ref, '--', path)
    seen = {}
    for line in out.splitlines():
        if not line.startswith(':'):
            continue
        for h in HEX40.findall(line):
            if h != NULL_BLOB:
                seen.setdefault(h, None)
    head = ref_blob(ref, path)
    if head:
        seen = {head: None, **seen}
    return tuple(seen)


def came_back(blob, own_hist, other_hist):
    """Сторона уже прошла версию новее blob из истории другой стороны и вернулась к blob.

    Так выглядит откат: revert восстанавливает старую версию, и она есть в
    чужой истории, но своей правкой сторона её не «отстала», а вернула.
    """
    newer = set(other_hist[:other_hist.index(blob)])
    return any(h in newer for h in own_hist if h != blob)


def fast_forward_side(ours_ref, theirs_ref_name, path, ours_blob, theirs_blob):
    """Какая сторона файла — просто более новая версия другой.

    Возвращает:
    - 'theirs' — версия ours есть в истории theirs: своих правок у ours нет;
    - 'ours' — версия theirs есть в истории ours;
    - None — обе стороны правили файл независимо, одна из них откатила файл
      к старой версии, или истории нет.
    """
    if not ours_blob or not theirs_blob or ours_blob == theirs_blob:
        return None
    ours_hist = history_blobs(ours_ref, path)
    theirs_hist = history_blobs(theirs_ref_name, path)
    ours_in_theirs = ours_blob in theirs_hist
    theirs_in_ours = theirs_blob in ours_hist
    if ours_in_theirs == theirs_in_ours:
        return None
    side, blob, own, other = (('theirs', ours_blob, ours_hist, theirs_hist) if ours_in_theirs
                              else ('ours', theirs_blob, theirs_hist, ours_hist))
    return None if came_back(blob, own, other) else side


Revert = namedtuple('Revert', 'side sha subject paths')


def find_reverts(ref):
    """Откаты в собственных диапазонах сторон, новые первыми.

    Откат ищем по строке `This reverts commit`, которую пишет `git revert`.
    """
    found = []
    for side, rng in (('theirs', f'HEAD..{ref}'), ('ours', f'{ref}..HEAD')):
        log = git('log', '--grep=^This reverts commit', '--name-only', '--no-renames',
                  '--format=%x00%H%x09%s', rng)
        for entry in log.split('\0')[1:]:
            header, *paths = [line for line in entry.splitlines() if line]
            sha, _, subject = header.partition('\t')
            found.append(Revert(side, sha, subject, paths))
    return found
