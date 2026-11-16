#!/usr/bin/env python3
"""classify_conflicts.py — делит конфликтные файлы на механические и ручные.

Классы:
- take-theirs: версия ours есть в истории theirs — у ours нет своих правок;
- take-ours: версия theirs есть в истории ours;
- realbase-clean: 3-way merge от последней версии файла, общей для обеих
  историй, проходит без конфликтов; готовый черновик лежит в OUT/drafts/<path>;
- manual: обе стороны правили одно место, нет общей версии (ренейм на одной
  стороне), delete/modify или бинарный файл. Черновик diff3 от реальной базы,
  если она есть, лежит там же.

Виртуальная база `ort` при нескольких merge-base делает конфликтом весь файл,
даже если одна сторона просто старее другой. Реальная база это снимает.

Дальше скрипт ищет удалённые экспорты с потребителями: символ экспортировался
на одной из сторон, пропал из результата (черновика или авто-слитого файла),
а другой файл импортирует его из этого модуля. Так ловится «берём сущность из
master, а её старый контракт держит страница, которая есть только в ours».

Индекс и рабочую копию не трогает. Пишет только в OUT.

Usage: python3 scripts/classify_conflicts.py [--out DIR]
"""

import argparse
import functools
import os
from concurrent.futures import ThreadPoolExecutor
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from merge_history import (  # noqa: E402
    CODE_EXT, BlobReader, fast_forward_side, git, git_bytes, history_blobs, stage_blob,
    theirs_ref,
)

NAME = r'[A-Za-z_$][\w$]*'

TS_EXPORT = re.compile(
    r'^[ \t]*export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?'
    r'(?:const\s+enum|const|let|var|function\*?|class|interface|type|enum|namespace)\s+('
    + NAME + ')', re.M)
TS_EXPORT_LIST = re.compile(r'^[ \t]*export\s+(?:type\s+)?\{([^}]*)\}', re.M)
PHP_EXPORTS = [
    re.compile(r'^[ \t]*(?:final\s+|abstract\s+|readonly\s+)*(?:class|interface|trait|enum)\s+(\w+)', re.M),
    re.compile(r'^[ \t]*(?:final\s+|abstract\s+)*public\s+(?:static\s+)?function\s+(\w+)', re.M),
    re.compile(r'^[ \t]*(?:final\s+)?(?:public\s+)?const\s+(\w+)\s*=', re.M),
]
MIN_NAME_LEN = 4
MAX_FILES_SHOWN = 3
# Сколько общих версий пробовать базой. Порядок полной истории (`--full-history`)
# перемешивает боковые ветки по дате, и первая общая версия не всегда ближайшая.
BASE_CANDIDATES = 5
LFS_POINTER = b'version https://git-lfs'


def exported_names(path, text):
    if text is None:
        return set()
    names = set()
    if path.endswith('.php'):
        for rx in PHP_EXPORTS:
            names.update(rx.findall(text))
        names.discard('__construct')
        return names
    names.update(TS_EXPORT.findall(text))
    for block in TS_EXPORT_LIST.findall(text):
        for item in block.split(','):
            item = re.sub(r'^\s*type\s+', '', item).strip()
            if not item:
                continue
            names.add(re.split(r'\s+as\s+', item)[-1].strip())
    return {n for n in names if re.fullmatch(NAME, n)}


def decode(data):
    return None if data is None else data.decode('utf-8', errors='replace')


def is_binary(*blobs):
    """Бинарный файл или LFS-указатель: текстовый мерж указателей бессмыслен."""
    return any(b is not None and (b'\0' in b[:8000] or b.startswith(LFS_POINTER))
               for b in blobs)


def merge_file(base, ours, theirs):
    """git merge-file --diff3 в память. Возвращает (число конфликтов, текст)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (('ours', ours), ('base', base), ('theirs', theirs)):
            p = os.path.join(tmp, name)
            with open(p, 'wb') as fh:
                fh.write(data)
            paths.append(p)
        r = subprocess.run(['git', 'merge-file', '-p', '--diff3',
                            '-L', 'ours', '-L', 'realbase', '-L', 'theirs', *paths],
                           capture_output=True)
        if r.returncode < 0:
            raise RuntimeError(f'git merge-file: {r.stderr.decode()}')
        return r.returncode, r.stdout


def classify(path, ref):
    """Класс файла, причина и текст результата для поиска удалённых экспортов."""
    b2, b3 = stage_blob(2, path), stage_blob(3, path)
    if not b2 or not b3:
        return 'manual', 'delete/modify или файл добавлен одной стороной', None, None

    ours = git_bytes('cat-file', 'blob', b2)
    theirs = git_bytes('cat-file', 'blob', b3)
    side = fast_forward_side('HEAD', ref, path, b2, b3)
    if side == 'theirs':
        return 'take-theirs', '', theirs, None
    if side == 'ours':
        return 'take-ours', '', ours, None

    if is_binary(ours, theirs):
        return 'manual', 'бинарный файл или LFS-указатель', None, None

    ours_set = set(history_blobs('HEAD', path))
    candidates = [h for h in history_blobs(ref, path) if h in ours_set][:BASE_CANDIDATES]
    if not candidates:
        return 'manual', 'нет общей версии в историях (ренейм на одной стороне?)', None, None

    best = None
    for real_base in candidates:
        conflicts, draft = merge_file(git_bytes('cat-file', 'blob', real_base), ours, theirs)
        if best is None or conflicts < best[0]:
            best = (conflicts, draft, real_base)
        if conflicts == 0:
            break

    conflicts, draft, real_base = best
    if conflicts == 0:
        return 'realbase-clean', '', draft, draft
    return 'manual', f'{conflicts} конфликт(ов) от реальной базы {real_base[:8]}', None, draft


def write_draft(out, path, data):
    target = Path(out) / 'drafts' / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def removed_exports(results, ref, blobs):
    """Экспорты, пропавшие из результата: {имя: [(файл, класс)]}.

    Версии сторон — из стадий индекса, у авто-слитого файла — из HEAD и ref.
    """
    removed = {}
    for path, cls, result in results:
        if not path.endswith(CODE_EXT):
            continue
        before = set()
        for stage, side_ref in ((2, 'HEAD'), (3, ref)):
            data = blobs.read(f':{stage}:{path}')
            if data is None:
                data = blobs.read(f'{side_ref}:{path}')
            before |= exported_names(path, decode(data))
        after = exported_names(path, decode(result))
        for name in before - after:
            if len(name) >= MIN_NAME_LEN:
                removed.setdefault(name, []).append((path, cls))
    return removed


TS_IMPORT = re.compile(r'(?:import|export)\s+(?:type\s+)?\{([^}]*)\}\s*from\s*[\'"]([^\'"]+)[\'"]')
PHP_USE = re.compile(r'^[ \t]*use\s+([\w\\]+?)(?:\s+as\s+\w+)?;', re.M)


def imports_from(name, hit, source, text):
    """hit импортирует name из модуля, путь которого указывает на source.

    TS: относительный путь резолвим от каталога hit; алиас (`@entities/X`) и
    голый путь (`lib/utils/x`) ищем как сегменты пути source. PHP: `use ...\\Name;`.
    """
    if source.endswith('.php'):
        return any(u.split('\\')[-1] == name for u in PHP_USE.findall(text))
    src = os.path.splitext(source)[0]
    if src.endswith('/index'):
        src = src[:-len('/index')]
    for names, spec in TS_IMPORT.findall(text):
        if not re.search(r'(?<![\w$])' + re.escape(name) + r'(?![\w$])', names):
            continue
        if spec.startswith('.'):
            target = os.path.normpath(os.path.join(os.path.dirname(hit), spec))
            if src == target or src.startswith(target + '/'):
                return True
        elif '/' + spec.lstrip('@').rstrip('/') + '/' in '/' + src + '/' \
                or src.endswith('/' + spec.lstrip('@')):
            return True
    return False


def find_users(removed, resolved):
    """Файлы, которые импортируют удалённое имя из исходного модуля: {имя: [файл]}.

    Конфликтный файл читаем из черновика результата, а не из рабочей копии
    с маркерами. Тёзка из другого модуля потребителем не считается.
    """
    @functools.lru_cache(maxsize=None)
    def text_of(path):
        if path in resolved:
            return decode(resolved[path]) or ''
        try:
            return Path(path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''

    if not removed:
        return {}
    # Один проход `git grep` на все имена: `-o -z` печатает `файл\0имя` на каждое вхождение.
    out = git('grep', '-I', '-o', '-z', '-w', '-F',
              *[arg for name in removed for arg in ('-e', name)],
              '--', *[f'*{ext}' for ext in CODE_EXT])
    grep_hits = {}
    for line in out.splitlines():
        path, _, name = line.partition('\0')
        grep_hits.setdefault(name, {})[path] = None

    users = {}
    for name, sources in removed.items():
        own = {p for p, _ in sources}
        hits = [f for f in grep_hits.get(name, ()) if f not in own
                and any(imports_from(name, f, src, text_of(f)) for src in own)]
        if hits:
            users[name] = hits
    return users


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', help='куда положить списки и черновики')
    args = parser.parse_args()

    ref = theirs_ref()
    if not ref:
        print('Нет MERGE_HEAD / CHERRY_PICK_HEAD / REBASE_HEAD / REVERT_HEAD: история theirs')
        print('недоступна (stash pop/apply). Классификация не применима — разбирай вручную.')
        return 0

    files = git('diff', '--name-only', '--diff-filter=U').splitlines()
    if not files:
        print('Unmerged-файлов нет.')
        return 0

    out = args.out or tempfile.mkdtemp(prefix='merge-resolve-')
    Path(out).mkdir(parents=True, exist_ok=True)

    groups = {'take-theirs': [], 'take-ours': [], 'realbase-clean': [], 'manual': []}
    reasons = {}
    results = []
    # История файла в большом репо — до секунды на сторону; git-процессы
    # независимы, поэтому файлы идут параллельно.
    with ThreadPoolExecutor(max_workers=8) as pool:
        classified = list(pool.map(lambda p: classify(p, ref), files))

    for path, (cls, reason, result, draft) in zip(files, classified):
        groups[cls].append(path)
        if draft is not None:
            write_draft(out, path, draft)
        if reason:
            reasons[path] = reason
        if result is not None:
            results.append((path, cls, result))

    for cls, paths in groups.items():
        Path(out, f'{cls}.txt').write_text(''.join(f'{p}\n' for p in paths))

    print(f'ОПЕРАЦИЯ: theirs = {ref}, ours = HEAD (при rebase стороны инвертированы)')
    print(f'КЛАССИФИКАЦИЯ ({len(files)} файлов). Списки и черновики: {out}')
    for cls, paths in groups.items():
        print(f'  {cls:<14} {len(paths):4}  {out}/{cls}.txt')

    if groups['manual']:
        print('\nРУЧНЫЕ (черновик diff3 от реальной базы — в drafts/, если база есть):')
        for path in groups['manual']:
            print(f'  {path} — {reasons[path]}')

    # Авто-слитые файлы тоже меняют контракт: git убрал реэкспорт из индекса
    # одной стороны, а потребитель другой стороны его импортирует.
    blobs = BlobReader()
    for path in git('diff', '--cached', '--name-only', '--diff-filter=M', 'HEAD').splitlines():
        if path.endswith(CODE_EXT) and path not in files:
            data = blobs.read(f':0:{path}')
            if data is not None:
                results.append((path, 'auto', data))

    removed = removed_exports(results, ref, blobs)
    blobs.close()
    users = find_users(removed, {path: result for path, _, result in results})
    if users:
        print('\nУДАЛЁННЫЕ ЭКСПОРТЫ С ПОТРЕБИТЕЛЯМИ (результат убрал символ, а файлы импортируют его оттуда):')
        for name in sorted(users):
            src = ', '.join(f'{p} [{c}]' for p, c in removed[name])
            hits = users[name]
            more = f' (+{len(hits) - MAX_FILES_SHOWN})' if len(hits) > MAX_FILES_SHOWN else ''
            print(f'  {name} — убран из {src}; встречается: '
                  f'{", ".join(hits[:MAX_FILES_SHOWN])}{more}')
        print('Каждая строка — развилка для плана: потребитель другой стороны держит старый '
              'контракт. [auto] — файл слил git, без конфликта.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
